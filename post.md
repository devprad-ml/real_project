# My tests were a superuser. My app was not.

I've been building a document processing pipeline as a side project — Postgres, SQLAlchemy,
a worker that pulls jobs off a table. Last night I found a bug that had been sitting in it
since the very first migration, and I only found it by accident, while checking something
unrelated.

The bug isn't in the application code. It's in the gap between how my tests connect to the
database and how my application connects to it.

## The setup

The project has a `audit_log` table that is supposed to be append-only. Not "append-only by
convention" — enforced by the database, so that no amount of careless application code can
rewrite history:

```sql
REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;
```

There's a dedicated application role, `app_user`, with deliberately narrow grants. It can
read and insert almost everywhere, update only the tables that genuinely need mutation, and
delete nowhere:

```sql
CREATE ROLE app_user WITH LOGIN PASSWORD '...';
GRANT SELECT, INSERT         ON audit_log TO app_user;
GRANT SELECT, INSERT, UPDATE ON documents TO app_user;
```

I was pleased with this. It's the kind of control that survives a bad refactor, a sloppy
ORM cascade, or a future me who forgets the rule. The database doesn't care about my
intentions.

## How I found it

I'd just added a `jobs` table — a Postgres-as-queue, claimed with `SELECT ... FOR UPDATE
SKIP LOCKED`. To keep the claim query fast I put a partial index on it:

```sql
CREATE INDEX ix_jobs_claim ON jobs (run_after) WHERE state = 'PENDING';
```

That predicate has a trap in it. I use SQLAlchemy's `Enum(..., native_enum=False)`, which
stores the enum **member name**, not its value. My Python enum is:

```python
class JobState(str, Enum):
    PENDING = "pending"
```

So the column holds `'PENDING'`, not `'pending'`. Write the predicate with the lowercase
value and the index matches zero rows forever: it exists, `\d jobs` shows it, nothing ever
uses it, and the only symptom is a sequential scan that nobody notices until the table is
large. My tests would have passed either way — a dead index is still a correct query.

So I wrote a throwaway script to check what was actually stored and what the index actually
said. It printed what I hoped:

```
stored:   [('NORMALIZE', 'PENDING')]
indexdef: CREATE INDEX ix_jobs_claim ON public.jobs USING btree (run_after)
          WHERE ((state)::text = 'PENDING'::text)
```

Then the script tried to clean up after itself, and died:

```
psycopg2.errors.InsufficientPrivilege: permission denied for table jobs
```

Which is correct! `app_user` has no DELETE. The permission model worked exactly as designed.

And that's when it hit me: **my test suite deletes rows in teardown, and my test suite has
never failed.**

## The actual bug

My throwaway script loaded `.env`. My test suite loads `.env.test`. At some point —
probably the first time a teardown fixture needed to clean up a table — I pointed
`.env.test`'s `DATABASE_URL` at the `postgres` superuser, because that made the cleanup
work.

So:

| | Connects as | Can DELETE | Can UPDATE `audit_log` |
| :--- | :--- | :--- | :--- |
| Production worker | `app_user` | no | no |
| Test suite | `postgres` | yes | yes |

Every test I'd written ran with privileges my application will never have. The append-only
audit log — the control I was proudest of — is not covered by a single test. Neither is any
other grant. If I wrote a handler that deleted a job row, or "corrected" an audit entry,
the test suite would go green and the worker would throw `InsufficientPrivilege` in
production.

## Why this one is nastier than a normal gap in coverage

Most missing coverage means a bug can slip through. This is worse in a specific way: the
test environment is **more** privileged than production.

That inverts what a passing test means. Normally a green suite says "the things I checked
work." Here it says "the things I checked work, *given permissions the real system doesn't
have*." No amount of adding tests fixes it, because every new test inherits the same
superuser connection. The blind spot scales with the suite.

It's also self-inflicted in a very ordinary way. Nobody decided to disable the security
model in tests. A fixture needed `DELETE` to tidy up, the superuser credential was sitting
right there, and the fastest thing that made teardown work quietly removed the thing under
test from the test environment. Convenience in a fixture ate a security control.

## The general shape

The lesson generalizes past Postgres grants. Any control enforced *outside* your
application code is only real if your tests run under the same identity production does:

- Postgres roles and grants
- row-level security policies
- S3 bucket policies and IAM roles
- Kubernetes RBAC
- database triggers that reject writes

All of these are invisible to a test suite running as an administrator. And the admin
credential is almost always the path of least resistance when you're setting up fixtures,
because setup and teardown are exactly the operations that need elevated rights.

If you enforce something in the database, test as the role that will hit it.

## What I'm doing about it

Two options, and they're not exclusive:

**Run the tests as the application role, clean up out-of-band.** The fixtures get a second,
privileged connection used *only* for setup and teardown; everything the tests actually
exercise goes through an `app_user` session. This is the honest version — every existing
test immediately starts validating the grant model for free.

**Write explicit privilege assertions.** A handful of tests that assert the control exists,
which also document it:

```python
def test_audit_log_rejects_updates(app_user_session):
    with pytest.raises(ProgrammingError, match="permission denied"):
        app_user_session.execute(
            text("UPDATE audit_log SET actor = 'tampered'")
        )
```

That second one is cheap and worth doing regardless. But on its own it's a patch — it tests
the controls I remember to think about. The first option is the fix, because it makes the
default path correct and stops the suite from quietly drifting back.

## The part I keep thinking about

I didn't find this by reviewing anything. I found it because a cleanup script I wrote in two
minutes happened to load a different `.env` file than pytest does, and crashed.

The uncomfortable question isn't "how did this bug get in" — it got in the obvious way, one
convenient shortcut at a time. It's: what else is only true in my test environment?

---
---

# LinkedIn post — "25 passed locally. 13 errors in CI."

*(Ready to paste. About 270 words. Plain text, no markdown, because LinkedIn doesn't render it.)*

```
25 tests passed on my machine. In CI, 13 of them errored before running a single line of test code.

The error: relation "clients" does not exist.

I'd just pushed a background job queue for a side project. Locally everything was green. In GitHub Actions, every test that touched a table fell over at setup.

The cause was embarrassingly simple. My CI started a fresh Postgres, then ran pytest. It never ran my database migrations. Locally my schema had existed for weeks, so I never noticed.

Why had this stayed hidden? My only database test up to then was SELECT 1. It needs no tables. The first test that needed a real schema was the first test to expose the gap.

The obvious fix was one line: add "alembic upgrade head" to the workflow. That failed too. I'd gitignored my alembic.ini weeks earlier because it had a hardcoded password in it. So CI had no migration config at all.

The real fix was to remove the reason it was ignored, not to work around it: commit the file with the credential blanked, and have the code read the database URL from settings instead.

Two lessons:

1. A green test suite only means something if it starts from the same blank state production does. Mine quietly inherited a schema I'd built by hand.

2. I found a second bug the same week with the same shape. My tests ran as a database superuser while the app ran as a restricted role, so none of my permission rules were ever tested.

Both times the test environment was supplying something production wouldn't.

What's in your test environment that production doesn't have?

#softwareengineering #postgres #python #testing #cicd
```

**Notes before posting:**

- The "13 errors" figure is the count of `ERROR at setup` lines in the CI log you pasted: 4 handler tests plus 9 queue tests (the 3 backoff cases are counted separately). The other 12 tests passed in CI because they never touch a table.
- I have not seen the CI run go green after the fix. Don't post "and now it passes" until it does; the draft deliberately stops at the lesson.
- The `app_user` / superuser story is the first post in this file. If you publish both, post that one first and let this one reference it.
