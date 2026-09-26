"""One disposable MaaCore process, with raw structured callbacks per attempt."""
from __future__ import annotations

import ctypes as C
import json
import signal
import sys
import threading
import time
from pathlib import Path


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


def terminal_result(events: list[dict], *, task_id: int, stage: str, filename: str) -> dict:
    active = None
    loaded = formed = battled = completed = all_done = False
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
        if bound and msg == 20003 and value.get('what') == 'CopilotListLoadTaskFileSuccess':
            loaded = detail.get('stage_name') == stage and detail.get('file_name') == filename
        if bound and msg == 20002:
            if value.get('subtask') == 'BattleFormationTask':
                formed = loaded
            if value.get('subtask') == 'BattleProcessTask':
                battled = formed
        if bound and msg == 10002:
            completed = loaded and formed and battled
        if msg == 3:
            all_done = completed and task_id in value.get('finished_tasks', [])
    success = all_done and not errors
    return {'status': 'success' if success else 'failed', 'loaded': loaded,
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
    }
    for name, (args, result) in signatures.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result

    callback_failed = threading.Event()
    mutex = threading.Lock()
    chains = []
    sequence = 0
    with (run / 'callbacks.jsonl').open('x') as output:
        @callback_type
        def callback(msg, raw, _):
            nonlocal sequence
            try:
                value = json.loads(raw)
                with mutex:
                    output.write(json.dumps({'run_id': run.name, 'sequence': sequence,
                                             'recorded_ns': time.monotonic_ns(),
                                             'message': msg, 'details': value}, ensure_ascii=False) + '\n')
                    sequence += 1
                    output.flush()
                    if msg in (0, 1, 10000, 10002, 10004):
                        chains.append((msg, value.get('taskid')))
            except Exception:
                callback_failed.set()

        def check(ok):
            if not ok:
                raise RuntimeError('MaaCore operation failed')

        check(lib.AsstSetUserDir(str(run).encode()))
        for resource in ('var/data', 'var/data/MaaResource', 'var/data/cache'):
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

            # Separate task starts prevent a failed navigation from proceeding
            # into Copilot. These tasks have no battle or refill actions.
            progress['phase'] = 'navigation'
            run_task(b'StartUp', b'{"client_type":"Official","start_game_enabled":true}')
            run_task(b'Custom', b'{"task_names":["Terminal-Entry"]}')
            run_task(b'Custom', b'{"task_names":["ZootdCopilotArchive"]}')
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
            return 0
        finally:
            lib.AsstStop(handle)
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
