"""Calendar filtering before regular-stage navigation; no device interaction."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .util import fixed_timezone


def regular_stage_availability(stage: str, now: datetime, payload: Any, client: str) -> str:
    if stage == "1-7":
        return "open"
    if stage != "AP-5" or client not in {"Official", "Bilibili"}:
        return "unknown"
    game_day = now.astimezone(ZoneInfo("Asia/Shanghai")) - timedelta(hours=4)
    # AP (粉碎防御) opens Monday, Thursday, Saturday and Sunday.
    if game_day.weekday() in {0, 3, 5, 6}:
        return "open"
    # A fresh calendar is needed to rule out an all-supplies-open event.
    try:
        event = payload[client]["resourceCollection"]
        if not isinstance(event, dict):
            return "unknown"
        if event.get("IsResourceCollection") is False:
            return "closed"
        if event.get("IsResourceCollection") is not True:
            return "unknown"
        offset = event["TimeZone"]
        if type(offset) is not int:
            return "unknown"
        zone = fixed_timezone(offset)
        start = datetime.strptime(event["UtcStartTime"], "%Y/%m/%d %H:%M:%S").replace(tzinfo=zone)
        end = datetime.strptime(event["UtcExpireTime"], "%Y/%m/%d %H:%M:%S").replace(tzinfo=zone) + timedelta(seconds=1)
        if end <= start:
            return "unknown"
        return "open" if start <= now < end else "closed"
    except (KeyError, TypeError, ValueError, OverflowError):
        return "unknown"
