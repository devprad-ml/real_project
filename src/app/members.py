''' Load a tenant's Master Patient Index from CSV.

    python -m app.members load --tenant <uuid> members.csv

Columns: member_id, first_name, last_name, dob (YYYY-MM-DD, may be blank). Upserts on
(tenant, member_id), so re-loading a fresh export updates instead of duplicating.
`ponytail:` a CSV standing in for the enterprise MPI; see `processing/mpi.lookup`.
'''

import argparse
import csv
import datetime
import uuid
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert

from app.db import SessionLocal
from app.models import Client, Member
from app.processing.mpi import normalize_id


def _rows(tenant: uuid.UUID, path: Path) -> list[dict]:
    rows: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for line, r in enumerate(csv.DictReader(f), start=2):
            member_id = normalize_id(r.get("member_id") or "")
            if not member_id:
                raise SystemExit(f"{path}:{line}: member_id is blank")
            try:
                dob = datetime.date.fromisoformat(r["dob"]) if r.get("dob") else None
            except ValueError:
                raise SystemExit(f"{path}:{line}: dob is not YYYY-MM-DD") from None
            # Keyed on the normalized id: a repeat in one file is the same member
            # (last row wins), and one INSERT cannot upsert the same key twice.
            rows[member_id] = {
                "id": uuid.uuid4(),
                "tenant_id": tenant,
                "member_id": member_id,
                "first_name": r.get("first_name") or None,
                "last_name": r.get("last_name") or None,
                "dob": dob,
            }
    return list(rows.values())


def load(tenant: uuid.UUID, path: Path) -> int:
    rows = _rows(tenant, path)
    with SessionLocal() as s:
        if s.get(Client, tenant) is None:
            raise SystemExit(f"no such tenant: {tenant}")
        if rows:
            stmt = insert(Member).values(rows)
            s.execute(
                stmt.on_conflict_do_update(
                    constraint="unique_tenant_member",
                    set_={
                        c: stmt.excluded[c] for c in ("first_name", "last_name", "dob")
                    },
                )
            )
        s.commit()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.members")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ld = sub.add_parser("load")
    ld.add_argument("--tenant", type=uuid.UUID, required=True)
    ld.add_argument("csv", type=Path)
    args = parser.parse_args()
    print(f"loaded {load(args.tenant, args.csv)} member(s)")


if __name__ == "__main__":
    main()
