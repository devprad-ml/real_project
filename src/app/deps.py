''' Who is calling, and may they. Every staff route depends on `require_role`.

API keys only: `Authorization: Bearer dk_...`. JWT is deliberately absent -- see m8.md
§1. `ponytail:` add an IdP JWT branch to `current_principal` when SSO exists.
'''

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.enums import Role
from app.models.principal import ApiKey, Principal, RoleGrant


def new_key() -> str:
    return "dk_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    # sha256, not bcrypt: slow hashing defends guessable passwords, and 256 random
    # bits are not guessable.
    return hashlib.sha256(key.encode()).hexdigest()


def current_principal(
    authorization: str = Header(""), db: Session = Depends(get_session)
) -> Principal:
    scheme, _, key = authorization.partition(" ")
    principal = None
    if scheme.lower() == "bearer" and key:
        principal = db.scalar(
            select(Principal)
            .join(ApiKey, ApiKey.principal_id == Principal.id)
            .where(ApiKey.hash == hash_key(key), ApiKey.revoked_at.is_(None))
        )
    if principal is None:
        # One answer for missing, malformed, unknown and revoked: which one it was
        # is information only an attacker needs.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_role(*roles: Role):
    ''' No roles = any authenticated staff member. ADMIN passes every check. '''

    def dep(
        principal: Principal = Depends(current_principal),
        db: Session = Depends(get_session),
    ) -> Principal:
        if roles:
            held = set(
                db.scalars(
                    select(RoleGrant.role).where(RoleGrant.principal_id == principal.id)
                )
            )
            if held.isdisjoint(roles) and Role.ADMIN not in held:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail="missing role")
        return principal

    return dep


def actor(principal: Principal) -> str:
    ''' audit_log.actor for a staff action. Workers write "system". '''
    return f"user:{principal.id}"
