from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Iterable, Literal, Mapping
from zoneinfo import ZoneInfo

from .models import Activity
from .util import isoformat, parse_iso_datetime


SERVER_TIMEZONE = ZoneInfo("Asia/Shanghai")
GAME_DAY_RESET = timedelta(hours=4)


@dataclass(frozen=True)
class GameWeek:
    start_game_day: date
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        return self.start_game_day.isoformat()


@dataclass(frozen=True)
class OccupancyWindow:
    start: datetime
    end: datetime
    sources: tuple[str, ...]
    activity_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": isoformat(self.start),
            "end": isoformat(self.end),
            "sources": list(self.sources),
            "activity_name": self.activity_name,
        }


def game_week(now: datetime) -> GameWeek:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(SERVER_TIMEZONE)
    game_day = (local - GAME_DAY_RESET).date()
    monday = game_day - timedelta(days=game_day.weekday())
    start = datetime.combine(monday, time(4), tzinfo=SERVER_TIMEZONE)
    return GameWeek(monday, start, start + timedelta(days=7))


def _overlaps(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> bool:
    return left_start < right_end and right_start < left_end


def _clip_to_week(
    start: datetime, end: datetime, week: GameWeek
) -> tuple[datetime, datetime] | None:
    clipped_start = max(start.astimezone(UTC), week.start.astimezone(UTC))
    clipped_end = min(end.astimezone(UTC), week.end.astimezone(UTC))
    if clipped_start >= clipped_end:
        return None
    return clipped_start, clipped_end


def _source_occupancy(
    *,
    week: GameWeek,
    activities: Iterable[Activity],
    client: str,
) -> tuple[list[OccupancyWindow], list[str]]:
    activities = [
        item
        for item in activities
        if item.client == client
        and _overlaps(item.start, item.end, week.start, week.end)
    ]
    occupancy: list[OccupancyWindow] = []

    for activity in activities:
        clipped = _clip_to_week(activity.start, activity.end, week)
        if clipped is not None:
            occupancy.append(
                OccupancyWindow(*clipped, ("maa",), activity.name)
            )

    occupancy.sort(key=lambda item: (item.start, item.end, item.activity_name))
    return occupancy, []


def _window_is_free(
    start: datetime,
    end: datetime,
    occupancy: Iterable[OccupancyWindow],
) -> bool:
    return not any(
        _overlaps(start, end, occupied.start, occupied.end)
        for occupied in occupancy
    )


def _full_week_is_covered(
    week: GameWeek, occupancy: Iterable[OccupancyWindow]
) -> bool:
    cursor = week.start.astimezone(UTC)
    end = week.end.astimezone(UTC)
    for occupied in occupancy:
        if occupied.end <= cursor:
            continue
        if occupied.start > cursor:
            return False
        cursor = max(cursor, occupied.end)
        if cursor >= end:
            return True
    return cursor >= end


def _morning_slots(week: GameWeek) -> list[datetime]:
    return [
        datetime.combine(
            week.start_game_day + timedelta(days=offset),
            time(6, 0),
            tzinfo=SERVER_TIMEZONE,
        ).astimezone(UTC)
        for offset in range(7)
    ]


def valid_annihilation_state(
    state: object,
    *,
    client: str,
    account: str,
    week: GameWeek,
) -> Mapping[str, Any] | None:
    if not isinstance(state, dict):
        return None

    if state.get("schema_version") == 2:
        if (
            state.get("kind") != "annihilation-operator-confirmation"
            or state.get("client") != client
            or state.get("account") != account
            or state.get("week_start_game_day") != week.key
            or state.get("week_start") != isoformat(week.start)
            or state.get("week_end") != isoformat(week.end)
            or state.get("status") != "complete"
            or "current" in state
            or "total" in state
            or "observed_at" in state
        ):
            return None
        confirmed_at_raw = state.get("confirmed_at")
        if state.get("completed_at") != confirmed_at_raw:
            return None
        try:
            confirmed_at = parse_iso_datetime(confirmed_at_raw).astimezone(UTC)
        except (TypeError, ValueError):
            return None
        if not week.start.astimezone(UTC) <= confirmed_at < week.end.astimezone(UTC):
            return None
        evidence = state.get("evidence")
        if not isinstance(evidence, dict):
            return None
        reason = evidence.get("reason")
        previous_state_sha256 = evidence.get("previous_state_sha256")
        if (
            evidence.get("source") != "operator-confirmation"
            or evidence.get("assertion") != "weekly-annihilation-complete"
            or evidence.get("actor") != "local-operator"
            or not isinstance(reason, str)
            or not reason
            or reason != reason.strip()
            or len(reason) > 512
            or any(ord(character) < 32 for character in reason)
            or not isinstance(evidence.get("confirmation_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", evidence["confirmation_id"]) is None
            or (
                previous_state_sha256 is not None
                and (
                    not isinstance(previous_state_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", previous_state_sha256) is None
                )
            )
        ):
            return None
        return state

    if state.get("schema_version") != 1:
        return None
    if (
        state.get("client") != client
        or state.get("account") != account
        or state.get("week_start_game_day") != week.key
    ):
        return None
    current = state.get("current")
    total = state.get("total")
    if (
        isinstance(current, bool)
        or not isinstance(current, int)
        or current < 0
        or current > 1_000_000
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total <= 0
        or total > 1_000_000
        or current > total
    ):
        return None
    status = state.get("status")
    if status == "complete" and current != total:
        return None
    if status in {"progress", "unstable"} and current >= total:
        return None
    if status not in {"complete", "progress", "unstable"}:
        return None
    try:
        observed_at = parse_iso_datetime(state.get("observed_at")).astimezone(UTC)
    except (TypeError, ValueError):
        return None
    if not week.start.astimezone(UTC) <= observed_at < week.end.astimezone(UTC):
        return None
    if state.get("week_start") != isoformat(week.start) or state.get(
        "week_end"
    ) != isoformat(week.end):
        return None
    completed_at = state.get("completed_at")
    if status == "complete":
        try:
            parsed_completed_at = parse_iso_datetime(completed_at).astimezone(UTC)
        except (TypeError, ValueError):
            return None
        if not week.start.astimezone(UTC) <= parsed_completed_at < week.end.astimezone(
            UTC
        ):
            return None
    elif completed_at is not None:
        return None
    evidence = state.get("evidence")
    if not isinstance(evidence, dict):
        return None
    task_id = evidence.get("task_id")
    since_byte = evidence.get("since_byte")
    log_device = evidence.get("log_device")
    log_inode = evidence.get("log_inode")
    log_was_missing = evidence.get("log_was_missing")
    valid_log_cursor = (
        isinstance(since_byte, int)
        and not isinstance(since_byte, bool)
        and since_byte >= 0
        and isinstance(log_was_missing, bool)
        and (
            (
                log_was_missing
                and since_byte == 0
                and log_device is None
                and log_inode is None
            )
            or (
                not log_was_missing
                and isinstance(log_device, int)
                and not isinstance(log_device, bool)
                and log_device >= 0
                and isinstance(log_inode, int)
                and not isinstance(log_inode, bool)
                and log_inode > 0
            )
        )
    )
    if (
        evidence.get("source") != "maa-annihilation-log"
        or evidence.get("current") != current
        or evidence.get("total") != total
        or isinstance(evidence.get("stars"), bool)
        or not isinstance(evidence.get("stars"), int)
        or evidence.get("stars") not in {0, 2, 3}
        or not isinstance(evidence.get("complete"), bool)
        or evidence.get("complete") != (current == total)
        or evidence.get("completed_by")
        not in {"TaskChainCompleted", "AllTasksCompleted"}
        or not isinstance(evidence.get("uuid"), str)
        or not evidence["uuid"]
        or isinstance(task_id, bool)
        or not isinstance(task_id, (int, str))
        or (isinstance(task_id, str) and not task_id)
        or not isinstance(evidence.get("log_suffix_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", evidence["log_suffix_sha256"])
        or not valid_log_cursor
    ):
        return None
    stars = evidence.get("stars")
    if status == "unstable" and stars != 2:
        return None
    if status == "progress" and stars == 2:
        return None
    return state


def operator_annihilation_confirmation(
    *,
    now: datetime,
    client: str,
    account: str,
    expected_week_start_game_day: str,
    reason: str,
    confirmation_id: str,
    previous_state_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a week-scoped operator assertion without inventing client progress."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(client, str) or not client or client != client.strip():
        raise ValueError("client must be a non-empty trimmed string")
    if not isinstance(account, str) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", account
    ) is None:
        raise ValueError("invalid account")
    if not isinstance(expected_week_start_game_day, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", expected_week_start_game_day
    ) is None:
        raise ValueError("invalid expected game-week key")
    week = game_week(now)
    if expected_week_start_game_day != week.key:
        raise ValueError(
            "operator confirmation is not for the current game week "
            f"({expected_week_start_game_day} -> {week.key})"
        )
    if (
        not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
        or len(reason) > 512
        or any(ord(character) < 32 for character in reason)
    ):
        raise ValueError("reason must be a non-empty trimmed printable string")
    if not isinstance(confirmation_id, str) or re.fullmatch(
        r"[0-9a-f]{32}", confirmation_id
    ) is None:
        raise ValueError("confirmation_id must be 32 lowercase hexadecimal characters")
    if previous_state_sha256 is not None and (
        not isinstance(previous_state_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", previous_state_sha256) is None
    ):
        raise ValueError("previous_state_sha256 must be a SHA-256 digest")

    confirmed_at = isoformat(now)
    payload = {
        "schema_version": 2,
        "kind": "annihilation-operator-confirmation",
        "client": client,
        "account": account,
        "week_start_game_day": week.key,
        "week_start": isoformat(week.start),
        "week_end": isoformat(week.end),
        "status": "complete",
        "confirmed_at": confirmed_at,
        "completed_at": confirmed_at,
        "evidence": {
            "source": "operator-confirmation",
            "assertion": "weekly-annihilation-complete",
            "actor": "local-operator",
            "reason": reason,
            "confirmation_id": confirmation_id,
            "previous_state_sha256": previous_state_sha256,
        },
    }
    if (
        valid_annihilation_state(payload, client=client, account=account, week=week)
        is None
    ):
        raise ValueError("generated operator confirmation failed validation")
    return payload


def plan_annihilation(
    *,
    now: datetime,
    activities: Iterable[Activity],
    client: str,
    account: str,
    state: object = None,
    source_available: bool = True,
    source_error: str | None = None,
    execution_budget: timedelta = timedelta(hours=2),
    transaction_timeout: timedelta = timedelta(minutes=30),
    max_transactions_per_run: int = 10,
    medicine_expire_days: int = 2,
) -> dict[str, Any]:
    """Plan weekly Annihilation without granting execution to an LLM."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if execution_budget <= timedelta(0):
        raise ValueError("execution_budget must be positive")
    if transaction_timeout < timedelta(minutes=10):
        raise ValueError("transaction_timeout must be at least ten minutes")
    if transaction_timeout > min(execution_budget, timedelta(hours=1)):
        raise ValueError(
            "transaction_timeout must not exceed the execution budget or one hour"
        )
    if not 1 <= max_transactions_per_run <= 20:
        raise ValueError("max_transactions_per_run must be between 1 and 20")
    if medicine_expire_days != 2:
        raise ValueError("medicine_expire_days must be 2")
    now = now.astimezone(UTC)
    week = game_week(now)
    deadline = week.end.astimezone(UTC)
    budget_seconds = int(execution_budget.total_seconds())
    transaction_seconds = int(transaction_timeout.total_seconds())

    def bounded_execution_end(start: datetime) -> datetime:
        return min(start + execution_budget, deadline)

    saved = valid_annihilation_state(
        state, client=client, account=account, week=week
    )
    base: dict[str, Any] = {
        "schema_version": 1,
        "kind": "annihilation-plan",
        "generated_at": isoformat(now),
        "client": client,
        "account": account,
        "week_start_game_day": week.key,
        "week_start": isoformat(week.start),
        "week_end": isoformat(week.end),
        "stage": "Annihilation",
        "medicine": 0,
        "medicine_expire_days": medicine_expire_days,
        "stone": 0,
        "times_per_transaction": 1,
        "series": 1,
        "max_transactions_per_run": max_transactions_per_run,
        "execution_budget_seconds": budget_seconds,
        "transaction_timeout_seconds": transaction_seconds,
        "authorization": "game-client-weekly-annihilation",
    }

    if saved is not None and saved["status"] == "complete":
        evidence = saved.get("evidence")
        operator_confirmed = (
            isinstance(evidence, Mapping)
            and evidence.get("source") == "operator-confirmation"
        )
        return {
            **base,
            "decision": "COMPLETE",
            "reason": (
                "OPERATOR_WEEKLY_CAP_CONFIRMED"
                if operator_confirmed
                else "CLIENT_WEEKLY_CAP_RECORDED"
            ),
            "due_at": None,
            "execute_before": None,
            "evidence": {
                "execution_budget_seconds": budget_seconds,
                "state": dict(saved),
            },
        }

    if saved is not None and saved["status"] == "unstable":
        return {
            **base,
            "decision": "BLOCKED",
            "reason": "CLIENT_ANNIHILATION_PROXY_UNSTABLE",
            "due_at": None,
            "execute_before": None,
            "evidence": {
                "execution_budget_seconds": budget_seconds,
                "state": dict(saved),
            },
        }

    occupancy, uncertainty = _source_occupancy(
        week=week,
        activities=activities,
        client=client,
    )
    if not source_available:
        uncertainty.append(f"SOURCE_UNAVAILABLE:{source_error or 'unspecified'}")
    uncertainty = sorted(set(uncertainty))
    monday = _morning_slots(week)[0]
    evidence: dict[str, Any] = {
        "source_policy": "maa-stage-activity-v2",
        "source_uncertainty": uncertainty,
        "occupancy": [item.as_dict() for item in occupancy],
        "execution_budget_seconds": budget_seconds,
        "full_week_activity_coverage": _full_week_is_covered(week, occupancy),
        "state": dict(saved) if saved is not None else None,
    }

    if now + transaction_timeout > deadline:
        return {
            **base,
            "decision": "MISSED",
            "reason": "INSUFFICIENT_WEEK_WINDOW",
            "due_at": None,
            "execute_before": None,
            "evidence": evidence,
        }

    # Manual or recovery runs in the final two hours may still finish this week.
    if now >= deadline - timedelta(hours=2):
        return {
            **base,
            "decision": "RUN",
            "reason": "WEEK_DEADLINE_CATCH_UP",
            "due_at": isoformat(now),
            "execute_before": isoformat(deadline),
            "evidence": evidence,
        }

    if evidence["full_week_activity_coverage"]:
        is_due = now >= monday
        return {
            **base,
            "decision": "RUN" if is_due else "WAIT",
            "reason": (
                "FULL_WEEK_ACTIVITY_MONDAY_CATCH_UP"
                if is_due
                else "FULL_WEEK_ACTIVITY_MONDAY"
            ),
            "due_at": isoformat(monday),
            "execute_before": isoformat(
                bounded_execution_end(now if is_due else monday)
            ),
            "evidence": evidence,
        }

    if uncertainty:
        decision: Literal["RUN", "WAIT"] = "RUN" if now >= monday else "WAIT"
        return {
            **base,
            "decision": decision,
            "reason": (
                "SOURCE_UNCERTAIN_MONDAY_CATCH_UP"
                if decision == "RUN"
                else "SOURCE_UNCERTAIN_WAIT_FOR_MONDAY"
            ),
            "due_at": isoformat(monday),
            "execute_before": isoformat(
                bounded_execution_end(now if decision == "RUN" else monday)
            ),
            "evidence": evidence,
        }

    current_end = now + execution_budget
    if current_end <= deadline and _window_is_free(now, current_end, occupancy):
        return {
            **base,
            "decision": "RUN",
            "reason": "CURRENT_WINDOW_HAS_NO_ACTIVITY",
            "due_at": isoformat(now),
            "execute_before": isoformat(current_end),
            "evidence": evidence,
        }

    future_free = [
        slot
        for slot in _morning_slots(week)
        if slot > now
        and slot + execution_budget <= deadline
        and _window_is_free(slot, slot + execution_budget, occupancy)
    ]
    if future_free:
        return {
            **base,
            "decision": "WAIT",
            "reason": "FUTURE_NO_ACTIVITY_WINDOW",
            "due_at": isoformat(future_free[0]),
            "execute_before": isoformat(future_free[0] + execution_budget),
            "evidence": evidence,
        }

    is_due = now >= monday
    return {
        **base,
        "decision": "RUN" if is_due else "WAIT",
        "reason": (
            "NO_SAFE_FREE_SLOT_MONDAY_CATCH_UP"
            if is_due
            else "NO_SAFE_FREE_SLOT_MONDAY"
        ),
        "due_at": isoformat(monday),
        "execute_before": isoformat(
            bounded_execution_end(now if is_due else monday)
        ),
        "evidence": evidence,
    }
