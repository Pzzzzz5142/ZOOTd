"""Explicit read-only PRTS adapter; no Box, ranking, execution or cache fallback."""
from __future__ import annotations

import copy
import http.client
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .sources import SourceError, _load_json_bytes


class PrtsError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PrtsError("schema_error", message)


def decode(raw: bytes | str):
    try:
        value = _load_json_bytes(raw)

        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("non-finite number")
            if isinstance(item, dict):
                for child in item.values():
                    finite(child)
            elif isinstance(item, list):
                for child in item:
                    finite(child)
        finite(value)
        return value
    except (SourceError, ValueError, RecursionError):
        raise PrtsError("schema_error", "Invalid PRTS JSON.") from None


def text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


class StageCatalog:
    """Use installed MAA stage IDs; ambiguous codes are never guessed."""
    def __init__(self, rows: list):
        require(isinstance(rows, list) and bool(rows), "Invalid MAA stage catalog.")
        self.aliases: dict[str, set[str]] = {}
        self.query_ids: dict[str, str] = {}
        for row in rows:
            require(isinstance(row, dict) and text(row.get("stageId")) and text(row.get("code")),
                    "Invalid MAA stage identity.")
            for field in ("stageId", "code", "levelId"):
                alias = row.get(field)
                if alias is not None:
                    require(text(alias), "Invalid MAA stage alias.")
                    self.aliases.setdefault(alias.casefold(), set()).add(row["stageId"])

    @classmethod
    def load(cls, path: Path) -> StageCatalog:
        try:
            rows = decode(path.read_bytes())
            catalog = cls(rows)
            overview = path.parent / 'Arknights-Tile-Pos/overview.json'
            if overview.exists():
                tiles = decode(overview.read_bytes())
                require(isinstance(tiles, dict), 'Invalid tile catalog.')
                # Permanent side stories use old battle IDs in PRTS. Bind the
                # alias only when both installed catalogs agree on code and ID.
                for row in rows:
                    stage = row['stageId']
                    if not stage.endswith('_perm'):
                        continue
                    matches = [t for t in tiles.values() if isinstance(t, dict)
                               and t.get('stageId') == stage[:-5]
                               and t.get('code') == row['code']]
                    if len(matches) == 1:
                        tile = matches[0]
                        for key in ('stageId', 'levelId'):
                            if text(tile.get(key)):
                                catalog.aliases.setdefault(tile[key].casefold(), set()).add(stage)
                        catalog.query_ids[stage] = tile['stageId']
            return catalog
        except OSError:
            raise PrtsError("stage_identity", "Cannot read installed MAA stages.json.") from None

    def resolve(self, value: str) -> str:
        matches = self.aliases.get(value.casefold(), set()) if text(value) else set()
        if len(matches) != 1:
            raise PrtsError("stage_identity", "Unknown or ambiguous stage; use an installed canonical stageId.")
        return next(iter(matches))


def operators(value) -> list[dict]:
    require(isinstance(value, list), "opers must be an array.")
    result = []
    for oper in value:
        require(isinstance(oper, dict) and text(oper.get("name")), "Invalid operator.")
        skill = oper.get("skill")
        require(skill is None or (type(skill) is int and 0 <= skill <= 3), "Invalid skill index.")
        require(oper.get("role") is None or text(oper["role"]), "Invalid operator role.")
        requirements = oper.get("requirements", {})
        require(isinstance(requirements, dict), "Invalid requirements.")
        # Preserve unknown requirements verbatim for the future matcher; no satisfied defaults.
        result.append({"name": oper["name"], "role": oper.get("role"), "skill": skill,
                       "requirements": copy.deepcopy(requirements)})
    return result


@dataclass(frozen=True)
class CopilotCandidate:
    id: int
    stage: str
    title: str
    operators: list[dict]
    groups: list[dict]
    difficulty: int | None
    metadata: dict
    source: str = "prts-plus"

    def to_dict(self) -> dict:
        return asdict(self)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PrtsError("network", "PRTS redirect refused.")


class PrtsCopilotClient:
    BASE_URL = "https://prts.maa.plus"
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, catalog: StageCatalog, *, opener=None):
        self.catalog = catalog
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def _request(self, path: str, *, full: bool = False) -> dict:
        request = urllib.request.Request(self.BASE_URL + path, headers={
            "Accept": "application/json", "User-Agent": "ZOOTd-experimental-prts/1"})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(self.MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            category = "copilot_not_found" if full and exc.code == 404 else "network"
            raise PrtsError(category, f"PRTS HTTP {exc.code}.") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise PrtsError("network", "PRTS request failed.") from None
        require(len(raw) <= self.MAX_BYTES, "PRTS response too large.")
        envelope = decode(raw)
        require(isinstance(envelope, dict) and type(envelope.get("status_code")) is int,
                "Invalid PRTS envelope.")
        status = envelope["status_code"]
        if full and status == 404:
            raise PrtsError("copilot_not_found", "Copilot not found.")
        if status != 200:
            raise PrtsError("network", f"PRTS service status {status}.")
        require(isinstance(envelope.get("data"), dict), "Missing PRTS data.")
        return envelope["data"]

    def _parse(self, row: dict, stage: str) -> tuple[CopilotCandidate, dict]:
        require(isinstance(row, dict), "Invalid candidate.")
        require(type(row.get("id")) is int and row["id"] > 0, "Invalid copilot ID.")
        require(row.get("type") == "PRTS" and row.get("available") is True,
                "Unavailable or unsupported copilot.")
        require(isinstance(row.get("content"), str), "Missing copilot content.")
        content = decode(row["content"])
        require(isinstance(content, dict) and text(content.get("stage_name")), "Invalid copilot stage.")
        try:
            actual = self.catalog.resolve(content["stage_name"])
        except PrtsError:
            raise PrtsError("stage_mismatch", "Copilot stage cannot be resolved.") from None
        if actual != stage:
            raise PrtsError("stage_mismatch", "Copilot stage differs from requested stage.")
        doc = content.get("doc")
        require(isinstance(doc, dict) and text(doc.get("title")), "Missing copilot title.")
        opers = operators(content.get("opers", []))
        groups = content.get("groups", [])
        require(isinstance(groups, list), "groups must be an array.")
        normalized_groups = []
        names = set()
        for group in groups:
            require(isinstance(group, dict) and text(group.get("name")), "Invalid group.")
            require(group["name"] not in names, "Duplicate group name.")
            names.add(group["name"])
            members = operators(group.get("opers"))
            require(bool(members), "Empty group.")
            normalized_groups.append({"name": group["name"], "operators": members})
        difficulty = content.get("difficulty")
        require(difficulty is None or (type(difficulty) is int and difficulty in (0, 1, 2, 3)),
                "Invalid difficulty.")
        metadata = {}
        for key in ("hot_score", "views", "rating_level", "rating_ratio", "like", "dislike"):
            value = row.get(key)
            require(value is None or (type(value) is int or (type(value) is float and math.isfinite(value))),
                    "Invalid rating metadata.")
            metadata[key] = value
        return CopilotCandidate(row["id"], stage, doc["title"], opers,
                                normalized_groups, difficulty, metadata), content

    def query(self, stage: str, *, page: int = 1, limit: int = 10) -> dict:
        canonical = self.catalog.resolve(stage)
        if type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 50:
            raise PrtsError("input", "page must be positive; limit must be 1..50.")
        params = urllib.parse.urlencode({"level_keyword": self.catalog.query_ids.get(canonical, canonical), "page": page, "limit": limit,
                                         "order_by": "id", "desc": "true"})
        data = self._request("/copilot/query?" + params)
        require(type(data.get("page")) is int and data["page"] == page
                and type(data.get("has_next")) is bool
                and type(data.get("total")) is int and data["total"] >= 0,
                "Invalid PRTS pagination.")
        rows = data.get("data")
        require(isinstance(rows, list) and len(rows) <= limit, "Invalid candidate page.")
        if not rows:
            raise PrtsError("empty_result", "No candidates on requested page.")
        # PRTS search also returns video guides, which are not executable JSON.
        candidates = [self._parse(row, canonical)[0] for row in rows
                      if not (isinstance(row, dict) and
                              row.get('type') == 'VIDEO')]
        require(len({c.id for c in candidates}) == len(candidates), "Duplicate copilot IDs.")
        return {"stage": canonical, "page": page, "has_next": data["has_next"],
                "total": data["total"], "candidates": [c.to_dict() for c in candidates]}

    def get(self, copilot_id: int, *, stage: str) -> dict:
        """Fetch only a caller-selected ID, always bound to the requested stage."""
        canonical = self.catalog.resolve(stage)
        if type(copilot_id) is not int or copilot_id <= 0:
            raise PrtsError("input", "copilot_id must be a positive integer.")
        row = self._request(f"/copilot/get/{copilot_id}", full=True)
        candidate, content = self._parse(row, canonical)
        require(candidate.id == copilot_id, "Returned copilot ID differs from requested ID.")
        require(isinstance(content.get("actions"), list) and bool(content["actions"])
                and all(isinstance(action, dict) for action in content["actions"]),
                "Missing or invalid full copilot actions.")
        return content
