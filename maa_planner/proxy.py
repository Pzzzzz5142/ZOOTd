"""No-battle proxy screen evidence, separate from actual Fight results.

MaaCore 6.17.1 treats Fight times=0 as skipping navigation too. Instead run
three Custom chains, with a preflight-only resource overlay implementing the
same startup stops as FightTask. The normal farming process has no overlay.
"""
from __future__ import annotations

from typing import Any

from .capability import _iter_log_records


PROXY_RESOURCE = {
    "MaaHostProxyTerminal": {"baseTask": "StageBegin"},
    **{name: {"action": "Stop", "next": [], "exceededNext": [],
              "onErrorNext": [], "sub": []}
       for name in ("GoLastBattle", "StartButton1", "StartButton2",
                    "MedicineConfirm", "ExpiringMedicineConfirm", "StoneConfirm")},
}


def extract_saved_proxy(log_text: str, stage: str) -> dict[str, Any] | None:
    """Require ordered, completed terminal/navigation/PRTS chains in one suffix.

    Merely entering a stage graph, CLI completion, a historical screenshot, or
    an enabled checkbox on a different stage cannot authorize a fight.
    """
    phase = 0
    active: tuple[str, int] | None = None
    device: str | None = None
    task_ids: list[int] = []
    matched = False
    expected_first = (["MaaHostProxyTerminal"], [stage], ["StageQueue@CheckPrts"])
    for label, value in _iter_log_records(log_text):
        if value.get("taskchain") == "Fight" or value.get("what") == "GameOffline":
            return None
        if label in {"TaskChainError", "TaskChainStopped", "TaskChainFailed",
                     "AllTasksStopped", "SubTaskError"}:
            return None
        if label == "TaskChainStart":
            uuid, task_id = value.get("uuid"), value.get("taskid")
            if (phase >= 3 or active is not None or value.get("taskchain") != "Custom"
                    or not isinstance(uuid, str) or not uuid
                    or type(task_id) is not int or task_id in task_ids
                    or (device is not None and uuid != device)):
                return None
            device = uuid
            active = (uuid, task_id)
            matched = False
            task_ids.append(task_id)
        elif label == "SubTaskCompleted" and active == (value.get("uuid"), value.get("taskid")):
            details = value.get("details", {})
            if not isinstance(details, dict) or value.get("first") != expected_first[phase]:
                continue
            task = details.get("task")
            if phase == 0:
                matched |= task == "MaaHostProxyTerminal"
            elif phase == 1:
                # Core callbacks strip derived task prefixes. Bind selection
                # to the requested chain and its actual OCR result, not a
                # presumed fully qualified task name.
                result = details.get("result", {})
                matched |= (details.get("action") == "ClickSelf"
                            and details.get("algorithm") == "OcrDetect"
                            and isinstance(result, dict)
                            and result.get("text") == stage)
            else:
                result = details.get("result", {})
                matched |= (task == "UsePrtsSuccessCheck"
                            and details.get("action") == "DoNothing"
                            and isinstance(result, dict)
                            and result.get("template") == "UsePrtsSuccess.png")
        elif label == "TaskChainCompleted":
            if active != (value.get("uuid"), value.get("taskid")) or not matched:
                return None
            phase += 1
            active = None
        elif label == "AllTasksCompleted":
            if phase != 3 or active is not None or value.get("uuid") != device:
                return None
            return {"source": "maa-proxy-screen", "stage_code": stage,
                    "uuid": device, "task_ids": task_ids, "consumes_sanity": False}
    return None
