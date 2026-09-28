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
    labels = route.get('activity_labels', [route['activity']])
    title_ocr = {'fullMatch': False, 'ocrReplace': [[r'[\s·•・.\-]+', '']]}
    tasks = {
        'ZootdNavigate': {'algorithm': 'JustReturn', 'next': ['ZootdEntry']},
        # Mirror StageNavigationTask::swipe_and_find_stage using installed
        # MAA base tasks: retain its OCR corrections, button detection and
        # swipe geometry. Override only target text, edges and finite limits.
        'ZootdStage': {'baseTask': 'ClickStageName', 'text': [code],
                      'next': ['ZootdStagePanel', 'ZootdStage'],
                      'maxTimes': 6, 'exceededNext': []},
        'ZootdStagePanel': {'baseTask': 'ClickedCorrectStageOrSwipe',
                            'action': 'DoNothing', 'sub': [], 'reduceOtherTimes': [],
                            'next': ['ZootdStageConfirmed', 'ZootdMapReset']},
        'ZootdStageConfirmed': {
            'baseTask': 'ClickedCorrectStage', 'text': [code, code.replace('-', '')],
            'action': 'DoNothing', 'next': [], 'sub': [], 'onErrorNext': [],
            'exceededNext': [], 'fullMatch': True},
        'ZootdZone': {**ocr(route['zone_names'], find), 'maxTimes': 2,
                     'exceededNext': ['ZootdStage', 'ZootdMapReset']},
        'ZootdMapReset': {'baseTask': 'FullStageNavigation',
                          'next': ['ZootdMapReset'], 'maxTimes': 10,
                          'exceededNext': scan},
        'ZootdMapScan': {'baseTask': 'StageNavigationSlowlySwipeLeft',
                         'next': ['ZootdMapReset'], 'maxTimes': 20, 'exceededNext': []},
    }
    if route['kind'] == 'archive':
        tasks.update({
            'ZootdEntry': {'baseTask': 'StageTheme', 'template': 'StageTheme.png',
                           'next': ['ZootdCollect']},
            'ZootdCollect': ocr(['乐章收录'], ['ZootdList'], roi=[980, 0, 300, 170]),
            'ZootdList': ocr(['默认进度'], ['ZootdListReset'], roi=[680, 0, 340, 90], click=False),
            'ZootdListReset': swipe([1110, 160, 20, 20], [1110, 610, 20, 20],
                                    ['ZootdListReset'], 15, ['ZootdActivity', 'ZootdListScan']),
            'ZootdActivity': {**ocr(labels, ['ZootdEnter'], roi=[45, 90, 1140, 590]), **title_ocr},
            'ZootdListScan': swipe([1110, 600, 20, 20], [1110, 350, 20, 20],
                                   ['ZootdActivity', 'ZootdListScan'], 45, []),
            'ZootdEnter': ocr(['进入活动', '前往章节'], find, roi=[940, 540, 340, 180]),
        })
    elif route['kind'] == 'main':
        chapter = route.get('chapter')
        if type(chapter) is not int:
            raise ValueError('Missing main chapter identity')
        native = [f'Episode{chapter}']
        if route.get('select_normal'):
            native.append('ChapterDifficultyNormal')
        tasks['ZootdEntry'] = {'algorithm': 'JustReturn', 'sub': native, 'next': find}
    elif route['kind'] == 'activity':
        tasks['ZootdEntry'] = {**ocr(labels, ['ZootdStage', 'ZootdZone', 'ZootdActivityEnter']), **title_ocr}
        tasks['ZootdActivityEnter'] = {**ocr(['进入活动', '前往活动', '进入作战'], find),
                                      'maxTimes': 2, 'exceededNext': []}
    elif route['kind'] == 'supplies':
        tasks['ZootdEntry'] = {'baseTask': 'ResourceStages', 'next': ['ZootdZone']}
    else:
        raise ValueError('Unsupported navigation entrance')
    # Older event maps may label the EX selector only as 'EX', rather
    # than printing zoneNameSecond. This is a shared zone type, not a
    # stage/event-specific route. Keep the exact target check afterwards.
    if '-EX-' in code:
        tasks['ZootdZoneTab'] = {'baseTask': 'ClickStageName', 'text': ['EX'],
                                'next': find, 'maxTimes': 2,
                                'exceededNext': ['ZootdStage', 'ZootdMapReset']}
        find.insert(find.index('ZootdMapReset'), 'ZootdZoneTab')
    if route.get('locked_texts'):

        tasks['ZootdNavigationLocked'] = ocr(route['locked_texts'], [], click=False)
        for task in tasks.values():
            following = task.get('next', [])
            if 'ZootdStage' in following and 'ZootdNavigationLocked' not in following:
                following.insert(following.index('ZootdStage') + 1, 'ZootdNavigationLocked')
    return tasks
