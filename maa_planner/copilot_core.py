"""One disposable MaaCore process, with raw structured callbacks per attempt."""
from __future__ import annotations

import ctypes as C
import json
import math
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def launch_game(address):
    """Wait for Android's actual launch result; am may exit zero on errors."""
    result = subprocess.run(
        ['/usr/bin/adb', '-s', address, 'shell', 'am', 'start', '-W', '-n',
         'com.hypergryph.arknights/com.u8.sdk.U8UnityContext'],
        capture_output=True, text=True, timeout=60, check=True)
    lines = result.stdout.splitlines()
    if 'Status: ok' not in lines or 'Complete' not in lines or any(
            line.startswith('Error') for line in lines):
        raise RuntimeError('Android did not confirm game launch')


def home_recognized(message, value):
    details = value.get('details', {})
    return (message == 20001 and value.get('taskchain') == 'Custom'
            and value.get('subtask') == 'ProcessTask'
            and details.get('task') == 'Home' and details.get('action') == 'Stop'
            and details.get('algorithm') == 'MatchTemplate'
            and details.get('result', {}).get('template') == 'SwitchTheme@ToggleSettingsMenu.png')


def map_recognized(message, value, code):
    """Require the requested stage in the detail panel, not a map prefix."""
    details = value.get('details', {})
    result = details.get('result', {})
    return (message == 20002 and value.get('taskchain') == 'Custom'
            and value.get('subtask') == 'ProcessTask'
            and details.get('task') == 'ZootdStageConfirmed'
            and details.get('algorithm') == 'OcrDetect'
            and details.get('action') == 'DoNothing'
            and result.get('text') in (code, code.replace('-', '')))


def special_panel_complete(events, *, task_id, code):
    """Require marker, available start and exact title in one finished Custom."""
    active = None
    observed = 0
    completed = finished = False
    expected = [('ZootdSpecialPanel', ('SPECIAL ACCESS CONTENT',)),
                ('ZootdSpecialStart', ('开始行动',)),
                ('ZootdSpecialStageConfirmed', (code, code.replace('-', '')))]
    for event in events:
        msg, value = event['message'], event['details']
        if (msg in (0, 1, 10000, 10004, 20000, 20004)
                or value.get('what') in ('GameOffline', 'Disconnect', 'Reconnecting', 'ExceededLimit')
                or value.get('taskchain') in ('Fight', 'Copilot')):
            return False
        if (msg == 2 and active is not None and not finished
                and value.get('uuid') == active[0]
                and value.get('what') in ('ScreencapCost', 'EmulatorFPS')
                and set(value) == {'uuid', 'what', 'details'}
                and isinstance(value['details'], dict)):
            continue
        key = (value.get('uuid'), value.get('taskid'))
        if (value.get('taskchain') != 'Custom' or type(key[1]) is not int or key[1] != task_id
                or not isinstance(key[0], str) or not key[0]):
            return False
        if msg == 10001:
            if active is not None:
                return False
            active = key
        elif key != active or finished:
            return False
        if completed and msg != 3:
            return False
        detail = value.get('details', {})
        if msg == 20002 and detail.get('task') in {name for name, _ in expected}:
            if (observed >= len(expected) or value.get('first') != ['ZootdNavigate']
                    or value.get('subtask') != 'ProcessTask'
                    or detail.get('task') != expected[observed][0]
                    or detail.get('algorithm') != 'OcrDetect' or detail.get('action') != 'DoNothing'
                    or detail.get('result', {}).get('text') not in expected[observed][1]):
                return False
            observed += 1
        if msg == 10002:
            if observed != len(expected) or completed:
                return False
            completed = True
        if msg == 3:
            if (not completed or value.get('finished_tasks') != [task_id]
                    or any(type(t) is not int for t in value['finished_tasks'])):
                return False
            finished = True
    return finished


def callbacks_are_fresh(events, *, run_id, started_ns, finished_ns):
    if (not isinstance(events, list) or not events or not isinstance(run_id, str) or not run_id
            or type(started_ns) is not int or type(finished_ns) is not int
            or not 0 < started_ns <= finished_ns):
        return False
    previous = started_ns
    for index, event in enumerate(events):
        if (not isinstance(event, dict) or event.get('run_id') != run_id
                or type(event.get('sequence')) is not int or event['sequence'] != index
                or type(event.get('recorded_ns')) is not int
                or not previous <= event['recorded_ns'] <= finished_ns
                or type(event.get('message')) is not int
                or not isinstance(event.get('details'), dict)
                or not isinstance(event['details'].get('details', {}), dict)):
            return False
        previous = event['recorded_ns']
    return True


def raid_recognized(message, value, *, tasks=('RaidConfirm', 'Copilot@RaidConfirm')):
    """MAA's native confirmation observes the button for returning to normal."""
    details = value.get('details', {})
    result = details.get('result', {})
    return (message == 20002 and value.get('subtask') == 'ProcessTask'
            and details.get('task') in tasks
            and details.get('algorithm') == 'MatchTemplate'
            and details.get('action') == 'DoNothing'
            and isinstance(result, dict)
            and result.get('template') in ('NormalDifficulty.png', 'NormalDifficulty-Chapter15.png')
            and type(result.get('score')) in (int, float)
            and math.isfinite(result['score']) and 0 < result['score'] <= 1)


def raid_preflight_complete(events, *, task_id, code):
    """Authorize dispatch only after target + mode in one completed Custom."""
    active = None
    stage = confirmed = completed = finished = False
    for event in events:
        msg, value = event['message'], event['details']
        if (msg in (0, 1, 10000, 10004, 20000, 20004)
                or value.get('what') in ('GameOffline', 'Disconnect', 'Reconnecting', 'ExceededLimit')
                or value.get('taskchain') in ('Fight', 'Copilot')):
            return False
        # Native device statistics have no task identity and can arrive between
        # Custom callbacks. They cannot establish stage, mode or completion.
        if (msg == 2 and active is not None and not finished
                and value.get('uuid') == active[0]
                and value.get('what') in ('ScreencapCost', 'EmulatorFPS')
                and set(value) == {'uuid', 'what', 'details'}
                and isinstance(value['details'], dict)):
            continue
        key = (value.get('uuid'), value.get('taskid'))
        if (value.get('taskchain') != 'Custom' or type(key[1]) is not int or key[1] != task_id
                or not isinstance(key[0], str) or not key[0]):
            return False
        if msg == 10001:
            if active is not None:
                return False
            active = key
        elif key != active or finished:
            return False
        if completed and msg != 3:
            return False
        detail = value.get('details', {})
        if msg == 20002:
            if completed or value.get('first') != ['ZootdRaidPreflight']:
                return False
            if (detail.get('task') == 'ZootdRaidPreflight'
                    and value.get('subtask') == 'ProcessTask'
                    and detail.get('algorithm') == 'OcrDetect' and detail.get('action') == 'DoNothing'
                    and detail.get('result', {}).get('text') in (code, code.replace('-', ''))):
                stage = True
            if confirmed:
                return False
            if raid_recognized(msg, value, tasks=('ZootdRaidConfirmed',)):
                if not stage:
                    return False
                confirmed = True
        if msg == 10002:
            if completed or not stage or not confirmed:
                return False
            completed = True
        if msg == 3:
            if (not completed or value.get('finished_tasks') != [task_id]
                    or any(type(t) is not int for t in value['finished_tasks'])):
                return False
            finished = True
    return finished


def two_star_recognized(message, value):
    details = value.get('details', {})
    result = details.get('result', {})
    return (message == 20002 and value.get('subtask') == 'ProcessTask'
            and value.get('first') == ['Copilot@WaitUntilEndOfAction']
            and details.get('task') in ('StageDrops-Stars-2', 'Copilot@StageDrops-Stars-2')
            and details.get('algorithm') == 'MatchTemplate'
            and details.get('action') == 'DoNothing' and isinstance(result, dict)
            and result.get('template') == 'StageDrops-Stars-2.png'
            and type(result.get('score')) in (int, float)
            and math.isfinite(result['score']) and 0.8 <= result['score'] <= 1)


def formation_auxiliary(message, value, *, device_uuid):
    """Recognize native default-ID formation UI helpers, never task evidence."""
    detail = value.get('details', {})
    if not (isinstance(device_uuid, str) and bool(device_uuid)
            and message in (20001, 20002) and value.get('uuid') == device_uuid
            and value.get('taskchain') == 'Copilot'
            and type(value.get('taskid')) is int and value['taskid'] == 0
            and value.get('subtask') == 'ProcessTask' and value.get('class') == 'asst::ProcessTask'
            and not value.get('why') and not value.get('what')):
        return False
    task, first, previous = detail.get('task'), value.get('first'), value.get('pre_task')
    action, algorithm, result = detail.get('action'), detail.get('algorithm'), detail.get('result')
    if task in {'BattleQuickFormationSkill-SwipeToTheDown', 'SupportList-MoveToHead', 'SupportList-MoveRight'}:
        return (first == [task] and previous == '' and action == 'Swipe'
                and algorithm == 'JustReturn' and result == {})
    refresh = ['SupportList-RefreshAfterCooldown']
    if task == 'SupportList-RefreshAfterCooldown':
        return (first == refresh and previous == '' and action == 'DoNothing'
                and algorithm == 'JustReturn' and result == {})
    if task == 'Stop':
        return (message == 20001 and first == refresh and previous == 'SupportList-Refresh'
                and action == 'Stop' and algorithm == 'JustReturn' and result == {})
    if (algorithm != 'MatchTemplate' or not isinstance(result, dict)
            or type(result.get('score')) not in (int, float)
            or not math.isfinite(result['score']) or not 0 < result['score'] <= 1):
        return False
    if task == 'SupportList-Refresh':
        return (first == refresh and previous == 'SupportList-RefreshAfterCooldown'
                and action == 'ClickSelf' and result.get('template') == 'SupportList-Refresh.png')
    if task in ('SupportList-DetailPanel-Flag', 'SupportList-DetailPanel-Confirm'):
        expected_first = ([task, task + '@LoadingText'] if task.endswith('-Flag') else [task])
        return (first == expected_first and previous == ''
                and action == ('DoNothing' if task.endswith('-Flag') else 'ClickSelf')
                and result.get('template') == 'SupportList-DetailPanel-Flag.png')
    if task in ('SupportList-SelectRole', 'SupportList-RoleSelected'):
        for role in ('Pioneer', 'Warrior', 'Tank', 'Sniper', 'Caster', 'Medic', 'Support', 'Special'):
            selected, select = f'{role}@SupportList-RoleSelected', f'{role}@SupportList-SelectRole'
            if first == [selected, select]:
                return (previous in ('', select) and result.get('template') == f'{role}@{task}.png'
                        and action == ('ClickSelf' if task == 'SupportList-SelectRole' else 'DoNothing'))
    return False


def terminal_result(events: list[dict], *, task_id: int, stage: str, filename: str, raid=False) -> dict:
    active = None
    loaded = formed = battled = completed = all_done = False
    raid_confirmed = forming = False
    errors = []
    for event in events:
        msg, value = event['message'], event['details']
        if msg in (0, 1, 10000, 10004) or value.get('what') == 'GameOffline':
            errors.append(value.get('what', str(msg)))
        if msg == 10001 and value.get('taskchain') == 'Copilot':
            if active is not None or value.get('taskid') != task_id or not value.get('uuid'):
                errors.append('unexpected_copilot_chain')
            active = (value.get('uuid'), value.get('taskid'))
        bound = (active is not None and active == (value.get('uuid'), value.get('taskid'))
                 and value.get('taskchain') == 'Copilot' and not completed)
        detail = value.get('details', {})
        if bound and battled and two_star_recognized(msg, value):
            errors.append('non_three_star_result')
        if bound and msg == 20003 and value.get('what') == 'CopilotListLoadTaskFileSuccess':
            loaded = detail.get('stage_name') == stage and detail.get('file_name') == filename
        if bound and loaded and not forming and not formed and raid_recognized(msg, value):
            raid_confirmed = True
        if bound and msg == 20001 and value.get('subtask') == 'BattleFormationTask':
            forming = True
        if bound and msg == 20002:
            if value.get('subtask') == 'BattleFormationTask':
                formed = loaded and (not raid or raid_confirmed)
            if value.get('subtask') == 'BattleProcessTask':
                battled = formed
        if bound and msg == 10002:
            completed = loaded and formed and battled
        if msg == 3:
            all_done = completed and task_id in value.get('finished_tasks', [])
    success = all_done and not errors
    return {'status': 'success' if success else 'failed', 'loaded': loaded,
            'raid': raid, 'raid_confirmed': raid_confirmed,
            'formation_completed': formed, 'battle_completed': battled,
            'chain_completed': completed, 'all_tasks_completed': all_done,
            'errors': errors, 'task_id': task_id}


def _worker(root: Path, run: Path, address: str, progress: dict) -> int:
    callback_type = C.CFUNCTYPE(None, C.c_int32, C.c_char_p, C.c_void_p)
    lib = C.CDLL(str(root / 'var/data/lib/libMaaCore.so'))
    signatures = {
        'AsstSetUserDir': ([C.c_char_p], C.c_uint8),
        'AsstLoadResource': ([C.c_char_p], C.c_uint8),
        'AsstCreateEx': ([callback_type, C.c_void_p], C.c_void_p),
        'AsstSetInstanceOption': ([C.c_void_p, C.c_int32, C.c_char_p], C.c_uint8),
        'AsstConnect': ([C.c_void_p, C.c_char_p, C.c_char_p, C.c_char_p], C.c_uint8),
        'AsstAppendTask': ([C.c_void_p, C.c_char_p, C.c_char_p], C.c_int32),
        'AsstStart': ([C.c_void_p], C.c_uint8),
        'AsstRunning': ([C.c_void_p], C.c_uint8),
        'AsstStop': ([C.c_void_p], C.c_uint8),
        'AsstDestroy': ([C.c_void_p], None),
        'AsstAsyncScreencap': ([C.c_void_p, C.c_uint8], C.c_int32),
        'AsstGetImage': ([C.c_void_p, C.c_void_p, C.c_uint64], C.c_uint64),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result

    route = json.loads((run / 'navigation.json').read_bytes())
    proof_context = None
    if (run / 'proof-context.json').exists():
        proof_context = json.loads((run / 'proof-context.json').read_bytes())
    callback_failed = threading.Event()
    mutex = threading.Lock()
    chains = []
    records = []
    sequence = 0
    map_observed = threading.Event()
    home_observed = threading.Event()
    locked_observed = threading.Event()
    encrypted_observed = threading.Event()
    reconstruction_observed = threading.Event()
    with (run / 'callbacks.jsonl').open('x') as output:
        @callback_type
        def callback(msg, raw, _):
            nonlocal sequence
            try:
                value = json.loads(raw)
                with mutex:
                    record = {'run_id': run.name, 'sequence': sequence,
                              'recorded_ns': time.monotonic_ns(), 'message': msg, 'details': value}
                    output.write(json.dumps(record, ensure_ascii=False) + '\n')
                    records.append(record)
                    sequence += 1
                    output.flush()
                    if home_recognized(msg, value):
                        home_observed.set()
                    if (msg == 20002 and value.get('details', {}).get('task') == 'ZootdNavigationLocked'
                            and value.get('details', {}).get('result', {}).get('text') in route.get('locked_texts', [])):
                        locked_observed.set()
                    if map_recognized(msg, value, route['code']):
                        map_observed.set()
                    if (msg == 20002 and value.get('taskchain') == 'Custom'
                            and value.get('subtask') == 'ProcessTask'
                            and value.get('details', {}).get('task') == 'ZootdEncryptedRecordPage'
                            and value['details'].get('algorithm') == 'OcrDetect'
                            and value['details'].get('action') == 'DoNothing'):
                        encrypted_observed.set()
                    if (msg == 20002 and value.get('taskchain') == 'Custom'
                            and value.get('subtask') == 'ProcessTask'
                            and value.get('details', {}).get('task') == 'ZootdEncryptedReconstruct'
                            and value['details'].get('algorithm') == 'OcrDetect'
                            and value['details'].get('action') == 'ClickSelf'
                            and value['details'].get('result', {}).get('text') == '事件重构'):
                        reconstruction_observed.set()
                    if msg in (0, 1, 10000, 10002, 10004):
                        chains.append((msg, value.get('taskid')))
            except Exception:
                callback_failed.set()

        def check(ok):
            if not ok:
                raise RuntimeError('MaaCore operation failed')

        check(lib.AsstSetUserDir(str(run).encode()))
        resources = ('var/data', 'var/data/MaaResource', 'var/data/cache')
        for resource in resources:
            check(lib.AsstLoadResource(str(root / resource).encode()))
        check(lib.AsstLoadResource(str(run / 'navigation').encode()))
        handle = lib.AsstCreateEx(callback, None)
        check(handle)
        # Parent sends TERM on timeout/interruption; stop before releasing lock.
        def cancelled(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, cancelled)
        try:
            for key, value in ((2, b'maatouch'), (3, b'0'), (4, b'0'), (5, b'0')):
                check(lib.AsstSetInstanceOption(handle, key, value))
            progress['phase'] = 'adb'
            check(lib.AsstConnect(handle, b'/usr/bin/adb', address.encode(), b'General'))
            def run_task(kind, params):
                task = lib.AsstAppendTask(handle, kind, params)
                check(task)
                check(lib.AsstStart(handle))
                while lib.AsstRunning(handle):
                    check(not callback_failed.is_set())
                    time.sleep(0.2)
                with mutex:
                    check((10002, task) in chains and not any(m in (0, 1, 10000, 10004) for m, _ in chains))
                check(not callback_failed.is_set())
                return task

            navigation_attempt = 0
            special_panel_observed = False

            def navigate():
                nonlocal navigation_attempt, special_panel_observed
                navigation_attempt += 1
                special_panel_observed = False
                from .copilot_navigation import STAGE_PANEL_TASKS
                from .navigation_vision import scan_map
                import cv2
                import numpy as np

                def overlay(tasks):
                    directory = run / 'navigation-vision-overlay/resource/tasks'
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / 'tasks.json').write_text(json.dumps(tasks))
                    check(lib.AsstLoadResource(str(directory.parent.parent).encode()))

                def capture():
                    check(lib.AsstAsyncScreencap(handle, True))
                    buffer = C.create_string_buffer(8 * 1024 * 1024)
                    size = lib.AsstGetImage(handle, buffer, len(buffer))
                    check(0 < size <= len(buffer))
                    image = cv2.imdecode(np.frombuffer(buffer.raw[:size], dtype=np.uint8), cv2.IMREAD_COLOR)
                    check(image is not None and image.shape[:2] == (720, 1280))
                    return image

                def click_and_confirm(rect):
                    nonlocal special_panel_observed
                    map_observed.clear()
                    special_panel_observed = False
                    overlay({
                        'ZootdNavigate': {'algorithm': 'JustReturn', 'next': ['ZootdVisionClick']},
                        'ZootdVisionClick': {'algorithm': 'JustReturn', 'action': 'ClickRect',
                                            'specificRect': rect, 'postDelay': 700,
                                            'next': STAGE_PANEL_TASKS}})
                    confirm_navigation()
                    return map_observed.is_set()

                def confirm_navigation():
                    nonlocal special_panel_observed
                    with mutex:
                        first = len(records)
                    task_id = run_task(b'Custom', b'{"task_names":["ZootdNavigate"]}')
                    with mutex:
                        special_panel_observed = special_panel_complete(
                            records[first:], task_id=task_id, code=route['code'])
                    if special_panel_observed:
                        map_observed.set()

                def swipe(index):
                    # Reuse installed MAA swipe geometry. Move right once, then
                    # scan left; stop early when consecutive images do not move.
                    base = 'ChapterSwipeToTheRight' if index == 0 else 'StageNavigationSlowlySwipeLeft'
                    overlay({'ZootdVisionSwipe': {'baseTask': base, 'next': [],
                                                 'maxTimes': 1, 'exceededNext': []}})
                    run_task(b'Custom', b'{"task_names":["ZootdVisionSwipe"]}')

                map_observed.clear()
                locked_observed.clear()
                encrypted_observed.clear()
                reconstruction_observed.clear()
                check(lib.AsstLoadResource(str(run / 'navigation').encode()))
                confirm_navigation()
                if locked_observed.is_set() or (encrypted_observed.is_set() and not reconstruction_observed.is_set()):
                    progress['phase'] = 'stage_locked'
                    raise RuntimeError('Stage prerequisite is locked')
                if not map_observed.is_set():
                    if not scan_map([root / path / 'resource' for path in resources], route['code'],
                                    run / f'map-vision-{navigation_attempt}',
                                    capture=capture, click_and_confirm=click_and_confirm, swipe=swipe):
                        progress['phase'] = 'stage_not_found_on_map'
                        raise RuntimeError('Stage not found on map')
                check(map_observed.is_set())

            # Separate task starts prevent a failed navigation from proceeding
            # into Copilot. These tasks have no battle or refill actions.
            progress['phase'] = 'navigation'
            launch_game(address)
            run_task(b'StartUp', b'{"client_type":"Official","start_game_enabled":false}')
            run_task(b'Custom', b'{"task_names":["Home","Home@ReturnButtons"]}')
            check(home_observed.is_set())
            run_task(b'Custom', b'{"task_names":["Terminal-Entry"]}')
            navigate()
            if route.get('navigation_only'):
                progress['phase'] = 'navigation_complete'
                return 0
            if route.get('raid'):
                # Never enqueue a battle until this separate zero-battle task
                # has proved the requested panel and actual challenge mode.
                progress['phase'] = 'raid_preflight'
                with mutex:
                    first = len(records)
                preflight_id = run_task(b'Custom', b'{"task_names":["ZootdRaidPreflight"]}')
                with mutex:
                    confirmed = raid_preflight_complete(records[first:], task_id=preflight_id, code=route['code'])
                check(confirmed)
            if special_panel_observed:
                from .copilot_navigation import special_panel_execution_tasks
                overlay = run / 'special-panel-overlay/resource/tasks'
                overlay.mkdir(parents=True)
                (overlay / 'tasks.json').write_text(json.dumps(special_panel_execution_tasks()))
                check(lib.AsstLoadResource(str(overlay.parent.parent).encode()))
            progress['phase'] = 'execution'
            params = (run / 'params.json').read_bytes()
            task_id = lib.AsstAppendTask(handle, b'Copilot', params)
            check(task_id)
            (run / 'task-id.json').write_text(json.dumps(task_id))
            check(lib.AsstStart(handle))
            while lib.AsstRunning(handle):
                if callback_failed.is_set():
                    raise RuntimeError('Callback recording failed')
                time.sleep(0.2)
            check(not callback_failed.is_set())
            if proof_context:
                # Only after battle: Stop overlays would otherwise prevent it.
                from .copilot_capability import safe_proxy_tasks
                overlay = run / 'proof-overlay/resource/tasks'
                overlay.mkdir(parents=True)
                (overlay / 'tasks.json').write_text(json.dumps(safe_proxy_tasks(proof_context['stage_code'])))
                check(lib.AsstLoadResource(str(run / 'proof-overlay').encode()))
                progress['phase'] = 'proxy_proof'
                home_observed.clear()
                map_observed.clear()
                run_task(b'Custom', b'{"task_names":["Home","Home@ReturnButtons"]}')
                check(home_observed.is_set())
                run_task(b'Custom', b'{"task_names":["Terminal-Entry"]}')
                navigate()
                run_task(b'Custom', b'{"task_names":["ZootdCopilotProofStage"]}')
                run_task(b'Custom', b'{"task_names":["StageQueue@CheckPrts"]}')
            return 0
        finally:
            lib.AsstStop(handle)
            try:
                shot = subprocess.run(['/usr/bin/adb', '-s', address, 'exec-out', 'screencap', '-p'],
                                      capture_output=True, timeout=10, check=True).stdout
                offset = shot.find(b'\x89PNG\r\n\x1a\n')
                if offset >= 0:
                    (run / 'navigation-final.png').write_bytes(shot[offset:])
            except (OSError, subprocess.SubprocessError):
                pass
            lib.AsstDestroy(handle)


def worker(root: Path, run: Path, address: str) -> int:
    progress = {'run_id': run.name, 'phase': 'runtime'}
    try:
        status = _worker(root, run, address, progress)
    except KeyboardInterrupt:
        status = 130
    except Exception:
        status = 1
    # Only safe enum values, never exception strings containing device/account data.
    progress['exit_code'] = status
    with (run / 'worker-result.json').open('x') as output:
        json.dump(progress, output)
    return status


if __name__ == '__main__':
    raise SystemExit(worker(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
