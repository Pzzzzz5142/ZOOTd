"""Generate bounded navigation from game metadata, never from a stage whitelist."""
from __future__ import annotations

from pathlib import Path
import shutil


SPECIAL_PANEL_ROIS = {
    'marker': [45, 150, 225, 45],
    'start': [775, 560, 415, 60],
    'title': [770, 145, 250, 90],
}
STAGE_PANEL_TASKS = ['ZootdStagePanel', 'ZootdSpecialPanel', 'ZootdMapReady']


def special_panel_execution_tasks() -> dict:
    """Adapt native Copilot only after a completed special-panel observation."""
    # MultiCopilotTaskPlugin checks these native ROIs independently of our
    # Custom graph. Keep its exact title check and all native button edges.
    return {'StartButton1': {'roi': SPECIAL_PANEL_ROIS['start']},
            'BattleStartPre': {'roi': SPECIAL_PANEL_ROIS['start']},
            'ClickedCorrectStage': {'roi': SPECIAL_PANEL_ROIS['title']}}


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

    find = ['ZootdStage', 'ZootdZone', 'ZootdMapReady']
    labels = route.get('activity_labels', [route['activity']])
    title_ocr = {'fullMatch': False, 'ocrReplace': [[r'[\s·•・.\-]+', ''], [r'^复刻[:：]?|[:：]?复刻$', '']]}
    tasks = {
        'ZootdNavigate': {'algorithm': 'JustReturn', 'next': ['ZootdEntry']},
        # Mirror StageNavigationTask::swipe_and_find_stage using installed
        # MAA base tasks: retain its OCR corrections, button detection and
        # swipe geometry. Override only target text, edges and finite limits.
        # Word OCR retains Chinese lock text; ASCII OCR can reduce
        # '通关DP-1解锁' to DP-1 and click the locked successor.
        'ZootdStage': {'baseTask': 'ClickStageName', 'text': [code],
                      'isAscii': False, 'specialParams': [],
                      'next': ['ZootdStagePanel', 'ZootdSpecialPanel', 'ZootdStage'],
                      'maxTimes': 6, 'exceededNext': []},
        'ZootdStagePanel': {'baseTask': 'ClickedCorrectStageOrSwipe',
                            'action': 'DoNothing', 'sub': [], 'reduceOtherTimes': [],
                            'next': ['ZootdStageConfirmed', 'ZootdMapReady']},
        'ZootdStageConfirmed': {
            'baseTask': 'ClickedCorrectStage', 'text': [code, code.replace('-', '')],
            'action': 'DoNothing', 'next': [], 'sub': [], 'onErrorNext': [],
            'exceededNext': [], 'fullMatch': True},
        # This detail layout has no map behind its title. Prove its marker
        # and available start button before accepting the exact stage code.
        # These are observations only, including in zero-battle navigation.
        'ZootdSpecialPanel': ocr(['SPECIAL ACCESS CONTENT'], ['ZootdSpecialStart'],
                                 roi=SPECIAL_PANEL_ROIS['marker'], click=False),
        'ZootdSpecialStart': ocr(['开始行动'], ['ZootdSpecialStageConfirmed'],
                                 roi=SPECIAL_PANEL_ROIS['start'], click=False),
        'ZootdSpecialStageConfirmed': ocr([code, code.replace('-', '')], [],
                                          roi=SPECIAL_PANEL_ROIS['title'], click=False),
        'ZootdZone': {**ocr(route['zone_names'], find), 'maxTimes': 1,
                     'exceededNext': ['ZootdStage', 'ZootdMapReady']},
        'ZootdMapReady': {'algorithm': 'JustReturn', 'next': []},
    }
    # The small English strip needs the installed character OCR model.
    # Word OCR repeatedly read it as SPELAccEsSCNTEN on the real panel.
    tasks['ZootdSpecialPanel']['isAscii'] = True
    tasks['ZootdSpecialStart']['ocrReplace'] = [[r'^[+＋]开始行动$', '开始行动']]
    tasks['ZootdSpecialStageConfirmed']['isAscii'] = True
    tasks['ZootdSpecialStageConfirmed']['baseTask'] = 'ClickedCorrectStage'
    # Native CloseAnno can match the special panel's close icon. After the
    # click, retain announcement/home checks and also resume bounded return.
    tasks['StartUp@CloseAnno'] = {
        'baseTask': 'CloseAnno', 'template': 'CloseAnno.png',
        'next': ['StartUp@MainThemes#next', 'StartUp@CloseAnnos#next',
                 'StartUp@ReturnButtons#next']}
    if route.get('has_raid') and not route.get('raid'):
        # The detail panel remembers challenge mode from a previous run.
        # Native recognition/switching finishes before ordinary execution.
        tasks['ZootdStageConfirmed']['next'] = ['NormalConfirm', 'ChangeToNormalDifficulty']
        tasks['ZootdSpecialStageConfirmed']['next'] = ['NormalConfirm', 'ChangeToNormalDifficulty']
    if route.get('raid'):
        tasks.update(raid_preflight_tasks(code))
    # Keep native return recognition first. Some event panels texture the
    # button background: reuse the same MAA arrow template, masking its dark
    # background instead of lowering the native confidence threshold.
    for prefix, following in [('StartUp', ['StartUpBegin']), ('Home', ['Home', 'Home@ReturnButtons'])]:
        fallback = 'Zootd' + prefix + 'TexturedReturn'
        tasks[prefix + '@ReturnButtons'] = {
            'next': [prefix + '@' + name for name in ('ReturnButton', 'FromStageSN', 'FromAnnihilation')] + [fallback]}
        tasks[fallback] = {'algorithm': 'MatchTemplate', 'template': 'Return.png',
                           'maskRange': [65, 255], 'templThreshold': 0.7,
                           'roi': [0, 0, 180, 80], 'action': 'ClickSelf',
                           'postDelay': 700, 'next': following, 'maxTimes': 6, 'exceededNext': []}
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
        if route.get('ap_cost') == 0:
            # Encrypted zero-cost stages hide their code until extra objectives
            # are met. Collect conditions; the available reconstruction button
            # may unlock access but still requires exact detail proof. An
            # anonymous entrance never constitutes target-stage proof.
            tasks.update({
                'ZootdEncryptedEntry': {**ocr(['TOP-SECRET'],
                                             ['ZootdEncryptedConditions', 'ZootdEncryptedRecordPage', 'ZootdEncryptedPanel'],
                                             roi=[0, 440, 1280, 140]),
                                        'isAscii': True, 'fullMatch': False,
                                        'maxTimes': 1, 'exceededNext': []},
                'ZootdEncryptedPanel': {**ocr(['SPECIAL ACCESS CONTENT'], ['ZootdEncryptedConditions'],
                                             roi=SPECIAL_PANEL_ROIS['marker'], click=False), 'isAscii': True},
                'ZootdEncryptedConditions': ocr(['查看条件'], ['ZootdEncryptedRecordPage'],
                                                roi=[30, 550, 225, 100]),
                'ZootdEncryptedRecordPage': {**ocr(['加密实验记录', '解密实验记录', '重构事件'],
                                                  ['ZootdEncryptedReconstruct', 'ZootdEncryptedBlocked'], click=False),
                                              'fullMatch': False},
                'ZootdEncryptedReconstruct': {**ocr(['事件重构'], STAGE_PANEL_TASKS,
                                                   roi=[180, 590, 900, 90]),
                                              'postDelay': 2000, 'maxTimes': 1, 'exceededNext': []},
                'ZootdEncryptedBlocked': {'algorithm': 'JustReturn', 'next': []},
            })
            find.insert(find.index('ZootdMapReady'), 'ZootdEncryptedEntry')
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
        tasks['ZootdEntry'] = {'baseTask': 'ResourceStages',
                               'next': ['ZootdZone', 'ZootdResourceLeft']}
        # Resource collection remembers its horizontal position. Use native
        # swipe geometry to search both directions for the data-owned name.
        tasks['ZootdResourceLeft'] = {
            'baseTask': 'SwipeToTheLeft', 'maxTimes': 3,
            'next': ['ZootdZone', 'ZootdResourceLeft'],
            'exceededNext': ['ZootdZone', 'ZootdResourceRight']}
        tasks['ZootdResourceRight'] = {
            'baseTask': 'SwipeToTheRight', 'maxTimes': 6,
            'next': ['ZootdZone', 'ZootdResourceRight'], 'exceededNext': []}
    else:
        raise ValueError('Unsupported navigation entrance')
    # Older event maps may label the EX selector only as 'EX', rather
    # than printing zoneNameSecond. This is a shared zone type, not a
    # stage/event-specific route. Keep the exact target check afterwards.
    if '-EX-' in code:
        tasks['ZootdZoneTab'] = {'baseTask': 'ClickStageName', 'text': ['EX'],
                                # EX may be a suffix on a dim English footer.
                                # Stage-name HSV filtering erases this label;
                                # restrict raw OCR to the selector bar instead.
                                'roi': [640, 550, 640, 170], 'specialParams': [],
                                'isAscii': False, 'fullMatch': False,
                                'ocrReplace': [['^巨[Xx]$', 'EX'], ['[Ee][Xx]', 'EX']],
                                # Selected tabs remain visible. Re-clicking
                                # their animated/decorative OCR boxes can hit
                                # a neighbor. Search this map after one click.
                                'next': ['ZootdStage', 'ZootdMapReady'], 'maxTimes': 2,
                                'exceededNext': ['ZootdStage', 'ZootdMapReady']}
        # OCR boxes may merge the neighboring icons, making ClickSelf
        # randomly hit another zone. Prefer a glyph-only template box.
        tasks['ZootdZoneTabGlyph'] = {
            'algorithm': 'MatchTemplate', 'template': 'StageZone-EX.png',
            'templThreshold': 0.8, 'maskRange': [180, 255],
            'roi': [640, 550, 640, 170], 'action': 'ClickSelf', 'postDelay': 1000,
            'next': ['ZootdStage', 'ZootdMapReady'], 'maxTimes': 1,
            'exceededNext': ['ZootdStage', 'ZootdMapReady']}
        find.insert(find.index('ZootdMapReady'), 'ZootdZoneTabGlyph')
        find.insert(find.index('ZootdMapReady'), 'ZootdZoneTab')
    if route['kind'] == 'archive' and '-S-' in code:
        # Some older special zones expose only a chess-piece footer selector,
        # with no zone-name text. The isolated glyph avoids adjacent tabs;
        # it only opens the map, never establishes the requested stage.
        tasks['ZootdSpecialZoneRook'] = {
            'algorithm': 'MatchTemplate', 'template': 'StageZone-SpecialRook.png',
            'templThreshold': .8, 'maskRange': [180, 255],
            'roi': [640, 550, 640, 170], 'action': 'ClickSelf', 'postDelay': 1000,
            'next': ['ZootdStage', 'ZootdMapReady'], 'maxTimes': 1,
            'exceededNext': ['ZootdStage', 'ZootdMapReady']}
        find.insert(find.index('ZootdMapReady'), 'ZootdSpecialZoneRook')
    if route.get('locked_texts'):

        tasks['ZootdNavigationLocked'] = ocr(route['locked_texts'], [], click=False)
        for task in tasks.values():
            following = task.get('next', [])
            if 'ZootdStage' in following and 'ZootdNavigationLocked' not in following:
                following.insert(following.index('ZootdStage') + 1, 'ZootdNavigationLocked')
    return tasks


def raid_preflight_tasks(code: str) -> dict:
    """A closed, zero-battle graph; native Copilot also fails closed on runout."""
    return {
        'ZootdRaidPreflight': {
            'baseTask': 'ClickedCorrectStage', 'text': [code, code.replace('-', '')],
            'fullMatch': True, 'action': 'DoNothing', 'sub': [],
            'next': ['ZootdRaidConfirmed', 'ZootdRaidSwitch'],
            'onErrorNext': [], 'exceededNext': []},
        'ZootdRaidConfirmed': {
            'baseTask': 'RaidConfirm', 'action': 'DoNothing', 'sub': [],
            'template': ['NormalDifficulty.png', 'NormalDifficulty-Chapter15.png'],
            'next': [], 'onErrorNext': [], 'exceededNext': []},
        'ZootdRaidSwitch': {
            'baseTask': 'ChangeToRaidDifficulty', 'maxTimes': 3, 'sub': [],
            'template': ['RaidDifficulty.png', 'RaidDifficulty-Chapter15.png'],
            'next': ['ZootdRaidConfirmed', 'ZootdRaidSwitch'],
            'onErrorNext': [], 'exceededNext': []},
        # ProcessTask returns true on empty exceededNext, even if the clicks
        # never changed mode. Requiring the real button after runout makes
        # MultiCopilotTaskPlugin fail before BattleFormationTask instead.
        'ChangeToRaidDifficulty': {'exceededNext': ['RaidConfirm'], 'onErrorNext': []},
    }


def copilot_result_tasks() -> dict:
    """Finish observed unsuccessful result pages for bounded failure classification."""
    tasks = {
        'Copilot@EndOfAction': {
            'next': ['Copilot@StageDrops-Stars-3', 'Copilot@StageDrops-Stars-Adverse',
                     'Copilot@StageDrops-Stars-2', 'Copilot@StageDrops-Stars-0']},
    }
    for stars in (0, 2):
        tasks[f'Copilot@StageDrops-Stars-{stars}'] = {
            'algorithm': 'MatchTemplate', 'template': f'StageDrops-Stars-{stars}.png',
            'templThreshold': 0.8, 'roi': [50, 270, 250, 100], 'action': 'DoNothing',
            'next': ['Copilot@ClickCornerUntilStartButton'],
            'sub': [], 'onErrorNext': [], 'exceededNext': []}
    return tasks


def zero_result_friend_tasks(prefix, following):
    """Decline a result friend prompt only after observing explicit zero stars."""
    return {
        prefix + 'FriendPrompt': {'algorithm': 'OcrDetect', 'action': 'DoNothing',
            'text': ['是否添加为好友'], 'fullMatch': True, 'roi': [850, 245, 430, 45],
            'ocrReplace': [[r'^是否添加.+为好友[?？]?$', '是否添加为好友']],
            'maxTimes': 1, 'next': [prefix + 'FriendCancel']},
        prefix + 'FriendCancel': {'algorithm': 'MatchTemplate', 'action': 'ClickSelf',
            'template': 'StageResult-FriendCancel.png', 'templThreshold': .9,
            'roi': [900, 280, 160, 65], 'maxTimes': 1, 'postDelay': 1000, 'next': following},
    }


def zero_result_recovery_tasks() -> dict:
    """Clear explicit defeat/zero-star pages before native StartUp."""
    tasks = {
        'ZootdRecoverZeroResult': {'algorithm': 'JustReturn',
            'next': ['ZootdRecoverThreeStars', 'ZootdRecoverAdverseStars',
                     'ZootdRecoverTwoStars', 'ZootdRecoverFailure',
                     'ZootdRecoverZeroStars', 'ZootdRecoverNoZero']},
        'ZootdRecoverFailure': {'baseTask': 'FightMissionFailed', 'maxTimes': 1,
            'preDelay': 0, 'postDelay': 1000,
            'next': ['ZootdRecoverZeroStars', 'ZootdRecoverNoZero']},
        'ZootdRecoverNoZero': {'algorithm': 'JustReturn', 'next': []},
        'ZootdRecoverClearZero': {'baseTask': 'ClickCorner', 'maxTimes': 3, 'postDelay': 1000,
            'next': ['ZootdRecoverZeroStars', 'ZootdRecoverNoZero']},
    }
    for name, stars in [('Three', '3'), ('Adverse', 'Adverse'), ('Two', '2'), ('Zero', '0')]:
        tasks[f'ZootdRecover{name}Stars'] = {
            'algorithm': 'MatchTemplate', 'template': f'StageDrops-Stars-{stars}.png',
            'templThreshold': .8, 'roi': [50, 270, 250, 100],
            'action': 'Stop' if stars == '2' else 'DoNothing',
            'next': (['ZootdRecoverFriendPrompt', 'ZootdRecoverClearZero'] if stars == '0' else
                     [] if stars == '2' else ['ZootdRecoverNoZero'])}
    tasks.update(zero_result_friend_tasks('ZootdRecover', ['ZootdRecoverClearZero']))
    for task in tasks.values():
        task.update(sub=[], onErrorNext=[], exceededNext=[])
    return tasks


def install_copilot_result_resources(resource_dir: Path) -> None:
    """Install our zero-star glyph template only in the disposable run overlay."""
    target = resource_dir / 'template'
    target.mkdir(parents=True, exist_ok=True)
    for name in ('StageDrops-Stars-0.png', 'StageResult-FriendCancel.png'):
        shutil.copyfile(Path(__file__).with_name('resources') / name, target / name)


def install_navigation_resources(resource_dir: Path) -> None:
    """Keep navigation UI glyphs in the run overlay, away from live runtime."""
    target = resource_dir / 'template'
    target.mkdir(parents=True, exist_ok=True)
    for name in ('StageZone-EX.png', 'StageZone-SpecialRook.png', 'StageDrops-Stars-0.png',
                 'StageResult-FriendCancel.png'):
        shutil.copyfile(Path(__file__).with_name('resources') / name, target / name)
