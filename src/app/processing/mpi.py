''' Patient matching: bind a document to a member of the tenant's MPI.

Exact match on the normalized member_id, or a human looks at it. No fuzzy auto-bind:
"one character off" (O/0, I/1) is exactly how a document lands on the wrong
patient's record, and a wrong bind is a PHI incident while a review is seconds of
staff time.
ponytail: if OCR-confusable misses flood the review queue, show the reviewer
near-miss candidates (pg_trgm on members.member_id). Still never auto-bind them.
'''

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.member import Member
from app.processing.confidence import parse_date


def normalize_id(value: str) -> str:
    ''' "abc-123 " and "ABC123" are the same member. '''
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def lookup(session: Session, tenant_id: uuid.UUID, member_id: str) -> Member | None:
    ''' The swap point for a real MPI client: nothing above this knows it is a table. '''
    return session.scalar(
        select(Member).where(
            Member.tenant_id == tenant_id, Member.member_id == normalize_id(member_id)
        )
    )


def mismatches(
    member_id: str | None, dob: str | None, member: Member | None
) -> list[str]:
    ''' Review reasons; empty means bind. `dob` only exists on doc types that carry
    one -- when both sides have it they must agree. '''
    if not member_id:
        return ["member_id_missing"]
    if member is None:
        return ["member_not_found"]
    if dob and member.dob and parse_date(dob) != member.dob:
        return ["dob_mismatch"]
    return []
