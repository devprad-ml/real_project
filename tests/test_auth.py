"""API-key auth and role checks (app.deps), and the key CLI (app.keys)."""

import uuid

import pytest
from sqlalchemy import select

from app import keys
from app.db import SessionLocal
from app.deps import hash_key
from app.enums import Role
from app.models import ApiKey

# /validation needs REVIEWER; /search needs any staff member.
REVIEWER_ONLY = "/validation"


def test_a_reviewer_key_gets_in(client, staff):
    assert client.get(REVIEWER_ONLY, headers=staff(Role.REVIEWER)).status_code == 200


def test_admin_passes_every_role_check(client, staff):
    assert client.get(REVIEWER_ONLY, headers=staff(Role.ADMIN)).status_code == 200


def test_a_staff_member_without_the_role_is_a_403(client, staff):
    assert client.get(REVIEWER_ONLY, headers=staff()).status_code == 403


@pytest.mark.parametrize(
    "authorization",
    [
        None,  # no header at all
        "",
        "Bearer",  # scheme, no key
        "Bearer dk_not-a-real-key",
        "Basic dXNlcjpwYXNz",
    ],
)
def test_anything_but_a_live_key_is_a_401(client, tenant, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    r = client.get(REVIEWER_ONLY, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_a_revoked_key_is_a_401(client, tenant):
    key_id, key = keys.create(tenant, "leaver", [Role.REVIEWER])
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get(REVIEWER_ONLY, headers=headers).status_code == 200

    keys.revoke(key_id)

    assert client.get(REVIEWER_ONLY, headers=headers).status_code == 401


def test_only_the_hash_is_stored(tenant):
    key_id, key = keys.create(tenant, "someone", [])
    with SessionLocal() as s:
        row = s.get(ApiKey, key_id)
        assert row.hash == hash_key(key)
        assert key not in row.hash
        assert s.scalar(select(ApiKey).where(ApiKey.hash == key)) is None


def test_create_refuses_an_unknown_tenant():
    with pytest.raises(SystemExit):
        keys.create(uuid.uuid4(), "nobody", [])
