"""SQLite persistence for reproducible black-box evaluation runs.

The schema intentionally has no prompt/message/request columns.  Probe identity is
represented only by a caller-computed hash, and response bodies are accepted only
when the caller explicitly labels them safe or redacted.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

LATEST_SCHEMA_VERSION = 3
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class StorageError(RuntimeError):
    """Base class for storage-specific failures."""


class UnsafePayloadError(StorageError, ValueError):
    """Raised before content that has not been approved for storage reaches SQLite."""


class SchemaVersionError(StorageError):
    """Raised when the database was created by a newer, incompatible harness."""


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    endpoint_version: str
    execution_fingerprint: str
    status: str
    started_at: str
    completed_at: str | None = None
    run_hash: str = ""
    suite_hash: str = ""
    config_hash: str = ""
    judge_prompt_hash: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    summary: Mapping[str, Any] = field(default_factory=dict)

    @property
    def probe_set_hash(self) -> str:
        return self.suite_hash

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProbeResultRecord:
    result_id: int
    run_id: str
    probe_hash: str
    category: str
    status: str
    passed: bool | None
    score: float | None
    latency_ms: float | None
    error_code: str | None
    response_text: str | None
    response_hash: str | None
    response_disposition: str
    metrics: Mapping[str, Any]
    metadata: Mapping[str, Any]
    created_at: str

    @property
    def response(self) -> str | None:
        return self.response_text

    @property
    def response_is_safe(self) -> bool:
        return self.response_disposition in {"safe", "redacted"}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CacheHit:
    """A cached result and the completed run that produced it."""

    result: ProbeResultRecord
    source_run: RunRecord

    @property
    def source_run_id(self) -> str:
        return self.source_run.run_id


_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        endpoint_version TEXT NOT NULL,
        execution_fingerprint TEXT NOT NULL,
        suite_hash TEXT NOT NULL DEFAULT '',
        config_hash TEXT NOT NULL DEFAULT '',
        judge_prompt_hash TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        summary_json TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS probe_results (
        result_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        probe_hash TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'uncategorized',
        status TEXT NOT NULL DEFAULT 'completed',
        passed INTEGER CHECK (passed IS NULL OR passed IN (0, 1)),
        score REAL,
        latency_ms REAL,
        error_code TEXT,
        response_text TEXT,
        response_hash TEXT,
        response_disposition TEXT NOT NULL DEFAULT 'not_stored'
            CHECK (response_disposition IN ('not_stored', 'safe', 'redacted')),
        metrics_json TEXT NOT NULL DEFAULT '{}',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE (run_id, probe_hash),
        CHECK (
            (response_text IS NULL AND response_disposition = 'not_stored')
            OR
            (response_text IS NOT NULL AND response_disposition IN ('safe', 'redacted'))
        )
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_cache_key
        ON runs(endpoint_version, execution_fingerprint, status, completed_at);
    CREATE INDEX IF NOT EXISTS idx_runs_history
        ON runs(started_at DESC, status);
    CREATE INDEX IF NOT EXISTS idx_results_probe_history
        ON probe_results(probe_hash, run_id);
    CREATE INDEX IF NOT EXISTS idx_results_run_category
        ON probe_results(run_id, category);
    """,
    """
    ALTER TABLE runs ADD COLUMN run_hash TEXT NOT NULL DEFAULT '';
    CREATE INDEX IF NOT EXISTS idx_runs_reproducibility
        ON runs(suite_hash, config_hash, judge_prompt_hash, execution_fingerprint);
    """,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return _utc_now()
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("timestamp strings must not be blank")
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _required_text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _looks_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    safe_suffixes = (
        "_hash",
        "_digest",
        "_sha256",
        "_version",
        "_id",
        "_name",
        "_count",
        "_rate",
        "_score",
        "_status",
        "_code",
        "_tokens",
        "_length",
        "_bytes",
        "_valid",
        "_compliant",
        "_disposition",
        "_type",
        "_class",
    )
    if normalized.endswith(safe_suffixes):
        return False
    sensitive_tokens = (
        "prompt",
        "message",
        "query",
        "request",
        "response",
        "completion",
        "output",
        "content",
    )
    return any(token in normalized for token in sensitive_tokens) or normalized == "input_text"


def _assert_metadata_safe(value: Any, *, path: str = "metadata") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _looks_sensitive_key(key):
                raise UnsafePayloadError(
                    f"{path}.{key} may contain raw prompt/response content; store a hash instead"
                )
            _assert_metadata_safe(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_metadata_safe(child, path=f"{path}[{index}]")


def _json_object(value: Mapping[str, Any] | None, *, name: str) -> str:
    data: Mapping[str, Any] = {} if value is None else value
    if not isinstance(data, Mapping):
        raise TypeError(f"{name} must be a mapping")
    _assert_metadata_safe(data, path=name)
    try:
        return json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable") from exc


def _decode_json(value: str | None) -> Mapping[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, Mapping) else {}


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        endpoint_version=row["endpoint_version"],
        execution_fingerprint=row["execution_fingerprint"],
        suite_hash=row["suite_hash"],
        config_hash=row["config_hash"],
        judge_prompt_hash=row["judge_prompt_hash"],
        status=row["status"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        run_hash=row["run_hash"],
        metadata=_decode_json(row["metadata_json"]),
        summary=_decode_json(row["summary_json"]),
    )


def _result_from_row(row: sqlite3.Row, *, prefix: str = "") -> ProbeResultRecord:
    def field_value(name: str) -> Any:
        return row[f"{prefix}{name}"]

    passed = field_value("passed")
    return ProbeResultRecord(
        result_id=field_value("result_id"),
        run_id=field_value("run_id"),
        probe_hash=field_value("probe_hash"),
        category=field_value("category"),
        status=field_value("status"),
        passed=None if passed is None else bool(passed),
        score=field_value("score"),
        latency_ms=field_value("latency_ms"),
        error_code=field_value("error_code"),
        response_text=field_value("response_text"),
        response_hash=field_value("response_hash"),
        response_disposition=field_value("response_disposition"),
        metrics=_decode_json(field_value("metrics_json")),
        metadata=_decode_json(field_value("metadata_json")),
        created_at=field_value("created_at"),
    )


class EvaluationStore:
    """Context-managed SQLite repository with migrations and WAL enabled."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        auto_migrate: bool = True,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.busy_timeout_ms = busy_timeout_ms
        self._memory_connection: sqlite3.Connection | None = None
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if auto_migrate:
            self.migrate()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        is_memory = str(self.path) == ":memory:"
        if is_memory and self._memory_connection is not None:
            connection = self._memory_connection
        else:
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level="DEFERRED",
            )
            if is_memory:
                self._memory_connection = connection
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        if str(self.path) != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if not is_memory:
                connection.close()

    def close(self) -> None:
        """Close the anchor connection used by an in-memory store, if any."""

        if self._memory_connection is not None:
            self._memory_connection.close()
            self._memory_connection = None

    def __enter__(self) -> EvaluationStore:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # Some integration code prefers ``with store.connect()``.
    connect = connection

    def migrate(self) -> int:
        """Apply all forward-only migrations and return the resulting version."""

        with self.connection() as connection:
            # Lock before reading user_version so two cron processes cannot both
            # decide to apply the same ALTER TABLE migration.
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > LATEST_SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema {version} is newer than supported version "
                    f"{LATEST_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            for migration_version in range(version + 1, LATEST_SCHEMA_VERSION + 1):
                statements = _MIGRATIONS[migration_version - 1].split(";")
                for statement in statements:
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration_version, _utc_now()),
                )
                connection.execute(f"PRAGMA user_version = {migration_version:d}")
            return LATEST_SCHEMA_VERSION

    @property
    def schema_version(self) -> int:
        with self.connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def create_run(
        self,
        *,
        endpoint_version: str,
        execution_fingerprint: str,
        run_id: str | None = None,
        run_hash: str = "",
        suite_hash: str = "",
        probe_set_hash: str | None = None,
        config_hash: str = "",
        judge_prompt_hash: str = "",
        metadata: Mapping[str, Any] | None = None,
        started_at: datetime | str | None = None,
        status: str = "running",
    ) -> RunRecord:
        run_identifier = _required_text(run_id or str(uuid4()), name="run_id")
        if probe_set_hash is not None and suite_hash and probe_set_hash != suite_hash:
            raise ValueError("suite_hash and probe_set_hash aliases must match")
        values = {
            "run_id": run_identifier,
            "endpoint_version": _required_text(endpoint_version, name="endpoint_version"),
            "execution_fingerprint": _required_text(
                execution_fingerprint, name="execution_fingerprint"
            ),
            "run_hash": str(run_hash or ""),
            "suite_hash": str(probe_set_hash if probe_set_hash is not None else suite_hash),
            "config_hash": str(config_hash or ""),
            "judge_prompt_hash": str(judge_prompt_hash or ""),
            "status": _required_text(status, name="status"),
            "started_at": _timestamp(started_at),
            "metadata_json": _json_object(metadata, name="metadata"),
        }
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, endpoint_version, execution_fingerprint, run_hash, suite_hash,
                    config_hash, judge_prompt_hash, status, started_at, metadata_json
                ) VALUES (
                    :run_id, :endpoint_version, :execution_fingerprint, :run_hash, :suite_hash,
                    :config_hash, :judge_prompt_hash, :status, :started_at, :metadata_json
                )
                """,
                values,
            )
        result = self.get_run(run_identifier)
        assert result is not None
        return result

    # ``start_run`` reads more naturally in orchestration code.
    start_run = create_run

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        run_hash: str | None = None,
        summary: Mapping[str, Any] | None = None,
        completed_at: datetime | str | None = None,
    ) -> RunRecord:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, summary_json = ?,
                    run_hash = COALESCE(?, run_hash)
                WHERE run_id = ?
                """,
                (
                    _required_text(status, name="status"),
                    _timestamp(completed_at),
                    _json_object(summary, name="summary"),
                    run_hash,
                    _required_text(run_id, name="run_id"),
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run_id: {run_id}")
        result = self.get_run(run_id)
        assert result is not None
        return result

    complete_run = finish_run

    def get_run(self, run_id: str) -> RunRecord | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_from_row(row) if row is not None else None

    def record_probe_result(
        self,
        *,
        run_id: str,
        probe_hash: str,
        category: str = "uncategorized",
        status: str = "completed",
        passed: bool | None = None,
        score: float | None = None,
        latency_ms: float | None = None,
        error_code: str | None = None,
        response_text: str | None = None,
        response_safe: bool = False,
        response_redacted: bool = False,
        response_hash: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: datetime | str | None = None,
    ) -> ProbeResultRecord:
        """Persist one probe result after enforcing the content-safety boundary."""

        if response_safe and response_redacted:
            raise ValueError("a response cannot be marked both safe and redacted")
        if response_text is not None and not (response_safe or response_redacted):
            raise UnsafePayloadError(
                "response_text requires response_safe=True or response_redacted=True"
            )
        if response_text is None and (response_safe or response_redacted):
            raise ValueError("a response disposition cannot be set without response_text")
        if score is not None and not math.isfinite(float(score)):
            raise ValueError("score must be finite")
        if passed is not None and not (
            isinstance(passed, bool)
            or (isinstance(passed, int) and not isinstance(passed, bool) and passed in (0, 1))
        ):
            raise TypeError("passed must be a boolean, 0/1, or None")
        if latency_ms is not None and (
            not math.isfinite(float(latency_ms)) or float(latency_ms) < 0.0
        ):
            raise ValueError("latency_ms must be a finite non-negative number")

        computed_hash = (
            hashlib.sha256(response_text.encode("utf-8")).hexdigest()
            if response_text is not None
            else None
        )
        if response_hash and computed_hash and response_hash != computed_hash:
            raise ValueError("response_hash does not match response_text")
        disposition = "safe" if response_safe else "redacted" if response_redacted else "not_stored"
        values = (
            _required_text(run_id, name="run_id"),
            _required_text(probe_hash, name="probe_hash"),
            _required_text(category, name="category"),
            _required_text(status, name="status"),
            None if passed is None else int(bool(passed)),
            None if score is None else float(score),
            None if latency_ms is None else float(latency_ms),
            str(error_code) if error_code is not None else None,
            response_text,
            computed_hash or response_hash,
            disposition,
            _json_object(metrics, name="metrics"),
            _json_object(metadata, name="metadata"),
            _timestamp(created_at),
        )
        try:
            with self.connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO probe_results (
                        run_id, probe_hash, category, status, passed, score,
                        latency_ms, error_code, response_text, response_hash,
                        response_disposition, metrics_json, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                result_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise StorageError(
                    f"probe {probe_hash!r} already has a result in run {run_id!r}"
                ) from exc
            raise
        result = self.get_probe_result(run_id, probe_hash)
        assert result is not None and result.result_id == result_id
        return result

    add_probe_result = record_probe_result

    def get_probe_result(self, run_id: str, probe_hash: str) -> ProbeResultRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM probe_results
                WHERE run_id = ? AND probe_hash = ?
                """,
                (run_id, probe_hash),
            ).fetchone()
        return _result_from_row(row) if row is not None else None

    def get_probe_results(
        self,
        run_id: str,
        *,
        category: str | None = None,
    ) -> list[ProbeResultRecord]:
        parameters: list[Any] = [run_id]
        category_clause = ""
        if category is not None:
            category_clause = " AND category = ?"
            parameters.append(category)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM probe_results
                WHERE run_id = ?{category_clause}
                ORDER BY category, probe_hash
                """,
                parameters,
            ).fetchall()
        return [_result_from_row(row) for row in rows]

    list_probe_results = get_probe_results

    def lookup_cache(
        self,
        *,
        probe_hash: str,
        endpoint_version: str,
        execution_fingerprint: str,
        exclude_run_id: str | None = None,
    ) -> CacheHit | None:
        """Find the newest completed result matching the full cache key."""

        parameters: list[Any] = [probe_hash, endpoint_version, execution_fingerprint]
        exclusion = ""
        if exclude_run_id is not None:
            exclusion = " AND r.run_id <> ?"
            parameters.append(exclude_run_id)
        with self.connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    p.*,
                    r.endpoint_version AS cache_endpoint_version,
                    r.execution_fingerprint AS cache_execution_fingerprint,
                    r.run_hash AS cache_run_hash,
                    r.suite_hash AS cache_suite_hash,
                    r.config_hash AS cache_config_hash,
                    r.judge_prompt_hash AS cache_judge_prompt_hash,
                    r.status AS cache_run_status,
                    r.started_at AS cache_started_at,
                    r.completed_at AS cache_completed_at,
                    r.metadata_json AS cache_run_metadata_json,
                    r.summary_json AS cache_summary_json
                FROM probe_results AS p
                JOIN runs AS r ON r.run_id = p.run_id
                WHERE p.probe_hash = ?
                  AND r.endpoint_version = ?
                  AND r.execution_fingerprint = ?
                  AND r.status = 'completed'
                  AND p.status = 'completed'
                  {exclusion}
                ORDER BY COALESCE(r.completed_at, r.started_at) DESC, p.result_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        if row is None:
            return None
        result = _result_from_row(row)
        source_run = RunRecord(
            run_id=row["run_id"],
            endpoint_version=row["cache_endpoint_version"],
            execution_fingerprint=row["cache_execution_fingerprint"],
            suite_hash=row["cache_suite_hash"],
            config_hash=row["cache_config_hash"],
            judge_prompt_hash=row["cache_judge_prompt_hash"],
            status=row["cache_run_status"],
            started_at=row["cache_started_at"],
            completed_at=row["cache_completed_at"],
            run_hash=row["cache_run_hash"],
            metadata=_decode_json(row["cache_run_metadata_json"]),
            summary=_decode_json(row["cache_summary_json"]),
        )
        return CacheHit(result=result, source_run=source_run)

    cache_lookup = lookup_cache

    def list_runs(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        endpoint_version: str | None = None,
        execution_fingerprint: str | None = None,
        before_started_at: str | None = None,
    ) -> list[RunRecord]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("status", status),
            ("endpoint_version", endpoint_version),
            ("execution_fingerprint", execution_fingerprint),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if before_started_at is not None:
            clauses.append("started_at < ?")
            parameters.append(before_started_at)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs {where} ORDER BY started_at DESC, run_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def get_previous_compatible_run(
        self,
        run_id: str,
        *,
        require_same_endpoint_version: bool = False,
    ) -> RunRecord | None:
        """Return the prior completed run with matching suite/execution hashes.

        Endpoint versions may differ by default: release-to-release change is the
        useful comparison.  Set ``require_same_endpoint_version`` for rerun/noise
        checks.  Config, probe suite, judge prompt, and execution fingerprint must
        match so that paired statistics compare like with like.
        """

        current = self.get_run(run_id)
        if current is None:
            raise KeyError(f"unknown run_id: {run_id}")
        with self.connection() as connection:
            current_rowid = connection.execute(
                "SELECT rowid FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        clauses = [
            "status = 'completed'",
            "(started_at < ? OR (started_at = ? AND rowid < ?))",
            "suite_hash = ?",
            "config_hash = ?",
            "judge_prompt_hash = ?",
            "execution_fingerprint = ?",
        ]
        parameters: list[Any] = [
            current.started_at,
            current.started_at,
            current_rowid,
            current.suite_hash,
            current.config_hash,
            current.judge_prompt_hash,
            current.execution_fingerprint,
        ]
        if require_same_endpoint_version:
            clauses.append("endpoint_version = ?")
            parameters.append(current.endpoint_version)
        with self.connection() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM runs
                WHERE {" AND ".join(clauses)}
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    previous_compatible_run = get_previous_compatible_run

    def probe_history(
        self,
        probe_hash: str,
        *,
        limit: int = 50,
        endpoint_version: str | None = None,
        execution_fingerprint: str | None = None,
    ) -> list[ProbeResultRecord]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        clauses = ["p.probe_hash = ?"]
        parameters: list[Any] = [probe_hash]
        if endpoint_version is not None:
            clauses.append("r.endpoint_version = ?")
            parameters.append(endpoint_version)
        if execution_fingerprint is not None:
            clauses.append("r.execution_fingerprint = ?")
            parameters.append(execution_fingerprint)
        parameters.append(limit)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*
                FROM probe_results AS p
                JOIN runs AS r ON r.run_id = p.run_id
                WHERE {" AND ".join(clauses)}
                ORDER BY r.started_at DESC, p.result_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [_result_from_row(row) for row in rows]

    def delete_run(self, run_id: str) -> bool:
        """Delete one explicitly named run (probe rows cascade)."""

        with self.connection() as connection:
            cursor = connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            return cursor.rowcount == 1


# Shorter alias for application code and backwards-friendly integration.
SQLiteStore = EvaluationStore


__all__ = [
    "CacheHit",
    "EvaluationStore",
    "LATEST_SCHEMA_VERSION",
    "ProbeResultRecord",
    "RunRecord",
    "SQLiteStore",
    "SchemaVersionError",
    "StorageError",
    "UnsafePayloadError",
]
