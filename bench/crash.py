''' Real-kill crash tests. See m13.md §2.3.

    python -m bench.crash            # all four
    python -m bench.crash 1 3        # some

Each scenario starts real worker *processes* and ends one with Process.kill()
(TerminateProcess on Windows, SIGKILL elsewhere): no cleanup, no `finally`. Needs
Postgres and qdrant up; scenario 4 also needs the `docker` CLI (container postgres_db).
The mock downstream is started if nothing answers at DOWNSTREAM_BASE_URL.
'''

import bench.env  # noqa: F401  (must be first)

import multiprocessing as mp
import os
import subprocess
import sys
import time

from sqlalchemy import create_engine, func, select, text

from app.config import get_settings
from app.db import SessionLocal
from app.enums import (
    DocumentStatus, DocumentType, JobKind, JobState, RouteQueue,
)
from app.jobs.queue import _utcnow
from app.models import Document, Job, Log
from app.storage import get_vector_store
from app.storage.vectors import tenant_collection
from bench import canned, seed

PG_CONTAINER = "postgres_db"
CTX = mp.get_context("spawn")
PAGES = [[
    "CMS-1500 Health Insurance Claim Form",
    "Claim number: C-1. Member ID: M-0001. Date of service 2026-08-03.",
    "CPT 72148 MRI lumbar spine without contrast. Billed amount $1,250.00.",
], ["Diagnosis codes: M54.50 low back pain. Referring provider Dr. A. Patel."]]


# --- worker processes (module-level so `spawn` can import them) -------------------

def worker_normal() -> None:
    canned.patch_claude()
    from app.jobs import worker

    worker.run_forever()


def worker_hangs_in_classify() -> None:
    ''' Claims a classify job and never finishes it. Its heartbeat thread keeps running,
    which is exactly a healthy worker deep in a long OCR. '''
    canned.patch_claude()
    from app.processing import classify

    classify.run = lambda pages: time.sleep(3600)
    from app.jobs import worker

    worker.run_forever()


def worker_dies_after_push() -> None:
    ''' The crash window of handle_push: downstream accepted, our commit never ran. '''
    canned.patch_claude()
    from app.integrations import downstream

    real = downstream.push

    def push_then_die(*a, **kw):
        real(*a, **kw)
        os._exit(1)

    downstream.push = push_then_die
    from app.jobs import worker

    worker.run_forever()


def worker_dies_after_index() -> None:
    ''' The crash window of handle_embed: vectors written, status not yet flipped. '''
    canned.patch_claude()
    from app.jobs import handlers

    real = handlers.index_document

    def index_then_die(doc):
        real(doc)
        os._exit(1)

    handlers.index_document = index_then_die
    from app.jobs import worker

    worker.run_forever()


def worker_slow_classify() -> None:
    ''' Classify takes 12 s: long enough to pull the database out from under it. '''
    canned.patch_claude()
    from app.processing import classify

    fast = classify.run

    def slow(pages):
        time.sleep(12)
        return fast(pages)

    classify.run = slow
    from app.jobs import worker

    worker.run_forever()


# --- helpers ----------------------------------------------------------------------

def start(target) -> mp.Process:
    p = CTX.Process(target=target, daemon=True)
    p.start()
    return p


def running(doc_id) -> bool:
    return job_of(doc_id, JobKind.CLASSIFY).state == JobState.RUNNING


def wait_for(pred, timeout: float, every: float = 0.25) -> float | None:
    ''' Seconds until pred() is true, or None on timeout. A predicate that raises
    (the database is down) counts as false: that is the point of scenario 4. '''
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if pred():
                return time.monotonic() - t0
        except Exception:
            pass
        time.sleep(every)
    return None


def doc_status(doc_id) -> DocumentStatus:
    with SessionLocal() as s:
        return s.get(Document, doc_id).status


def job_of(doc_id, kind) -> Job:
    with SessionLocal() as s:
        return s.scalars(
            select(Job).where(Job.document_id == doc_id, Job.kind == kind)
        ).one()


def pipeline_doc(status, *, doc_type, job, route_queue=None):
    with SessionLocal() as s:
        tenant = seed.new_tenant(s, "crash")
        seed.add_member(s, tenant, canned.MEMBER)
        doc = seed.add_doc(
            s, tenant, status=status, doc_type=doc_type, pages=PAGES,
            filename="claim.pdf", job=job, route_queue=route_queue,
        )
        s.commit()
        return tenant, doc.id


def kill_all(*procs) -> None:
    for p in procs:
        if p is not None and p.is_alive():
            p.kill()
            p.join(5)


# --- scenarios --------------------------------------------------------------------

def scenario_1() -> tuple[bool, str]:
    ''' Kill a worker mid-handler: reclaimed within DESIGN's 60 s, and a *live* worker
    holding a job longer than the lease is not robbed of it. '''
    lease = get_settings().job_lease_seconds
    tenant, doc = pipeline_doc(
        DocumentStatus.NORMALIZED, doc_type=DocumentType.UNKNOWN, job=JobKind.CLASSIFY
    )
    a = b = None
    try:
        a = start(worker_hangs_in_classify)
        if wait_for(lambda: running(doc), 120) is None:
            return False, "worker A never claimed the job"
        b = start(worker_normal)  # idle; its housekeeper is the one that must sweep
        time.sleep(lease + 8)  # past the lease: only the heartbeat holds the job
        job = job_of(doc, JobKind.CLASSIFY)
        age = (_utcnow() - job.locked_at).total_seconds() if job.locked_at else None
        if job.state != JobState.RUNNING or age is None or age > 15:
            return False, f"live job lost: state={job.state.value} lock age={age}"

        a.kill()
        a.join(5)
        t_kill = time.monotonic()
        done = wait_for(lambda: job_of(doc, JobKind.CLASSIFY).state == JobState.DONE, 90)
        if done is None:
            return False, "job not reclaimed within 90 s of the kill"
        total = time.monotonic() - t_kill
        ok = total <= 60
        return ok, f"held {lease + 8:.0f}s past a {lease}s lease by heartbeat; " \
                   f"reclaimed and finished {total:.1f}s after kill (budget 60s)"
    finally:
        kill_all(a, b)
        seed.cleanup(tenant)


def scenario_2() -> tuple[bool, str]:
    ''' Crash after the downstream accepted the push, before our commit. '''
    mock = canned.ensure_mock_downstream()
    tenant, doc = pipeline_doc(
        DocumentStatus.ROUTED, doc_type=DocumentType.CLAIMS, job=JobKind.PUSH,
        route_queue=RouteQueue.CLAIMS_OPS,
    )
    a = b = None
    try:
        a = start(worker_dies_after_push)
        a.join(180)
        if a.exitcode != 1:
            return False, f"worker A did not die at the crash point (exit {a.exitcode})"
        b = start(worker_normal)
        t = wait_for(lambda: doc_status(doc) == DocumentStatus.PUSHED, 120)
        if t is None:
            return False, "document never reached PUSHED after the crash"
        mine = [k for k in canned.received_keys() if k.startswith(f"{doc}:")]
        with SessionLocal() as s:
            pushed_rows = s.scalar(
                select(func.count()).where(
                    Log.document_id == doc, Log.detail["to"].astext == "PUSHED"
                )
            )
        ok = len(mine) >= 2 and len(set(mine)) == 1 and pushed_rows == 1
        return ok, (f"downstream saw {len(mine)} requests, {len(set(mine))} distinct "
                    f"key(s); {pushed_rows} PUSHED audit row(s); "
                    f"PUSHED {t:.1f}s after restart")
    finally:
        kill_all(a, b)
        seed.cleanup(tenant)
        if mock:
            mock.terminate()


def scenario_3() -> tuple[bool, str]:
    ''' Crash between the qdrant upsert and the EMBEDDED flip: the retry must overwrite,
    not duplicate. '''
    mock = canned.ensure_mock_downstream()
    tenant, doc = pipeline_doc(
        DocumentStatus.CLASSIFIED, doc_type=DocumentType.CLAIMS, job=JobKind.EMBED
    )
    store = get_vector_store()
    collection = tenant_collection(tenant)

    def count() -> int:
        return store.client.count(collection, exact=True).count
    a = b = None
    try:
        a = start(worker_dies_after_index)
        a.join(180)
        if a.exitcode != 1:
            return False, f"worker A did not die at the crash point (exit {a.exitcode})"
        before = count()
        if doc_status(doc) != DocumentStatus.CLASSIFIED or before == 0:
            return False, f"crash window missed: {doc_status(doc).value}, {before} points"
        b = start(worker_normal)
        if wait_for(lambda: doc_status(doc) == DocumentStatus.PUSHED, 120) is None:
            return False, "document never completed after the crash"
        after = count()
        return after == before, f"{before} points after the crash, {after} after retry"
    finally:
        kill_all(a, b)
        seed.cleanup(tenant)
        if mock:
            mock.terminate()


def _docker(*args) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


def _postgres_back(timeout: float = 90) -> bool:
    url = get_settings().database_url.get_secret_value()

    def ping():
        engine = create_engine(url)
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True
        finally:
            engine.dispose()

    return wait_for(ping, timeout, every=1) is not None


def scenario_4() -> tuple[bool, str]:
    ''' Postgres goes away for 25 s: idle, then in the middle of a job. The worker
    process must outlive both. '''
    notes = []
    tenant, doc = pipeline_doc(
        DocumentStatus.NORMALIZED, doc_type=DocumentType.UNKNOWN, job=JobKind.CLASSIFY
    )
    w = None
    try:
        # Idle: no job in flight when the database disappears.
        with SessionLocal() as s:  # park the seeded job for later; claim nothing yet
            s.execute(text("UPDATE jobs SET run_after = now() + interval '1 hour' "
                           "WHERE document_id = :d"), {"d": doc})
            s.commit()
        w = start(worker_normal)
        time.sleep(6)
        _docker("stop", PG_CONTAINER)
        time.sleep(25)
        _docker("start", PG_CONTAINER)
        if not _postgres_back():
            return False, "postgres did not come back"
        if not w.is_alive():
            return False, "worker died during the idle outage"
        with SessionLocal() as s:
            s.execute(text("UPDATE jobs SET run_after = now() WHERE document_id = :d"),
                      {"d": doc})
            s.commit()
        t = wait_for(lambda: doc_status(doc) != DocumentStatus.NORMALIZED, 60)
        if t is None:
            return False, "worker alive but never resumed after the idle outage"
        notes.append(f"idle outage: alive, resumed in {t:.1f}s")
        kill_all(w)

        # Mid-job: Postgres disappears while classify is running. A fresh document, so
        # nothing left over from the idle phase can answer for it.
        with SessionLocal() as s:
            doc = seed.add_doc(
                s, tenant, status=DocumentStatus.NORMALIZED,
                doc_type=DocumentType.UNKNOWN,
                pages=[PAGES[0], ["a second document, so its hash differs"]],
                filename="claim2.pdf", job=JobKind.CLASSIFY,
            ).id
            s.commit()
        w = start(worker_slow_classify)
        if wait_for(lambda: running(doc), 120) is None:
            return False, "worker never claimed the mid-job job"
        _docker("stop", PG_CONTAINER)
        time.sleep(25)
        _docker("start", PG_CONTAINER)
        if not _postgres_back():
            return False, "postgres did not come back (mid-job)"
        if not w.is_alive():
            return False, "worker died when the database vanished mid-job"
        t = wait_for(lambda: job_of(doc, JobKind.CLASSIFY).state == JobState.DONE, 120)
        if t is None:
            return False, "job never finished after the mid-job outage"
        notes.append(f"mid-job outage: alive, job DONE {t:.1f}s after postgres returned")
        return True, "; ".join(notes)
    finally:
        try:
            _docker("start", PG_CONTAINER)  # never leave the dev database stopped
        except Exception:
            pass
        kill_all(w)
        _postgres_back()
        seed.cleanup(tenant)


SCENARIOS = {
    1: ("kill mid-handler; heartbeat holds a live job", scenario_1),
    2: ("crash after downstream accepted the push", scenario_2),
    3: ("crash after qdrant upsert, before status flip", scenario_3),
    4: ("postgres outage, idle and mid-job", scenario_4),
}


def main() -> None:
    chosen = [int(a) for a in sys.argv[1:]] or list(SCENARIOS)
    failed = 0
    for n in chosen:
        name, fn = SCENARIOS[n]
        print(f"[{n}] {name} ...", flush=True)
        t0 = time.monotonic()
        try:
            ok, detail = fn()
        except Exception as exc:  # a broken harness is a failure, not a pass
            ok, detail = False, f"harness error: {type(exc).__name__}: {exc}"
        failed += not ok
        print(f"    {'PASS' if ok else 'FAIL'} ({time.monotonic() - t0:.0f}s): {detail}",
              flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
