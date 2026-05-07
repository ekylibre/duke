from __future__ import annotations

import os

# Provide required env vars before duke.config is imported anywhere in the test session.
# Real DSNs are not used by unit tests; integration tests override pools/clients explicitly.
os.environ.setdefault("EKYLIBRE_API_BASE_URL", "http://ekylibre.test")
os.environ.setdefault(
    "EKYLIBRE_DB_DSN", "postgresql://duke_reader:test@localhost:5432/ekylibre_test"
)
os.environ.setdefault("DUKE_DB_DSN", "postgresql+asyncpg://duke:test@localhost:5432/duke_test")
os.environ.setdefault("ALLOWED_WS_ORIGINS", "")

from duke.config import get_settings

# Reset cached settings if any prior import populated it before our env vars.
get_settings.cache_clear()
