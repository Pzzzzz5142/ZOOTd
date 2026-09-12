from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import Activity, ActivityStage, StageEfficiency
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    fixed_timezone,
    isoformat,
    load_json,
    parse_iso_datetime,
    sha256_bytes,
    utc_now,
)


class SourceError(RuntimeError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SourceError(f"non-finite JSON number: {value}")


def _load_json_bytes(body: bytes) -> Any:
    try:
        return json.loads(
            body,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"response is not valid JSON: {exc}") from exc


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], None]) -> None:
        super().__init__()
        self._validator = validator

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    metadata: dict[str, Any]
    network_validated: bool

    @property
    def sha256(self) -> str:
        return str(self.metadata["sha256"])

    @property
    def fetched_at(self) -> datetime:
        return parse_iso_datetime(str(self.metadata["fetched_at"]))

    def json(self) -> Any:
        return _load_json_bytes(self.body)


class HttpCache:
    """Small conditional-GET cache that never replaces a valid body with bad data."""

    def __init__(
        self,
        root: Path,
        *,
        allowed_hosts: Iterable[str],
        timeout: float = 15.0,
        retries: int = 2,
        max_bytes: int = 12 * 1024 * 1024,
        network_enabled: bool = True,
    ) -> None:
        self.root = root
        self.allowed_hosts = frozenset(allowed_hosts)
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.network_enabled = network_enabled
        self._opener = urllib.request.build_opener(
            _AllowlistedRedirectHandler(self._validate_url)
        )

    def fetch(
        self,
        key: str,
        url: str,
        *,
        accept: str,
        max_stale: timedelta,
        validate: Callable[[bytes], None] | None = None,
        method: str = "GET",
        request_body: bytes | None = None,
    ) -> FetchResult:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise ValueError(f"unsafe cache key: {key!r}")
        self._validate_url(url)
        body_path = self.root / f"{key}.body"
        metadata_path = self.root / f"{key}.meta.json"
        previous = self._load_metadata(metadata_path)
        request_sha256 = sha256_bytes(request_body) if request_body is not None else None
        previous_compatible = bool(
            previous
            and previous.get("url") == url
            and previous.get("method", "GET") == method
            and previous.get("request_sha256") == request_sha256
        )
        headers = {
            "Accept": accept,
            "User-Agent": "zootd-planner/1 (+local unattended launcher)",
        }
        if request_body is not None:
            headers["Content-Type"] = "application/json"
        if previous_compatible and previous.get("etag"):
            headers["If-None-Match"] = str(previous["etag"])
        if previous_compatible and previous.get("last_modified"):
            headers["If-Modified-Since"] = str(previous["last_modified"])

        last_error: Exception | None = None
        attempts = self.retries + 1 if self.network_enabled else 0
        for attempt in range(attempts):
            try:
                request = urllib.request.Request(
                    url,
                    data=request_body,
                    headers=headers,
                    method=method,
                )
                with self._opener.open(request, timeout=self.timeout) as response:
                    self._validate_url(response.geturl())
                    body = response.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise SourceError(f"response exceeds {self.max_bytes} bytes")
                    if validate is not None:
                        validate(body)
                    content_type = response.headers.get("Content-Type", "")
                    metadata = {
                        "schema_version": 1,
                        "url": url,
                        "final_url": response.geturl(),
                        "fetched_at": isoformat(utc_now()),
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "content_type": content_type,
                        "sha256": sha256_bytes(body),
                        "size": len(body),
                        "method": method,
                        "request_sha256": request_sha256,
                    }
                    atomic_write_bytes(body_path, body)
                    atomic_write_json(metadata_path, metadata)
                    return FetchResult(body, metadata, True)
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and body_path.is_file() and previous_compatible:
                    body = body_path.read_bytes()
                    if sha256_bytes(body) != previous.get("sha256"):
                        raise SourceError(f"cache checksum mismatch for {key}")
                    if validate is not None:
                        validate(body)
                    previous = {
                        **previous,
                        "fetched_at": isoformat(utc_now()),
                        "revalidated": True,
                    }
                    atomic_write_json(metadata_path, previous)
                    return FetchResult(body, previous, True)
                last_error = exc
                if 400 <= exc.code < 500:
                    break
            except (OSError, SourceError, urllib.error.URLError) as exc:
                last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))

        if body_path.is_file() and previous_compatible:
            try:
                age = utc_now() - parse_iso_datetime(str(previous["fetched_at"]))
            except (KeyError, TypeError, ValueError):
                age = max_stale + timedelta(seconds=1)
            if age <= max_stale:
                body = body_path.read_bytes()
                if sha256_bytes(body) != previous.get("sha256"):
                    raise SourceError(f"cache checksum mismatch for {key}")
                # A fresh checksum proves identity, not semantic validity.  Run
                # the same strict parser/schema validator on offline and
                # network-fallback bodies that a live response must pass.
                if validate is not None:
                    validate(body)
                return FetchResult(body, {**previous, "fallback_error": str(last_error)}, False)
        raise SourceError(f"failed to refresh {url}: {last_error}")

    def fetch_json(
        self,
        key: str,
        url: str,
        *,
        max_stale: timedelta,
        validator: Callable[[Any], None] | None = None,
        method: str = "GET",
        json_body: Any | None = None,
    ) -> tuple[Any, FetchResult]:
        def validate_json(body: bytes) -> None:
            value = _load_json_bytes(body)
            if validator is not None:
                validator(value)

        result = self.fetch(
            key,
            url,
            accept="application/json",
            max_stale=max_stale,
            validate=validate_json,
            method=method,
            request_body=(
                json.dumps(json_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if json_body is not None
                else None
            ),
        )
        return result.json(), result

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise SourceError(f"URL is outside the source allowlist: {url}")
        if parsed.username or parsed.password:
            raise SourceError("credentials in source URL are not allowed")

    @staticmethod
    def _load_metadata(path: Path) -> dict[str, Any]:
        try:
            value = load_json(path)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


def parse_maa_activities(payload: Any, client: str, source_sha256: str) -> list[Activity]:
    if not isinstance(payload, dict):
        raise SourceError("MAA activity root must be an object")
    source_client = "Official" if client == "Bilibili" else client
    client_data = payload.get(source_client)
    if not isinstance(client_data, dict):
        raise SourceError(f"MAA activity has no client section: {source_client}")
    side_stories = client_data.get("sideStoryStage")
    if not isinstance(side_stories, dict):
        raise SourceError("MAA activity sideStoryStage must be an object")

    result: list[Activity] = []
    for key, raw in side_stories.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            continue
        info = raw.get("Activity")
        raw_stages = raw.get("Stages")
        if not isinstance(info, dict) or not isinstance(raw_stages, list):
            continue
        try:
            offset = int(info["TimeZone"])
            zone = fixed_timezone(offset)
            start = datetime.strptime(str(info["UtcStartTime"]), "%Y/%m/%d %H:%M:%S").replace(tzinfo=zone)
            # MAA stores the last valid second; use a half-open interval internally.
            end = datetime.strptime(str(info["UtcExpireTime"]), "%Y/%m/%d %H:%M:%S").replace(
                tzinfo=zone
            ) + timedelta(seconds=1)
            name = str(info["StageName"])
            tip = str(info["Tip"])
            minimum_required = raw.get("MinimumRequired")
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"invalid MAA activity time record for {key}: {exc}") from exc
        if end <= start:
            raise SourceError(f"MAA activity has a non-positive window: {key}")
        if not isinstance(minimum_required, str) or not re.fullmatch(
            r"v?\d+(?:\.\d+){1,3}", minimum_required
        ):
            raise SourceError(f"MAA activity has an invalid MinimumRequired: {key}")

        stages: list[ActivityStage] = []
        for stage in raw_stages:
            if not isinstance(stage, dict):
                continue
            code = stage.get("Value") or stage.get("Display")
            item_id = stage.get("Drop")
            if isinstance(code, str) and isinstance(item_id, str) and re.fullmatch(r"\d+", item_id):
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,63}", code):
                    stages.append(ActivityStage(code=code, item_id=item_id))
        result.append(
            Activity(
                client=client,
                key=key,
                name=name,
                tip=tip,
                start=start,
                end=end,
                minimum_required=minimum_required,
                stages=tuple(stages),
                source_sha256=source_sha256,
            )
        )
    return result


def _unwrap_list(payload: Any, name: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload = payload["data"]
    if not isinstance(payload, list):
        raise SourceError(f"{name} must be an array or a data envelope")
    return [item for item in payload if isinstance(item, dict)]


def build_yituliu_efficiencies(
    stage_payload: Any,
    matrix_payload: Any,
    value_payload: Any,
    candidates: Iterable[ActivityStage],
    *,
    minimum_samples: int,
    source_sha256: str,
) -> dict[str, StageEfficiency]:
    stages = _unwrap_list(stage_payload, "yituliu stage info")
    values = _unwrap_list(value_payload, "yituliu item values")
    if isinstance(matrix_payload, dict):
        matrix_payload = matrix_payload.get("matrix")
    matrix = _unwrap_list(matrix_payload, "yituliu matrix")

    item_values: dict[str, float] = {}
    for item in values:
        item_id = item.get("itemId")
        item_value = item.get("itemValue")
        if (
            isinstance(item_id, str)
            and isinstance(item_value, (int, float))
            and not isinstance(item_value, bool)
            and math.isfinite(float(item_value))
            and item_value >= 0
        ):
            item_values[item_id] = float(item_value)
    if "4001" not in item_values:
        raise SourceError("yituliu value table has no LMD value")

    stages_by_code: dict[str, list[dict[str, Any]]] = {}
    for stage in stages:
        code = stage.get("stageCode")
        if isinstance(code, str):
            stages_by_code.setdefault(code, []).append(stage)

    matrix_by_stage: dict[str, list[dict[str, Any]]] = {}
    for row in matrix:
        stage_id = row.get("stageId")
        if isinstance(stage_id, str):
            matrix_by_stage.setdefault(stage_id, []).append(row)

    unlimited_items = {
        "4001": (20.0, 1.0),
        "30073": (1.0, 25.0),
        "30083": (1.0, 30.0),
        "30093": (1.0, 35.0),
        "30103": (1.0, 40.0),
    }
    unlimited_value_per_token = max(
        (item_values.get(item_id, 0.0) * quantity / price for item_id, (quantity, price) in unlimited_items.items()),
        default=0.0,
    )

    result: dict[str, StageEfficiency] = {}
    for candidate in candidates:
        matches: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for stage in stages_by_code.get(candidate.code, []):
            stage_id = stage.get("stageId")
            if not isinstance(stage_id, str):
                continue
            for row in matrix_by_stage.get(stage_id, []):
                if row.get("itemId") != candidate.item_id:
                    continue
                times = row.get("times")
                quantity = row.get("quantity")
                if (
                    isinstance(times, int)
                    and not isinstance(times, bool)
                    and isinstance(quantity, (int, float))
                    and not isinstance(quantity, bool)
                    and math.isfinite(float(quantity))
                    and times >= minimum_samples
                    and quantity > 0
                ):
                    matches.append((times, stage, row))
        if not matches:
            continue
        matches.sort(key=lambda value: (value[0], str(value[1].get("stageId"))), reverse=True)
        sample_size, stage, main_row = matches[0]
        stage_id = str(stage["stageId"])
        ap_cost = stage.get("apCost")
        if not isinstance(ap_cost, int) or isinstance(ap_cost, bool) or ap_cost <= 0:
            continue
        drop_rate = float(main_row["quantity"]) / sample_size
        if drop_rate <= 0:
            continue

        expected_value = ap_cost * 12.0 * item_values["4001"]
        for row in matrix_by_stage.get(stage_id, []):
            item_id = row.get("itemId")
            times = row.get("times")
            quantity = row.get("quantity")
            if (
                isinstance(item_id, str)
                and item_id in item_values
                and isinstance(times, int)
                and not isinstance(times, bool)
                and times >= minimum_samples
                and isinstance(quantity, (int, float))
                and not isinstance(quantity, bool)
                and math.isfinite(float(quantity))
                and quantity >= 0
            ):
                expected_value += float(quantity) / times * item_values[item_id]
        if stage.get("stageType") in {"ACT", "ACT_REP"}:
            expected_value += ap_cost * unlimited_value_per_token

        result[candidate.code] = StageEfficiency(
            stage_code=candidate.code,
            stage_id=stage_id,
            item_id=candidate.item_id,
            ap_cost=ap_cost,
            drop_rate=drop_rate,
            expected_ap_per_item=ap_cost / drop_rate,
            overall_efficiency=expected_value / ap_cost,
            sample_size=sample_size,
            source_sha256=source_sha256,
        )
    return result
