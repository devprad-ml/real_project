''' The worker process: claim -> dispatch -> commit, forever.

Run with `python -m app.jobs.worker`. Same codebase as the API, different entrypoint.
The API never does slow work; everything expensive happens here. Scale by running more
copies -- SKIP LOCKED makes that safe with no coordination between them.
'''

import datetime
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.enums import JobState
from app.errors import PermanentError
from app.ingestion.email_poller import poll_once
from app.jobs.handlers import HANDLERS
from app.jobs.queue import claim_one, fail_with_backoff, sweep_stale
from app.models.document import Document
from app.models.job import Job

IDLE_SLEEP_SECONDS = 1
SWEEP_EVERY_SECONDS = 60

_stop = False


def _request_stop(signum, frame) -> None:
    # Only sets a flag. The loop finishes the job it is holding before exiting, so a
    # deploy does not orphan a RUNNING row and wait out the whole lease.
    global _stop
    _stop = True
    print("shutdown requested, draining current job")


def run_once() -> bool:
    ''' Claim and run at most one job. Returns False when the queue had nothing due.

    Two transactions, deliberately. The claim commits immediately so the row is marked
    RUNNING and released -- holding the row lock for the whole handler would make a
    30-second OCR block every other worker's claim scan.
    '''
    with SessionLocal() as session:
        job = claim_one(session)
        if job is None:
            session.commit()
            return False
        job.state = JobState.RUNNING
        job.locked_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        session.commit()
        job_id = job.id

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        doc = session.get(Document, job.document_id)
        # Read before the try: rollback() expires the instance, so job.kind in the
        # except block would re-query (or fail) instead of telling us what broke.
        job_kind = job.kind
        try:
            handler = HANDLERS.get(job_kind)
            if handler is None:
                raise PermanentError(f"no handler for kind {job_kind!r}")
            handler(session, doc)
            job.state = JobState.DONE
            job.locked_at = None
            session.commit()
        except Exception as exc:
            # Roll back the handler's partial work first: the failure bookkeeping must
            # not ride along with whatever half-finished writes caused the failure.
            session.rollback()
            with SessionLocal() as fail_session:
                fail_with_backoff(
                    fail_session,
                    fail_session.get(Job, job_id),
                    repr(exc),
                    permanent=isinstance(exc, PermanentError),
                )
                fail_session.commit()
            print(f"job {job_id} ({job_kind}) failed: {exc!r}")
    return True


def run_forever() -> None:
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    settings = get_settings()
    last_sweep = 0.0
    last_poll = 0.0
    while not _stop:
        if time.monotonic() - last_sweep > SWEEP_EVERY_SECONDS:
            with SessionLocal() as session:
                reclaimed = sweep_stale(session)
                session.commit()
            if reclaimed:
                print(f"swept {reclaimed} stale job(s) back to pending")
            last_sweep = time.monotonic()

        # ponytail: the poller is a tick in this loop, so a long OCR job delays the next
        # poll. Split it into its own process if the queue is ever saturated.
        if settings.imap_host and time.monotonic() - last_poll > settings.imap_poll_seconds:
            try:
                new = poll_once(SessionLocal)
                if new:
                    print(f"intake: {new} new document(s)")
            except Exception as exc:  # a mailbox outage must not stop job processing
                print(f"intake poll failed: {exc!r}")
            last_poll = time.monotonic()

        if not run_once():
            time.sleep(IDLE_SLEEP_SECONDS)

    print("worker stopped")


if __name__ == "__main__":
    run_forever()
