from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable

from .util import canonical_json


class AgentAdviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentAdvice:
    classification: str
    confidence: float
    summary: str
    mappings: tuple[dict[str, str], ...]
    evidence_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "classification": self.classification,
            "confidence": self.confidence,
            "summary": self.summary,
            "mappings": list(self.mappings),
            "evidence_refs": list(self.evidence_refs),
            "authorization": "advisory-only",
        }


def _strings(value: object, field: str, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise AgentAdviceError(f"{field} must be an array with at most {maximum} entries")
    result = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 512:
            raise AgentAdviceError(f"invalid entry in {field}")
        result.append(item)
    return tuple(result)


def validate_advice(value: object, allowed_stages: Iterable[str]) -> AgentAdvice:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise AgentAdviceError("agent output has an unsupported schema")
    classification = value.get("classification")
    if classification not in {"stage_mapping", "source_schema", "source_conflict", "unknown"}:
        raise AgentAdviceError("invalid agent classification")
    confidence = value.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise AgentAdviceError("confidence must be between zero and one")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary or len(summary) > 2000:
        raise AgentAdviceError("summary is missing or too long")
    evidence_refs = _strings(value.get("evidence_refs", []), "evidence_refs", maximum=32)
    allowed = set(allowed_stages)
    raw_mappings = value.get("mappings", [])
    if not isinstance(raw_mappings, list) or len(raw_mappings) > 32:
        raise AgentAdviceError("mappings must be a bounded array")
    mappings: list[dict[str, str]] = []
    for raw in raw_mappings:
        if not isinstance(raw, dict) or set(raw) != {"stage_code", "yituliu_stage_id", "reason"}:
            raise AgentAdviceError("invalid mapping shape")
        if raw.get("stage_code") not in allowed:
            raise AgentAdviceError("agent invented a stage outside the deterministic candidate set")
        if not all(isinstance(raw.get(key), str) and 0 < len(raw[key]) <= 512 for key in raw):
            raise AgentAdviceError("invalid mapping value")
        mappings.append(dict(raw))
    return AgentAdvice(
        classification=classification,
        confidence=float(confidence),
        summary=summary,
        mappings=tuple(mappings),
        evidence_refs=evidence_refs,
    )


def run_advisor(
    command: Iterable[str],
    evidence_bundle: dict[str, Any],
    *,
    allowed_stages: Iterable[str],
    timeout_seconds: int,
) -> AgentAdvice:
    argv = tuple(command)
    if not argv:
        raise AgentAdviceError("agent command is empty")
    try:
        completed = subprocess.run(
            argv,
            input=canonical_json(evidence_bundle),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentAdviceError(f"agent invocation failed: {exc}") from exc
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace")[-1000:]
        raise AgentAdviceError(f"agent exited with {completed.returncode}: {error}")
    if len(completed.stdout) > 128 * 1024:
        raise AgentAdviceError("agent output is too large")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentAdviceError("agent did not return one JSON object") from exc
    return validate_advice(value, allowed_stages)
