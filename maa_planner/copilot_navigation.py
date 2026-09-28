"""Generate bounded navigation from game metadata, never from a stage whitelist."""
from __future__ import annotations


def navigation_tasks(route: dict) -> dict:
    code = route['code']

    def ocr(texts, next_tasks, *, roi=None, click=True):
        return {'algorithm': 'OcrDetect', 'text': texts, 'fullMatch': True,
                'roi': roi or [0, 0, 1280, 720], 'action': 'ClickSelf' if click else 'DoNothing',
                'postDelay': 700, 'next': next_tasks}

    def swipe(start, end, next_tasks, limit, exceeded):
        return {'algorithm': 'JustReturn', 'action': 'Swipe', 'specificRect': start,
                'rectMove': end, 'specialParams': [450], 'postDelay': 400,
                'maxTimes': limit, 'next': next_tasks, 'exceededNext': exceeded}

    find = ['ZootdStage', 'ZootdZone', 'ZootdMapReset']
    scan = ['ZootdStage', 'ZootdMapScan']
    tasks = {
        'ZootdNavigate': {'algorithm': 'JustReturn', 'next': ['ZootdEntry']},
        'ZootdStage': {**ocr([code], ['ZootdStagePanel']), 'isAscii': True,
                      'ocrReplace': [[' ', '']], 'maxTimes': 4, 'exceededNext': []},
        'ZootdStagePanel': {'algorithm': 'MatchTemplate', 'template': 'StartButton1.png',
                            'action': 'DoNothing', 'next': ['ZootdStageConfirmed']},
        'ZootdStageConfirmed': {
            'baseTask': 'ClickedCorrectStage', 'text': [code, code.replace('-', '')],
            'action': 'DoNothing', 'next': [], 'sub': [], 'onErrorNext': [],
            'exceededNext': [], 'fullMatch': True},
        'ZootdZone': {**ocr(route['zone_names'], find), 'maxTimes': 2,
                     'exceededNext': ['ZootdStage', 'ZootdMapReset']},
        'ZootdMapReset': swipe([250, 120, 20, 20], [1050, 120, 20, 20],
                               ['ZootdStage', 'ZootdMapReset'], 10, scan),
        'ZootdMapScan': swipe([1050, 120, 20, 20], [450, 120, 20, 20], scan, 24, []),
    }
    if route['kind'] in ('archive', 'main'):
        tasks.update({
            'ZootdEntry': {'baseTask': 'StageTheme', 'template': 'StageTheme.png',
                           'next': ['ZootdCollect']},
            'ZootdCollect': ocr(['乐章收录'], ['ZootdList'], roi=[980, 0, 300, 170]),
            'ZootdList': ocr(['默认进度'], ['ZootdListReset'], roi=[680, 0, 340, 90], click=False),
            'ZootdListReset': swipe([1110, 160, 20, 20], [1110, 610, 20, 20],
                                    ['ZootdListReset'], 15, ['ZootdActivity', 'ZootdListScan']),
            'ZootdActivity': ocr([route['activity']] if route['kind'] == 'archive' else route['zone_names'],
                                  ['ZootdEnter'], roi=[45, 90, 1140, 590]),
            'ZootdListScan': swipe([1110, 600, 20, 20], [1110, 350, 20, 20],
                                   ['ZootdActivity', 'ZootdListScan'], 45, []),
            'ZootdEnter': ocr(['进入活动', '前往章节'], find, roi=[940, 540, 340, 180]),
        })
    elif route['kind'] == 'activity':
        tasks['ZootdEntry'] = ocr([route['activity']], ['ZootdStage', 'ZootdZone', 'ZootdActivityEnter'])
        tasks['ZootdActivityEnter'] = {**ocr(['进入活动', '前往活动', '进入作战'], find),
                                      'maxTimes': 2, 'exceededNext': []}
    elif route['kind'] == 'supplies':
        tasks['ZootdEntry'] = ocr(['资源收集'], ['ZootdZone'])
    else:
        raise ValueError('Unsupported navigation entrance')
    return tasks
