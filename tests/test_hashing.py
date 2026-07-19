from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from xeval.hashing import canonical_json, content_hash, short_hash, text_hash


@dataclass(frozen=True)
class _Fixture:
    name: str
    path: Path
    labels: set[str]


def test_canonical_json_normalises_order_paths_dataclasses_and_collections() -> None:
    first = {
        "z": (3, 2, 1),
        "fixture": _Fixture("demo", Path("catalog/probe.yaml"), {"beta", "alpha"}),
        "nested": {"b": 2, "a": 1},
    }
    second = {
        "nested": {"a": 1, "b": 2},
        "fixture": {
            "labels": ["alpha", "beta"],
            "path": "catalog/probe.yaml",
            "name": "demo",
        },
        "z": [3, 2, 1],
    }

    assert canonical_json(first) == canonical_json(second)
    assert content_hash(first) == content_hash(second)


def test_hash_helpers_are_deterministic_and_have_expected_lengths() -> None:
    value = {"probe": "gold-001", "version": 1}

    assert content_hash(value) == content_hash(value)
    assert len(content_hash(value)) == 64
    assert short_hash(value) == content_hash(value)[:12]
    assert short_hash(value, length=7) == content_hash(value)[:7]
    assert text_hash("café") == text_hash("café")
    assert text_hash("café") != text_hash("cafe")


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_json({"score": float("nan")})
