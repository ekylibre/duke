"""Irreversible identifiers for the Duke audit log.

`tenant_hash` and `user_hash` are stored instead of the raw tenant/email so the
audit log can be queried per tenant/user without keeping personal data in clear.
The HASH_SECRET prevents trivial rainbow-table reversal.
"""

from __future__ import annotations

import hashlib

HASH_LEN = 32  # 128 bits, hex-encoded


def _digest(*parts: str, secret: str) -> str:
    if not secret:
        raise ValueError("HASH_SECRET must be set")
    h = hashlib.sha256()
    h.update(secret.encode("utf-8"))
    for part in parts:
        h.update(b"\x00")
        h.update(part.encode("utf-8"))
    return h.hexdigest()[:HASH_LEN]


def tenant_hash(tenant: str, *, secret: str) -> str:
    return _digest("tenant", tenant, secret=secret)


def user_hash(email: str, *, secret: str) -> str:
    return _digest("user", email.lower().strip(), secret=secret)
