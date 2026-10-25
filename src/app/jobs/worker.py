''' The worker process: claim -> dispatch -> commit, forever.

Run with `python -m app.jobs.worker`. Same codebase as the API, different entrypoint.
The API never does slow work; everything expensive happens here. Scale by running more
copies -- SKIP LOCKED makes that safe with no coordination between them.
'''

import datetime
import signal
import threading
import time
import uuid

from sqlalchemy import update
from sqlalchemy.exc import InterfaceError, OperationalError

from app.config import get_settings
from app.db import SessionLocal
from app.enums import JobState
from app.errors import PermanentError
from app.ingestion.email_poller import poll_once
from app.jobs.handlers import HANDLERS
from app.jobs.queue import _utcnow, claim_one, fail_with_backoff, sweep_stale
from app.models.document import Document
from app.models.job import Job
from app.observability.logging import configure, log
from app.processing import llm

IDLE_SLEEP_SECONDS = 1
# Longest wait between retries while the database is unreachable.
DB_BACKOFF_CAP_SECONDS = 30

_stop = False


def _request_stop(signum, frame) -> None:
    # Only sets a flag. The loop finishes the job it is holding before exiting, so a
    # deploy does not orphan a RUNNING row and wait out the whole lease.
    global _stop
    _stop = True
    log.info("shutdown requested, draining current job")


class Housekeeper(threading.Thread):
    ''' Keeps the held job's lock fresh and sweeps dead peers' jobs, from a thread of
    its own so a handler that blocks for minutes (OCR) cannot starve either.

    This is what lets the lease be 30 s instead of "longer than the slowest handler":
    a live worker proves it every heartbeat, and a silent one is reclaimed by anyone.
    Its sessions are its own; a SQLAlchemy session is not thread-safe. '''

    TICK = 1.0

    def __init__(self) -> None:
        super().__init__(daemon=True, name="housekeeping")
        self._current: uuid.UUID | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()

    def hold(self, job_id: uuid.UUID | None) -> None:
        with self._lock:
            self._current = job_id

    def stop(self) -> None:
        self._done.set()

    def run(self) -> None:
        last_beat = time.monotonic()  # claim just set locked_at; no beat is due yet
        last_sweep = float("-inf")  # sweep at startup, like the old loop did
        while not self._done.wait(self.TICK):
            s = get_settings()
            now = time.monotonic()
            if now - last_beat >= s.job_heartbeat_seconds:
                last_beat = now
                self._beat()
            if now - last_sweep >= s.sweep_seconds:
                last_sweep = now
                self._sweep()

    def _beat(self) -> None:
        with self._lock:
            job_id = self._current
        if job_id is None:
            return
        try:
            with SessionLocal() as session:
                session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.state == JobState.RUNNING)
                    .values(locked_at=_utcnow())
                )
                session.commit()
        except Exception as exc:  # a missed beat is survivable; a dead thread is not
            log.warning(
                "heartbeat_failed",
                extra={"job_id": job_id, "error_type": type(exc).__name__},
            )

    def _sweep(self) -> None:
        try:
            with SessionLocal() as session:
                reclaimed = sweep_stale(session)
                session.commit()
            if reclaimed:
                log.warning(f"swept {reclaimed} stale job(s) back to pending")
        except Exception as exc:
            log.warning("sweep_failed", extra={"error_type": type(exc).__name__})


# Set by run_forever. None when run_once is driven directly (tests), where there is no
# thread to tell.
housekeeper: Housekeeper | None = None


def _hold(job_id: uuid.UUID | None) -> None:
    if housekeeper is not None:
        housekeeper.hold(job_id)


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

    _hold(job_id)
    started = time.perf_counter()
    try:
        _run_claimed(job_id, started)
    finally:
        _hold(None)
    return True


def _run_claimed(job_id: uuid.UUID, started: float) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        doc = session.get(Document, job.document_id)
        # Read before the try: rollback() expires the instance, so job.kind in the
        # except block would re-query (or fail) instead of telling us what broke.
        job_kind = job.kind
        ids = {
            "job_id": job_id,
            "kind": job_kind.value,
            "tenant_id": job.tenant_id,
            "document_id": job.document_id,
        }
        scope = llm.scope.set((job.tenant_id, job.document_id))
        try:
            handler = HANDLERS.get(job_kind)
            if handler is None:
                raise PermanentError(f"no handler for kind {job_kind!r}")
            handler(session, doc)
            job.state = JobState.DONE
            job.locked_at = None
            session.commit()
            log.info(
                "job_done",
                extra={**ids, "duration_ms": int((time.perf_counter() - started) * 1000)},
            )
        except Exception as exc:
            # Roll back the handler's partial work first: the failure bookkeeping must
            # not ride along with whatever half-finished writes caused the failure.
            session.rollback()
            permanent = isinstance(exc, PermanentError)
            with SessionLocal() as fail_session:
                failed = fail_session.get(Job, job_id)
                fail_with_backoff(fail_session, failed, repr(exc), permanent=permanent)
                fail_session.commit()
                attempts = failed.attempts
            log.warning(
                "job_failed_permanent" if permanent else "job_failed",
                extra={
                    **ids,
                    "attempts": attempts,
                    "error_type": type(exc).__name__,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
        finally:
            llm.scope.reset(scope)


def _poll_mailbox() -> None:
    try:
        new = poll_once(SessionLocal)
        if new:
            log.info(f"intake: {new} new document(s)")
    except Exception as exc:  # a mailbox outage must not stop job processing
        log.warning("intake_poll_failed", extra={"error_type": type(exc).__name__})


def run_forever() -> None:
    global housekeeper
    configure()
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    settings = get_settings()
    housekeeper = Housekeeper()
    housekeeper.start()

    last_poll = 0.0
    db_backoff = 1.0
    while not _stop:
        try:
            # ponytail: the poller is a tick in this loop, so a long OCR job delays the
            # next poll. Split it into its own process if the queue is ever saturated.
            due = time.monotonic() - last_poll > settings.imap_poll_seconds
            if settings.imap_host and due:
                _poll_mailbox()
                last_poll = time.monotonic()

            if not run_once():
                time.sleep(IDLE_SLEEP_SECONDS)
            db_backoff = 1.0
        except (OperationalError, InterfaceError) as exc:
            # Postgres restarting or the network blipping: wait it out. The job this
            # worker held, if any, stays RUNNING and is reclaimed once its lock goes
            # stale -- the same path as a kill.
            log.error("worker_db_unavailable", extra={"error_type": type(exc).__name__})
            time.sleep(db_backoff)
            db_backoff = min(db_backoff * 2, DB_BACKOFF_CAP_SECONDS)
        except Exception as exc:
            # A bug in the bookkeeping, not in a handler (handler errors are caught in
            # run_once). One poison row must never take the worker down with it.
            log.error("worker_loop_error", extra={"error_type": type(exc).__name__})
            time.sleep(IDLE_SLEEP_SECONDS)

    housekeeper.stop()
    log.info("worker stopped")


if __name__ == "__main__":
    run_forever()
