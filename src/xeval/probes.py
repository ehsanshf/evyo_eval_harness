"""Versioned YAML probe catalog loader and validator."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError
from .hashing import content_hash
from .models import Message, Probe, ScorerSpec

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
_ROLES = {"system", "user", "assistant"}


def _parse_scorer(value: Any, source: Path, probe_id: str) -> ScorerSpec:
    if isinstance(value, str):
        return ScorerSpec(value, {})
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise ConfigurationError(f"{source}: probe {probe_id}: invalid scorer specification")
    params = dict(value)
    name = params.pop("name")
    nested = params.pop("params", None)
    if nested is not None:
        if not isinstance(nested, dict):
            raise ConfigurationError(f"{source}: probe {probe_id}: scorer params must be a mapping")
        params = {**nested, **params}
    return ScorerSpec(name=name, params=params)


def load_probe_file(path: str | Path) -> tuple[Probe, ...]:
    source = Path(path).resolve()
    if not source.is_file():
        raise ConfigurationError(f"probe suite does not exist: {source}")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{source}: suite root must be a mapping")
    version = str(data.get("version", "1"))
    if version != "1":
        raise ConfigurationError(f"{source}: unsupported suite version {version}; expected 1")
    suite = str(data.get("suite", source.stem)).strip()
    if not suite:
        raise ConfigurationError(f"{source}: suite cannot be blank")
    rows = data.get("probes")
    if not isinstance(rows, list) or not rows:
        raise ConfigurationError(f"{source}: probes must be a non-empty list")

    loaded: list[Probe] = []
    local_ids: set[str] = set()
    for index, row in enumerate(rows):
        where = f"{source}: probes[{index}]"
        if not isinstance(row, dict):
            raise ConfigurationError(f"{where} must be a mapping")
        probe_id = str(row.get("id", "")).strip()
        if not _ID_PATTERN.fullmatch(probe_id):
            raise ConfigurationError(f"{where}: invalid id {probe_id!r}")
        if probe_id in local_ids:
            raise ConfigurationError(f"{where}: duplicate id {probe_id}")
        local_ids.add(probe_id)
        category = str(row.get("category", "")).strip()
        if not category:
            raise ConfigurationError(f"{where}: category is required")
        message_rows = row.get("messages")
        if not isinstance(message_rows, list) or not message_rows:
            raise ConfigurationError(f"{where}: messages must be a non-empty list")
        messages: list[Message] = []
        for message_index, message in enumerate(message_rows):
            if not isinstance(message, dict):
                raise ConfigurationError(f"{where}: messages[{message_index}] must be a mapping")
            role = str(message.get("role", ""))
            content = message.get("content")
            if role not in _ROLES or not isinstance(content, str) or not content.strip():
                raise ConfigurationError(
                    f"{where}: messages[{message_index}] requires a valid role "
                    "and non-empty content"
                )
            messages.append(Message(role, content))
        scorer_rows = row.get("scorers")
        if not isinstance(scorer_rows, list) or not scorer_rows:
            raise ConfigurationError(f"{where}: scorers must be a non-empty list")
        scorers = tuple(_parse_scorer(item, source, probe_id) for item in scorer_rows)
        tags_value = row.get("tags", [])
        if not isinstance(tags_value, list) or not all(isinstance(tag, str) for tag in tags_value):
            raise ConfigurationError(f"{where}: tags must be a list of strings")
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ConfigurationError(f"{where}: metadata must be a mapping")
        hash_input = {
            "suite": suite,
            "suite_version": version,
            "id": probe_id,
            "category": category,
            "messages": message_rows,
            "scorers": scorer_rows,
            "tags": tags_value,
            "sensitive": bool(row.get("sensitive", False)),
            "metadata": metadata,
        }
        loaded.append(
            Probe(
                id=probe_id,
                category=category,
                messages=tuple(messages),
                scorers=scorers,
                suite=suite,
                suite_version=version,
                source=source,
                tags=tuple(tags_value),
                sensitive=bool(row.get("sensitive", False)),
                metadata=metadata,
                probe_hash=content_hash(hash_input),
            )
        )
    return tuple(loaded)


def load_probes(paths: tuple[Path, ...] | list[Path]) -> tuple[Probe, ...]:
    probes = [probe for path in paths for probe in load_probe_file(path)]
    seen: dict[str, Path] = {}
    for probe in probes:
        if probe.id in seen:
            raise ConfigurationError(
                f"duplicate probe id {probe.id!r} in {seen[probe.id]} and {probe.source}"
            )
        seen[probe.id] = probe.source
    return tuple(sorted(probes, key=lambda probe: probe.id))


def probe_set_hash(probes: tuple[Probe, ...]) -> str:
    return content_hash([(probe.id, probe.probe_hash) for probe in probes])
