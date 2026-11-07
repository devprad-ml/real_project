''' Import this first in every bench script.

The benchmarks create tenants and documents, which `app_user` cannot (it has no INSERT
on `clients`), so they run as the owner role. That is more privilege than the API has; the
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
# Per-job INFO lines would bury the results table.
os.environ.setdefault("LOG_LEVEL", "WARNING")
