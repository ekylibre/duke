from __future__ import annotations

import pytest

from duke.integration.store.hashing import HASH_LEN, tenant_hash, user_hash


def test_tenant_hash_deterministic() -> None:
    assert tenant_hash("farm_a", secret="s") == tenant_hash("farm_a", secret="s")


def test_tenant_hash_changes_with_secret() -> None:
    assert tenant_hash("farm_a", secret="s1") != tenant_hash("farm_a", secret="s2")


def test_tenant_and_user_hashes_are_different_namespaces() -> None:
    same = "x@example.com"
    assert tenant_hash(same, secret="s") != user_hash(same, secret="s")


def test_user_hash_is_case_insensitive() -> None:
    assert user_hash("Alice@example.com", secret="s") == user_hash("alice@example.com", secret="s")


def test_hash_len() -> None:
    assert len(tenant_hash("t", secret="s")) == HASH_LEN
    assert len(user_hash("u@e.x", secret="s")) == HASH_LEN


def test_secret_required() -> None:
    with pytest.raises(ValueError):
        tenant_hash("t", secret="")
