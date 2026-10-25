''' Import this first in every bench script.

The benchmarks create tenants and documents, which `app_user` cannot (it has no INSERT
on `clients`), so they run as the owner role. The workers measured here therefore hold
more privilege than production workers; that does not change throughput, and the
privilege model has its own tests (tests/test_privileges.py).
'''

import os
from pathlib import Path

from dotenv import dotenv_values

_owner = dotenv_values(Path(__file__).resolve().parent.parent / ".env").get(
    "ALEMBIC_DATABASE_URL"
)
if _owner:
    os.environ["DATABASE_URL"] = _owner
# Benchmarks load the embedder themselves, and time it separately.
os.environ.setdefault("WARM_EMBEDDER", "false")
# Windows resolves "localhost" to ::1 first, and the mock listens on IPv4 only: every
# push then waits ~2 s for the IPv6 attempt to fail (measured, m13.md §8). That is a dev
# host artifact, not the pipeline, so the benches use the literal address.
if "//localhost" in os.environ.get("DOWNSTREAM_BASE_URL", ""):
    os.environ["DOWNSTREAM_BASE_URL"] = os.environ["DOWNSTREAM_BASE_URL"].replace(
        "//localhost", "//127.0.0.1"
    )
# Per-job INFO lines would bury the results table.
os.environ.setdefault("LOG_LEVEL", "WARNING")
