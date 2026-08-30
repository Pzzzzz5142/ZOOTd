from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .util import atomic_write_json, isoformat, parse_iso_datetime, utc_now


SNAPSHOT_SCHEMA_VERSION = 1
PURE_GOLD_ITEM_ID = "3003"
_DEPOT_WHAT = frozenset({"Depot", "DepotInfo"})


class InventoryError(ValueError):
    """Base class for inventory input that cannot be trusted."""


class InventoryParseError(InventoryError):
    """Raised when no complete, valid Depot result can be extracted."""


class InventoryValidationError(InventoryError):
    """Raised when a snapshot or an item quantity is malformed."""


def _validate_item_id(item_id: object) -> str:
    if not isinstance(item_id, str):
        raise InventoryValidationError("item id must be a string")
    if not item_id or item_id != item_id.strip():
        raise InventoryValidationError("item id must be non-empty and trimmed")
    if len(item_id) > 256 or any(character.isspace() for character in item_id):
        raise InventoryValidationError(f"invalid item id: {item_id!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in item_id):
        raise InventoryValidationError(f"invalid item id: {item_id!r}")
    return item_id


def _validate_items(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise InventoryValidationError("Depot data must be a JSON object")

    result: dict[str, int] = {}
    for raw_item_id, quantity in value.items():
        item_id = _validate_item_id(raw_item_id)
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise InventoryValidationError(
                f"quantity for {item_id!r} must be an integer"
            )
        if quantity < 0:
            raise InventoryValidationError(
                f"quantity for {item_id!r} must be non-negative"
            )
        result[item_id] = quantity
    return result


def _validate_optional_label(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise InventoryValidationError(f"{field} must be a non-empty trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InventoryValidationError(f"{field} contains control characters")
    return value


@dataclass(frozen=True)
class InventorySnapshot:
    """A complete or explicitly incomplete point-in-time Depot observation.

    ``quantity`` deliberately returns ``None`` for an unobserved item.  A caller
    must not silently turn an OCR omission into a zero inventory count.
    """

    items: Mapping[str, int]
    captured_at: datetime
    complete: bool
    client: str | None = None
    account: str | None = None
    source: str = "maa-depot"

    def __post_init__(self) -> None:
        if not isinstance(self.complete, bool):
            raise InventoryValidationError("complete must be a boolean")
        if not isinstance(self.captured_at, datetime) or self.captured_at.tzinfo is None:
            raise InventoryValidationError("captured_at must be timezone-aware")

        source = _validate_optional_label(self.source, "source")
        if source is None:  # source is not optional, but shares label validation.
            raise InventoryValidationError("source must not be null")

        object.__setattr__(self, "items", MappingProxyType(_validate_items(self.items)))
        object.__setattr__(self, "captured_at", self.captured_at.astimezone(UTC))
        object.__setattr__(self, "client", _validate_optional_label(self.client, "client"))
        object.__setattr__(self, "account", _validate_optional_label(self.account, "account"))
        object.__setattr__(self, "source", source)

    def quantity(self, item_id: str) -> int | None:
        """Return an observed quantity, or ``None`` when the item was absent."""

        return self.items.get(_validate_item_id(item_id))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "captured_at": isoformat(self.captured_at),
            "complete": self.complete,
            "client": self.client,
            "account": self.account,
            "source": self.source,
            "items": dict(sorted(self.items.items())),
        }

    @classmethod
    def from_dict(cls, value: object) -> "InventorySnapshot":
        if not isinstance(value, dict):
            raise InventoryValidationError("inventory snapshot must be a JSON object")
        if value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise InventoryValidationError("unsupported inventory snapshot schema")
        for required in ("captured_at", "complete", "source", "items"):
            if required not in value:
                raise InventoryValidationError(f"snapshot is missing {required!r}")

        captured_at = value["captured_at"]
        if not isinstance(captured_at, str):
            raise InventoryValidationError("captured_at must be an ISO-8601 string")
        try:
            parsed_time = parse_iso_datetime(captured_at)
        except (TypeError, ValueError) as error:
            raise InventoryValidationError("invalid captured_at timestamp") from error

        return cls(
            items=value["items"],
            captured_at=parsed_time,
            complete=value["complete"],
            client=value.get("client"),
            account=value.get("account"),
            source=value["source"],
        )


def select_drone_mode(
    snapshot: InventorySnapshot,
    *,
    threshold: int = 150,
) -> tuple[str, int]:
    """Select the static MaaCore drone target from a complete Depot snapshot."""

    if not isinstance(snapshot, InventorySnapshot):
        raise TypeError("snapshot must be an InventorySnapshot")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise InventoryValidationError("drone threshold must be a non-negative integer")
    if not snapshot.complete:
        raise InventoryValidationError("drone policy requires a complete inventory snapshot")
    quantity = snapshot.quantity(PURE_GOLD_ITEM_ID)
    if quantity is None:
        raise InventoryValidationError("Depot did not observe Pure Gold item 3003")
    return ("PureGold" if quantity < threshold else "Money", quantity)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InventoryParseError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


_STRICT_DECODER = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)


def _iter_embedded_json_objects(log_text: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield JSON objects and their same-line log prefixes.

    MaaCore prefixes callback JSON with timestamps and log text, while some
    maa-cli modes pretty-print the object over multiple lines.  Decoding from
    each line's first opening brace handles both without a brace-counting regex.
    """

    cursor = 0
    text_length = len(log_text)
    while cursor < text_length:
        line_end = log_text.find("\n", cursor)
        if line_end < 0:
            line_end = text_length
        object_start = log_text.find("{", cursor, line_end + 1)
        if object_start < 0:
            cursor = line_end + 1
            continue

        prefix = log_text[cursor:object_start]
        try:
            value, object_end = _STRICT_DECODER.raw_decode(log_text, object_start)
        except (json.JSONDecodeError, InventoryParseError):
            # Never retry at a nested brace on the same malformed record: doing
            # so could accidentally accept only the inner ``details`` object.
            cursor = line_end + 1
            continue
        cursor = max(object_end, line_end + 1)
        if isinstance(value, dict):
            yield prefix, value


def _depot_payload(prefix: str, value: dict[str, Any]) -> dict[str, Any] | None:
    what = value.get("what")
    if what in _DEPOT_WHAT:
        details = value.get("details")
        return details if isinstance(details, dict) else None

    # Some callback transports wrap the MaaCore object in their own ``details``.
    child = value.get("details")
    if isinstance(child, dict) and child.get("what") in _DEPOT_WHAT:
        grandchild = child.get("details")
        if isinstance(grandchild, dict):
            return grandchild
        return child

    # A structured maa-cli line may use the event name as its textual prefix and
    # emit only {"done": ..., "data": ...} as JSON.
    if ("DepotInfo" in prefix or "Depot" in prefix) and (
        "done" in value or "data" in value
    ):
        return value
    return None


def _decode_depot_data(value: object) -> dict[str, int]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, InventoryParseError) as error:
            raise InventoryParseError("Depot data contains invalid inner JSON") from error
    else:
        decoded = value
    return _validate_items(decoded)


def extract_depot_snapshot(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    client: str | None = None,
    account: str | None = None,
    source: str = "maa-depot-log",
) -> InventorySnapshot:
    """Extract the last final Depot/DepotInfo callback from maa-cli output.

    Intermediate ``done=false`` reports are ignored.  If the latest final report
    is malformed, this function fails instead of falling back to an older scan.
    """

    if not isinstance(log_text, str):
        raise TypeError("log_text must be a string")

    saw_final = False
    last_items: dict[str, int] | None = None
    last_error: InventoryError | None = None

    for prefix, value in _iter_embedded_json_objects(log_text):
        payload = _depot_payload(prefix, value)
        if payload is None or payload.get("done") is not True:
            continue

        saw_final = True
        try:
            if "data" not in payload:
                raise InventoryParseError("final Depot result has no data field")
            last_items = _decode_depot_data(payload["data"])
            last_error = None
        except InventoryError as error:
            last_items = None
            last_error = error

    if last_error is not None:
        raise InventoryParseError("latest final Depot result is invalid") from last_error
    if not saw_final or last_items is None:
        raise InventoryParseError("no final done=true Depot result found")
    if not last_items:
        raise InventoryParseError("final Depot result contains no observed items")

    return InventorySnapshot(
        items=last_items,
        captured_at=captured_at or utc_now(),
        complete=True,
        client=client,
        account=account,
        source=source,
    )


def save_snapshot(path: str | Path, snapshot: InventorySnapshot) -> None:
    if not isinstance(snapshot, InventorySnapshot):
        raise TypeError("snapshot must be an InventorySnapshot")
    atomic_write_json(Path(path), snapshot.as_dict())


def load_snapshot(path: str | Path) -> InventorySnapshot:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError, InventoryParseError) as error:
        raise InventoryParseError(f"cannot load inventory snapshot: {path}") from error
    return InventorySnapshot.from_dict(value)
