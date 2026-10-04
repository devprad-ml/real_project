"""The database-enforced privilege model.

Every other test in this suite connects as the owner, because teardown needs DELETE.
That makes the test environment *more* privileged than production, so none of these
grants are exercised anywhere else: a handler that deleted a row would go green in CI
and raise InsufficientPrivilege in the worker.

These tests connect as `app_user` -- the role the API and worker actually run as --
and assert the controls still exist. They are the only coverage the grant migrations
(1513a549af5f, 3f4b8b1e99a0, 7c1d4e2fa903) have.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.db import SessionLocal
from app.enums import AuditAction, JobKind, JobState
from app.jobs.queue import enqueue
from app.models import Document, Job, Log


def test_connected_as_the_application_role(app_user_session):
    """Guard for the other tests here: if this ever reports `postgres`, every
    assertion below passes vacuously."""
    assert app_user_session.execute(text("SELECT current_user")).scalar() == "app_user"


def test_audit_log_rejects_updates(app_user_session, tenant, make_doc):
    """Append-only, enforced by REVOKE in 1513a549af5f. Audit history that the
    application can rewrite is not audit history."""
    doc = make_doc()
    with SessionLocal() as s:  # seed as owner
        s.add(
            Log(
                tenant_id=tenant,
                document_id=doc.id,
                actor="system",
                action=AuditAction.STATE_CHANGE,
                detail={"to": "RECEIVED"},
            )
        )
        s.commit()

    with pytest.raises(ProgrammingError, match="permission denied"):
        app_user_session.execute(
            text("UPDATE audit_log SET actor = 'tampered' WHERE tenant_id = :t"),
            {"t": tenant},
        )


def test_audit_log_rejects_deletes(app_user_session, tenant):
    with pytest.raises(ProgrammingError, match="permission denied"):
        app_user_session.execute(
            text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": tenant}
        )


def test_jobs_reject_deletes(app_user_session, tenant):
    """Jobs are history: attempts, last_error and timings are the only record of what
    went wrong. Pruning is a retention job, not an application privilege."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        app_user_session.execute(
            text("DELETE FROM jobs WHERE tenant_id = :t"), {"t": tenant}
        )


def test_documents_reject_deletes(app_user_session, tenant):
    with pytest.raises(ProgrammingError, match="permission denied"):
        app_user_session.execute(
            text("DELETE FROM documents WHERE tenant_id = :t"), {"t": tenant}
        )


def test_app_user_can_do_what_the_worker_needs(app_user_session, tenant, make_doc):
    """The mirror image: the grants must not be so tight that the pipeline cannot
    run. This is the write set of one normalize transition."""
    doc = make_doc()

    fresh = app_user_session.get(Document, doc.id)
    fresh.page_count = 3  # UPDATE on documents
    enqueue(  # INSERT on jobs
        app_user_session, tenant_id=tenant, document_id=doc.id, kind=JobKind.OCR
    )
    app_user_session.add(  # INSERT on audit_log
        Log(
            tenant_id=tenant,
            document_id=doc.id,
            actor="system",
            action=AuditAction.STATE_CHANGE,
            detail={"to": "NORMALIZED"},
        )
    )
    app_user_session.commit()

    with SessionLocal() as s:
        assert s.get(Document, doc.id).page_count == 3


def test_app_user_can_claim_and_update_a_job(app_user_session, tenant, make_doc):
    """claim_one takes FOR UPDATE and the worker then writes state/locked_at, so
    SELECT alone is not enough."""
    doc = make_doc()
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        s.commit()
        job_id = job.id

    claimed = app_user_session.get(Job, job_id, with_for_update=True)
    claimed.state = JobState.RUNNING
    app_user_session.commit()

    with SessionLocal() as s:
        assert s.get(Job, job_id).state == JobState.RUNNING


def test_app_user_cannot_create_tables(app_user_session):
    """app_user got USAGE on `public`, never CREATE. Schema changes belong to
    migrations running as the owner -- this is what makes ALEMBIC_DATABASE_URL a
    separate credential rather than a convenience."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        app_user_session.execute(text("CREATE TABLE public.sneaky (id uuid)"))
