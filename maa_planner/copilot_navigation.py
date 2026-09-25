"""Zero-battle archive navigation, isolated under this attempt's resources."""


def archive_tasks(activity: str, marker: str) -> dict:
    def ocr(text, roi, next_tasks, action='ClickSelf'):
        return {'algorithm': 'OcrDetect', 'text': [text], 'roi': roi,
                'action': action, 'postDelay': 700, 'next': next_tasks}

    return {
        'ZootdCopilotArchive': {
            'baseTask': 'StageTheme', 'template': 'StageTheme.png',
            'next': ['ZootdCopilotCollect']},
        'ZootdCopilotCollect': ocr('乐章收录', [980, 0, 300, 170], ['ZootdCopilotList']),
        'ZootdCopilotList': ocr('默认进度', [680, 0, 340, 90], ['ZootdCopilotReset'], 'DoNothing'),
        'ZootdCopilotReset': {
            'algorithm': 'JustReturn', 'action': 'Swipe',
            'specificRect': [1110, 160, 20, 20], 'rectMove': [1110, 610, 20, 20],
            'specialParams': [400], 'postDelay': 300, 'maxTimes': 15,
            'next': ['ZootdCopilotReset'],
            'exceededNext': ['ZootdCopilotActivity', 'ZootdCopilotScan']},
        'ZootdCopilotActivity': ocr(activity, [45, 90, 1140, 590], ['ZootdCopilotEnter']),
        'ZootdCopilotScan': {
            'algorithm': 'JustReturn', 'action': 'Swipe',
            'specificRect': [1110, 600, 20, 20], 'rectMove': [1110, 200, 20, 20],
            'specialParams': [500], 'postDelay': 500, 'maxTimes': 20,
            'next': ['ZootdCopilotActivity', 'ZootdCopilotScan'], 'exceededNext': []},
        'ZootdCopilotEnter': ocr('进入活动', [990, 550, 285, 130], ['ZootdCopilotMap']),
        'ZootdCopilotMap': ocr(marker, [0, 60, 1280, 560], [], 'DoNothing'),
    }
