from __future__ import annotations

import hashlib

import pytest

from xeval.storage import (
    LATEST_SCHEMA_VERSION,
    EvaluationStore,
    UnsafePayloadError,
)


def _create_completed_run(
    store: EvaluationStore,
    run_id: str,
    started_at: str,
    *,
    endpoint_version: str = "endpoint-v1",
) -> None:
    store.create_run(
        run_id=run_id,
        endpoint_version=endpoint_version,
        execution_fingerprint="exec-sha",
        suite_hash="suite-sha",
        config_hash="config-sha",
        judge_prompt_hash="judge-sha",
        started_at=started_at,
    )
    store.finish_run(run_id, completed_at=started_at)


def test_migrations_and_in_memory_lifecycle() -> None:
    with EvaluationStore(":memory:") as store:
        assert store.schema_version == LATEST_SCHEMA_VERSION
        run = store.create_run(
            run_id="run-1",
            endpoint_version="v1",
            execution_fingerprint="exec",
        )
        assert store.get_run(run.run_id) == run

        with store.connection() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(probe_results)").fetchall()
            }
            migration_count = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
        assert not {"prompt", "messages", "request_body"} & columns
        assert migration_count == LATEST_SCHEMA_VERSION


def test_file_connections_use_wal_and_busy_timeout(tmp_path) -> None:
    store = EvaluationStore(tmp_path / "history.sqlite3", busy_timeout_ms=3210)
    with store.connection() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 3210


def test_unsafe_content_never_reaches_probe_results() -> None:
    with EvaluationStore(":memory:") as store:
        store.create_run(
            run_id="run-1",
            endpoint_version="v1",
            execution_fingerprint="exec",
        )
        with pytest.raises(UnsafePayloadError):
            store.record_probe_result(
                run_id="run-1",
                probe_hash="probe-sha",
                response_text="unclassified response",
            )
        with pytest.raises(UnsafePayloadError):
            store.record_probe_result(
                run_id="run-1",
                probe_hash="other-sha",
                metadata={"raw_prompt": "sensitive"},
            )
        with pytest.raises(UnsafePayloadError):
            store.finish_run(
                "run-1",
                summary={"response_samples": ["unclassified response"]},
            )
        assert store.get_probe_results("run-1") == []


def test_cache_requires_the_complete_key_and_safe_response_marker() -> None:
    with EvaluationStore(":memory:") as store:
        store.create_run(
            run_id="source",
            endpoint_version="v1",
            execution_fingerprint="exec-a",
            started_at="2026-01-01T00:00:00Z",
        )
        result = store.record_probe_result(
            run_id="source",
            probe_hash="probe-sha",
            passed=True,
            score=0.8,
            response_text="curated safe response",
            response_safe=True,
        )
        store.finish_run("source", completed_at="2026-01-01T00:01:00Z")

        hit = store.lookup_cache(
            probe_hash="probe-sha",
            endpoint_version="v1",
            execution_fingerprint="exec-a",
        )
        assert hit is not None
        assert hit.result == result
        assert hit.result.response_hash == hashlib.sha256(b"curated safe response").hexdigest()
        assert (
            store.lookup_cache(
                probe_hash="probe-sha",
                endpoint_version="v2",
                execution_fingerprint="exec-a",
            )
            is None
        )
        assert (
            store.lookup_cache(
                probe_hash="probe-sha",
                endpoint_version="v1",
                execution_fingerprint="exec-b",
            )
            is None
        )


def test_previous_compatible_run_can_cross_endpoint_versions() -> None:
    with EvaluationStore(":memory:") as store:
        _create_completed_run(store, "prior", "2026-01-01T00:00:00Z")
        store.create_run(
            run_id="current",
            endpoint_version="endpoint-v2",
            execution_fingerprint="exec-sha",
            suite_hash="suite-sha",
            config_hash="config-sha",
            judge_prompt_hash="judge-sha",
            started_at="2026-01-02T00:00:00Z",
        )

        previous = store.get_previous_compatible_run("current")
        assert previous is not None
        assert previous.run_id == "prior"
        assert (
            store.get_previous_compatible_run("current", require_same_endpoint_version=True) is None
        )
