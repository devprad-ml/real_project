''' Staff API keys from the command line. The key is printed once and never stored.

    python -m app.keys create --tenant <uuid> --name "Jane Doe" --role reviewer
    python -m app.keys revoke --key-id <uuid>

`ponytail:` no /admin/keys endpoints; this covers a handful of staff.
'''

import argparse
import uuid

from sqlalchemy import func

from app.db import SessionLocal
from app.deps import hash_key, new_key
from app.enums import Role
from app.models import ApiKey, Client, Principal, RoleGrant


def create(tenant: uuid.UUID, name: str, roles: list[Role]) -> tuple[uuid.UUID, str]:
    ''' New principal + grants + key. Returns (key id, key). '''
    key = new_key()
    with SessionLocal() as s:
        if s.get(Client, tenant) is None:
            raise SystemExit(f"no such tenant: {tenant}")
        p = Principal(tenant_id=tenant, display_name=name)
        s.add(p)
        s.flush()
        s.add_all(RoleGrant(principal_id=p.id, role=r) for r in roles)
        row = ApiKey(principal_id=p.id, hash=hash_key(key))
        s.add(row)
        s.commit()
        return row.id, key


def revoke(key_id: uuid.UUID) -> None:
    with SessionLocal() as s:
        row = s.get(ApiKey, key_id)
        if row is None:
            raise SystemExit(f"no such key: {key_id}")
        row.revoked_at = func.now()
        s.commit()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.keys")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--tenant", type=uuid.UUID, required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--role", type=Role, action="append", default=[])
    r = sub.add_parser("revoke")
    r.add_argument("--key-id", type=uuid.UUID, required=True)
    args = parser.parse_args()

    if args.cmd == "create":
        key_id, key = create(args.tenant, args.name, args.role)
        print(f"key id: {key_id}\nkey:    {key}\n(shown once -- store it now)")
    else:
        revoke(args.key_id)
        print("revoked")


if __name__ == "__main__":
    main()
