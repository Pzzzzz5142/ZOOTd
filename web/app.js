'use strict';
const $ = id => document.getElementById(id);
const labels = {
  success: '已通过',
  failed: '未通过',
  invalid: '证据异常',
  unfinished: '未结束',
  succeeded: '通过',
  'policy-resolved': '策略完成',
  'not-applicable': '无需执行',
  degraded: '降级',
  pending: '尚无记录'
};
const phases = {
  'runtime-readiness': '运行环境',
  'device-readiness': '设备准备',
  depot: '仓库扫描',
  daily: '基建与日常',
  'source-refresh': '来源刷新',
  annihilation: '每周剿灭',
  farming: '材料刷图',
  award: '奖励领取',
  cleanup: '设备清理'
};
const modes = {
  full: '完整托管',
  award: '奖励领取',
  'dry-run': '静态检查',
  device: '设备检查'
};
let state = {
  offset: 0,
  total: 0,
  runs: [],
  filter: 'all',
  selected: null,
  latest: null,
  busy: false,
  detailKey: null,
  view: null,
  historyScroll: 0,
  lastSelected: null,
  upstream: null,
  localVersion: null,
  upstreamBusy: false,
  skland: null,
  sklandBusy: false,
  sklandClock: null,
  sklandAnchor: 0
};
const expanded = new Map();

function node(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (className) n.className = className;
  return n;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}

function resourceRail(ratio) {
  const known = typeof ratio === 'number' && Number.isFinite(ratio);
  const progress = Math.max(0, Math.min(1, known ? ratio : 0));
  const rail = node('div', undefined, 'resource-rail' + (known ? '' : ' unknown'));
  rail.setAttribute('role', 'img');
  rail.setAttribute('aria-label', known ? `进度 ${Math.round(progress * 100)}%` : '进度未知');
  for (let i = 0; i < 10; i++) rail.append(node('span', undefined, i < Math.floor(progress * 10) ? 'filled' : ''));
  return rail;
}

function date(value) {
  return value ? new Date(value).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false
  }) : '—';
}

function renderAttendance(value) {
  const record = value?.record;
  const service = value?.service;
  const interrupted = record?.status === 'running' && service?.available &&
    ['inactive', 'failed'].includes(service.ActiveState);
  const previous = value?.state === 'previous';
  const active = service?.available && ['active', 'activating'].includes(service.ActiveState);
  const unknown = record?.status === 'running' && !service?.available;
  const failedService = service?.ActiveState === 'failed' && (record?.status !== 'ok' || previous);
  const labels = {pending:'等待签到', running:'签到中', succeeded:'签到成功',
    already_signed:'今日已签到', failed:'签到失败', unbound:'未绑定角色'};
  let heading = !record ? '等待早晨日常' : previous ? '上次签到记录' :
    interrupted ? '签到中断' : unknown ? '签到状态未知' : record.status === 'running' ? '正在签到' :
    record.status === 'ok' ? '今日签到完成' : '签到需处理';
  if (active) heading = '正在签到';
  if (value?.error || failedService) heading = '签到需处理';
  $('attendance-state').textContent = heading;
  $('attendance-state').className = 'badge ' +
    (heading === '今日签到完成' ? 'success' : ['签到需处理','签到中断'].includes(heading) ? 'failed' : 'unfinished');
  $('attendance-games').replaceChildren();
  for (const [game, name] of [['arknights','明日方舟'], ['endfield','明日方舟：终末地']]) {
    const result = record?.games?.[game];
    const unconfirmed = (interrupted || unknown) && ['pending','running'].includes(result?.status);
    const status = unconfirmed ? 'failed' : result?.status;
    const card = node('article', undefined, 'attendance-game');
    card.dataset.game = game;
    const head = node('div', undefined, 'resource-heading');
    head.append(node('strong', name), node('span', unconfirmed ? '结果未确认' : previous && status === 'already_signed' ? '当日已签到' : labels[status] || '尚无记录',
      'badge ' + (['succeeded','already_signed'].includes(status) ? 'success' : ['failed','unbound'].includes(status) ? 'failed' : 'unfinished')));
    card.append(head);
    if (result?.rewards?.length) card.append(node('p', result.rewards.map(r => `${r.name} ×${r.count}`).join('、')));
    if (result?.error) card.append(node('p', result.error.message, 'muted'));
    if (result?.checked_at) card.append(node('p', `执行于 ${date(new Date(result.checked_at * 1000))}`, 'card-footnote'));
    $('attendance-games').append(card);
  }
  $('attendance-message').textContent = value?.error?.message || (interrupted ? '上次执行中断，签到结果未确认。' :
    unknown ? '无法确认签到是否仍在执行，请检查签到服务。' :
    failedService ? '签到服务执行失败，请查看两款游戏的结果与服务日志。' :
    previous ? `记录日期 ${record.day}，今天尚无签到结果；等待早晨日常完成。` :
    record?.finished_at ? `最近完成 ${date(new Date(record.finished_at * 1000))}` :
    active || record?.status === 'running' ? '签到正在执行，结果会自动更新。' : '等待下一轮早晨日常完成。');
}

function duration(run) {
  if (!run.finished_at) return '—';
  const s = Math.max(0, Math.round((new Date(run.finished_at) - new Date(run.started_at)) / 1000));
  return s < 60 ? `${s} 秒` : `${Math.floor(s/60)} 分 ${s%60} 秒`;
}

function badge(status) {
  return node('span', labels[status] || status, 'badge ' + status);
}
async function api(path) {
  const r = await fetch(path, {
    signal: AbortSignal.timeout(8000)
  });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function render() {
  $('rows').replaceChildren();
  const query = $('search').value.trim().toLowerCase();
  const visible = state.runs.filter(r => (state.filter === 'all' || (state.filter === 'success' ? r.status === 'success' : r.status !== 'success')) && r.run_id.toLowerCase().includes(query));
  $('empty').hidden = visible.length > 0;
  $('empty').textContent = state.total === 0 ? '暂无运行记录。完成一次托管运行后，记录会显示在这里。' : '当前页没有符合筛选条件的记录。';
  for (const r of visible) {
    const tr = node('tr', undefined, r.run_id === state.lastSelected ? 'selected-run' : '');
    const time = node('td', date(r.started_at));
    time.append(node('small', r.run_id));
    tr.append(time, node('td', modes[r.mode] || '未知'));
    const status = node('td');
    status.append(badge(r.status));
    tr.append(status);
    const progress = node('td');
    if (r.status === 'invalid') progress.append(node('span', '无法核验', 'muted'));
    else {
      const c = counts(r);
      progress.append(node('strong', `已记录 ${c.recorded} / ${c.total}`), segments(r), node('small', `通过 ${c.passed} · 策略 ${c.policy} · 无需 ${c.skipped} · 异常 ${c.problem}`));
    }
    tr.append(progress, node('td', duration(r)));
    const action = node('td'),
      button = node('button', '查看详情 ↗', 'view');
    button.dataset.runId = r.run_id;
    button.addEventListener('click', () => select(r.run_id));
    action.append(button);
    tr.append(action);
    $('rows').append(tr);
  }
  $('success').textContent = `${state.runs.filter(r=>r.status==='success').length} / ${state.runs.length}`;
  $('attention').textContent = state.runs.filter(r => r.status !== 'success').length;
  $('total').textContent = `共 ${state.total} 次运行`;
  $('page').textContent = Math.floor(state.offset / 30) + 1;
  $('prev').disabled = state.offset === 0 || state.busy;
  $('next').disabled = state.offset + 30 >= state.total || state.busy;
}
const accepted = ['succeeded', 'policy-resolved', 'not-applicable'];
const outcomes = {
  'receipt-accepted': '运行环境校验通过',
  'device-ready': '设备连接、分辨率与网络检查完成',
  'inventory-snapshot-ready': '仓库扫描完成，已生成库存快照',
  'inventory-snapshot-reused': '已复用可用的库存快照',
  'protected-daily-complete': '基建、公招与信用商店日常已完成',
  'sources-refreshed': '规划数据来源已刷新',
  'refresh-failed-cache-only': '来源刷新失败，按策略仅使用缓存',
  'planner-network-not-needed': '本轮无需联网刷新规划数据',
  'source-snapshot-missing': '缺少来源快照',
  'isolated-award-complete': '独立奖励领取已完成',
  'resources-released': '设备资源已释放',
  'resource-cleanup-incomplete': '设备资源未完全释放',
  'weekly-cap-confirmed': '已确认本周剿灭奖励达到上限',
  'partial-progress': '本次剿灭取得部分进度',
  waiting: '按排程等待下次剿灭',
  deferred: '本次剿灭已按策略延后',
  blocked: '剿灭被策略阻止',
  'weekly-state-unknown': '本周剿灭状态未知',
  'farming-disabled': '本轮已关闭刷图',
  'inventory-unavailable': '库存不可用',
  'planner-failed': '规划器执行失败',
  'scan-failed': '仓库扫描失败',
  'snapshot-invalid': '库存快照无效',
  'morning-snapshot-unavailable': '早间库存快照不可用',
  'managed-task-contract-invalid': '受管任务契约校验失败',
  'contracts-or-helper-unavailable': '契约或辅助工具不可用',
  'activity-sanity-below-global-minimum': '活动刷图后剩余理智低于最低门槛',
  'no-client-authorized-fight': '没有客户端授权的可执行战斗',
  'no-authorized-fight': '没有已授权的刷图任务',
  'activity-fight-plus-sanity-below-global-minimum': '活动关刷图完成，已用常驻关清理尾数',
  'regular-fallback-sanity-below-global-minimum': '常驻关刷图完成，剩余理智低于 6'
};
const fieldLabels = {
  receipt_mode: '环境校验模式',
  farming_contracts_ready: '刷图契约就绪',
  serial: '设备地址',
  resolution: '分辨率',
  package: '游戏包名',
  network_test_url: '网络检查地址',
  drone_mode: '无人机用途',
  game_day: '游戏日',
  farm_mode: '刷图模式',
  stage: '关卡',
  purpose: '执行用途',
  exit_status: '退出码',
  launcher_started_session: '本轮启动的设备会话'
};
const valueLabels = {
  true: '是',
  false: '否',
  automatic: '自动选关',
  auto: '自动',
  off: '关闭',
  PureGold: '赤金',
  Money: '龙门币'
};

function counts(r) {
  const ps = r.phases || [];
  return {
    total: r.expected_phases?.length || 0,
    recorded: ps.length,
    passed: ps.filter(p => p.result === 'succeeded').length,
    policy: ps.filter(p => p.result === 'policy-resolved').length,
    skipped: ps.filter(p => p.result === 'not-applicable').length,
    problem: ps.filter(p => !accepted.includes(p.result)).length
  };
}

function segments(r) {
  const bar = node('div', undefined, 'segments');
  bar.setAttribute('role', 'img');
  bar.setAttribute('aria-label', (r.expected_phases || []).map(name => {
    const result = r.phases.find(p => p.phase === name)?.result || 'pending';
    return `${phases[name] || name}：${labels[result] || result}`;
  }).join('；'));
  for (const name of r.expected_phases || []) {
    const p = r.phases.find(p => p.phase === name),
      part = node('span', undefined, p?.result || 'pending');
    part.title = `${phases[name]||name}：${labels[p?.result||'pending']||p?.result}`;
    bar.append(part);
  }
  return bar;
}

function disclosure(title, child, key, defaultOpen = false) {
  const d = node('details');
  d.append(node('summary', title), child);
  d.open = expanded.has(key) ? expanded.get(key) : defaultOpen;
  d.addEventListener('toggle', () => {
    if (d.isConnected) expanded.set(key, d.open);
  });
  return d;
}

const phaseHelp = {
  'runtime-readiness': '检查 MAA 与资源版本能否安全执行托管任务。',
  'device-readiness': '准备游戏设备，并检查连接、画面尺寸和网络。',
  depot: '读取库存，为无人机用途和材料选关提供依据。',
  daily: '依次处理设施换班、宿舍、公招和信用商店。',
  'source-refresh': '刷新活动日历、材料掉落和效率数据。',
  annihilation: '按本周排程补齐剿灭奖励，或说明本轮为什么跳过。',
  farming: '按库存缺口选择活动关，最后清理剩余理智。',
  award: '在刷图结束后领取普通任务奖励。',
  cleanup: '释放本轮设备会话和临时资源。'
};
const reasonLabels = {
  ELIGIBLE_STAGE_SELECTED: '按库存缺口、优先级和掉落效率排序',
  CLIENT_WEEKLY_CAP_RECORDED: '客户端已证明本周奖励达到上限',
  OPERATOR_WEEKLY_CAP_CONFIRMED: '本周满额已由人工确认',
  CLIENT_ANNIHILATION_PROXY_UNSTABLE: '剿灭代理不稳定，本周停止自动尝试',
  FUTURE_NO_ACTIVITY_WINDOW: '等待本周后续的活动空窗',
  CURRENT_WINDOW_HAS_NO_ACTIVITY: '当前没有活动冲突，可以执行剿灭',
  FULL_WEEK_ACTIVITY_MONDAY: '活动覆盖整周，安排在周一执行',
  FULL_WEEK_ACTIVITY_MONDAY_CATCH_UP: '活动覆盖整周，本轮补做周一的剿灭',
  SOURCE_UNCERTAIN_WAIT_FOR_MONDAY: '活动来源不确定，等待周一保守排程',
  SOURCE_UNCERTAIN_MONDAY_CATCH_UP: '活动来源不确定，本轮按保守排程补做',
  WEEK_DEADLINE_CATCH_UP: '临近周末，补做尚未完成的剿灭',
  INSUFFICIENT_WEEK_WINDOW: '剩余周窗口不足以安全完成一笔剿灭'
};

function insight(r, name) { return r.insights?.phases?.[name] || {}; }
function phaseOutcome(p) {
  if (!p) return '等待阶段结果';
  return outcomes[p.outcome] || (p.outcome?.startsWith('planner-noop-')
    ? '活动规划本轮没有可执行候选，查看选关说明' : `结果说明：${p.outcome}`);
}
function logsFor(r, name) { return insight(r, name).logs || []; }
function battlesFor(r, name = 'farming') {
  return logsFor(r, name).filter(log => log.kind === 'fight' || log.kind === 'annihilation');
}
function taskCount(logs, task) {
  return logs.reduce((sum, log) => sum + (log.tasks?.[task]?.completed || 0), 0);
}
function dailySummary(r) {
  const logs = logsFor(r, 'daily');
  if (!logs.length) return null;
  const rooms = new Set(logs.flatMap(log => log.rooms || []));
  const recruited = logs.reduce((sum, log) => sum + (log.recruit_confirmed || 0), 0);
  return `访问 ${rooms.size} 间设施 · 确认 ${recruited} 项公招 · 信用商店${taskCount(logs, 'Mall') ? '已结束' : '结果待确认'}`;
}
function farmingSummary(r) {
  const battles = battlesFor(r), reported = battles.filter(log => log.reported_battles > 0);
  if (!battles.length) return null;
  const route = reported.map(log => `${log.stage} ×${log.reported_battles}`).join(' → ');
  const remaining = battles.findLast(log => log.last_sanity != null)?.last_sanity;
  return (route || '日志尚未报告战斗掉落') + (remaining == null ? '' : ` · 剩余 ${remaining} 理智`);
}
function phaseHeadline(r, name) {
  const p = r.phases.find(p => p.phase === name), info = insight(r, name);
  if (name === 'daily' && dailySummary(r)) return dailySummary(r);
  if (name === 'farming' && farmingSummary(r)) return farmingSummary(r);
  if (name === 'depot' && info.inventory) return `已识别 ${info.inventory.observed_items} 项库存 · 赤金 ${info.inventory.pure_gold ?? '未知'}`;
  if (name === 'annihilation' && info.plan) {
    const plan = info.plan;
    const progress = plan.current != null && plan.total != null ? `规划时 ${plan.current} / ${plan.total} · ` : '';
    return progress + (reasonLabels[plan.reason] || phaseOutcome(p));
  }
  if (name === 'source-refresh' && info.activities?.length) return `活动来源已读取 · ${info.activities.join('、')}`;
  if (!p) {
    const latest = (info.logs || []).flatMap(log => log.entries || []).sort((a, b) => a.at.localeCompare(b.at)).at(-1);
    return latest ? `最近日志：${latest.text} · 等待阶段结果` : '尚无结果记录';
  }
  return phaseOutcome(p);
}

function facts(pairs) {
  const dl = node('dl', undefined, 'fields');
  for (const [label, value] of pairs) {
    const pair = node('div');
    pair.append(node('dt', label), node('dd', value == null ? '未知' : String(value)));
    dl.append(pair);
  }
  return dl;
}

function renderPhaseInsights(r, name, body) {
  const info = insight(r, name), logs = info.logs || [];
  if (info.inventory) body.append(facts([
    ['库存采集时间', date(info.inventory.captured_at)], ['识别物品', `${info.inventory.observed_items} 项`],
    ['赤金库存', info.inventory.pure_gold], ['库存用途', '本轮选关与无人机策略；采集后不会实时扣减']
  ]));
  if (name === 'daily' && logs.length) {
    const steps = node('div', undefined, 'task-observations');
    const rooms = [...new Set(logs.flatMap(log => log.rooms || []))];
    const recruit = logs.reduce((n, log) => n + (log.recruit_confirmed || 0), 0);
    const refreshes = logs.reduce((n, log) => n + (log.tag_refreshes || 0), 0);
    for (const [title, text] of [
      ['设施换班', rooms.filter(room => !room.startsWith('宿舍')).join('、') || '日志未记录设施明细'],
      ['宿舍恢复', rooms.filter(room => room.startsWith('宿舍')).join('、') || '日志未记录宿舍明细'],
      ['公开招募', `确认 ${recruit} 项招募，刷新 ${refreshes} 次标签；${taskCount(logs, 'Recruit')} 段任务结束`],
      ['信用商店', taskCount(logs, 'Mall') ? '任务已结束；日志未提供商品明细' : '尚无任务结束记录']
    ]) {
      const step = node('article');
      step.append(node('h4', title), node('p', text));
      steps.append(step);
    }
    body.append(steps);
    if (logs.length > 1) body.append(node('p', `本轮保存了 ${logs.length} 份日常尝试日志，可在下方核对重试过程。`, 'muted'));
  }
  const plan = info.plan;
  if (plan && name === 'annihilation') body.append(facts([
    ['排程原因', reasonLabels[plan.reason] || plan.reason], ['本游戏周', plan.week],
    ['规划时奖励进度', plan.current != null && plan.total != null ? `${plan.current} / ${plan.total}` : '尚无客户端进度'],
    ['计划执行时间', plan.due_at ? date(plan.due_at) : '本轮无需等待新的执行时间']
  ]));
  if (plan && name === 'farming') {
    const chosen = plan.candidates.find(row => row.stage === plan.selected_stage);
    const why = chosen && chosen.deficit > 0
      ? `选择 ${plan.selected_stage}：${chosen.material} 等价库存 ${chosen.inventory ?? '未知'} / ${chosen.target ?? '未知'}，缺口 ${chosen.deficit}。${reasonLabels[plan.reason] || plan.reason}。`
      : `选关说明：${reasonLabels[plan.reason] || plan.reason || '原因未记录'}。`;
    body.append(node('h4', '为什么选这关'), node('p', why, 'selection-explanation'));
    body.append(node('p', `${plan.activity || '活动名称未知'} · 库存采集 ${date(plan.inventory_at)} · 候选顺序 ${(plan.order || []).join(' → ') || '暂无'}`, 'muted'));
    const table = node('table', undefined, 'candidate-table'), head = node('thead'), header = node('tr');
    for (const label of ['关卡 / 材料', '等价库存 / 目标', '缺口', '候选状态']) header.append(node('th', label));
    head.append(header); table.append(head);
    const rows = node('tbody');
    for (const candidate of plan.candidates || []) {
      const row = node('tr'), material = node('td');
      material.append(node('strong', candidate.stage), node('small', candidate.material));
      const inventory = node('td', `${candidate.inventory ?? '未知'} / ${candidate.target ?? '未知'}`);
      if (candidate.craftable > 0) inventory.append(node('small', `蓝材料 ${candidate.direct} + 绿色合成折算 ${candidate.craftable}`));
      row.append(material, inventory, node('td', candidate.deficit ?? '未知'),
        node('td', candidate.stage === plan.selected_stage ? '本轮首选' : candidate.eligible ? '备用候选' : `不可用：${(candidate.rejected_by || []).join('、') || '原因未知'}`));
      rows.append(row);
    }
    table.append(rows);
    const wrap = node('div', undefined, 'table-scroll'); wrap.append(table); body.append(wrap);
  }
  if (name === 'farming') {
    const p = r.phases.find(p => p.phase === name);
    if (p?.outcome?.startsWith('activity-fight-plus-')) {
      body.append(node('p', '为什么还打 1-7：活动关理智不足后，按 AP-5 → 1-7 清理尾数，直到剩余理智低于 6。', 'notice tail-explanation'));
    }
  }
  const battles = battlesFor(r, name);
  if (battles.length) {
    body.append(node('h4', '实际刷图记录'));
    const list = node('div', undefined, 'battle-observations');
    for (const battle of battles) {
      const card = node('article'), head = node('div', undefined, 'resource-heading');
      head.append(node('strong', battle.stage), node('span', battle.reported_battles
        ? `日志报告 ${battle.reported_battles} 场 · 费用 ${battle.sanity_cost} 理智` : '未报告战斗掉落', 'muted'));
      card.append(head, node('p', `理智读数 ${battle.first_sanity ?? '未知'} → ${battle.last_sanity ?? '未知'} · ${battle.complete ? '任务日志正常结束' : '尚无正常结束记录'}`));
      if (battle.drops?.length) card.append(node('p', battle.drops.map(drop => `${drop.name} ×${drop.count}`).join('、'), 'drop-list'));
      list.append(card);
    }
    body.append(list);
  }
  if (logs.length) {
    const entries = logs.flatMap(log => log.entries || []).sort((a, b) => a.at.localeCompare(b.at));
    const warnings = entries.filter(entry => entry.tone === 'warning');
    if (warnings.length) body.append(node('p', `过程提醒：${[...new Set(warnings.map(entry => entry.text))].join('；')}。最终结果见阶段状态。`, 'notice'));
    const list = node('ol', undefined, 'operation-log');
    for (const entry of entries.slice(-100)) {
      const li = node('li', undefined, entry.tone), at = node('time', date(entry.at));
      at.dateTime = entry.at;
      li.append(at, node('span', entry.text)); list.append(li);
    }
    body.append(disclosure(`操作记录 · ${entries.length} 条`, list, `${r.run_id}:${name}:operations`));
    body.append(node('p', '操作摘要来自日志；按本轮时间关联的日志用于解释过程，阶段是否通过以审计终态为准。', 'observation-note'));
  }
  const missing = (info.files || []).filter(file => file.state !== 'verified');
  if (missing.length) body.append(node('p', `部分过程明细${missing.some(file => file.state === 'changed') ? '已变化，与原记录不一致' : '已清理或暂时无法读取'}，保留原始阶段结果。`, 'observation-note'));
}

function runStory(r) {
  const box = node('section', undefined, 'run-story');
  box.append(node('h3', '本轮做了什么'));
  const list = node('dl');
  for (const name of ['daily', 'annihilation', 'farming', 'award']) {
    if (!r.expected_phases.includes(name)) continue;
    const item = node('div');
    item.append(node('dt', phases[name]), node('dd', phaseHeadline(r, name))); list.append(item);
  }
  box.append(list);
  const problems = r.phases.filter(p => !accepted.includes(p.result));
  if (problems.length) box.append(node('p', `需要关注：${problems.map(p => `${phases[p.phase] || p.phase} · ${phaseOutcome(p)}`).join('；')}`, 'notice error'));
  if (r.insights?.logs_truncated) box.append(node('p', '日志较多，仅展示最近 64 份的操作摘要。', 'observation-note'));
  return box;
}

function phaseCard(r, name, index) {
  const p = r.phases.find(p => p.phase === name),
    result = p?.result || 'pending';
  const body = node('div', undefined, 'stage-body');
  body.append(node('p', phaseHelp[name] || '查看此阶段的已记录操作与结果。', 'phase-purpose'));
  renderPhaseInsights(r, name, body);
  if (p) {
    body.append(node('p', phaseOutcome(p), 'outcome'));
    const fields = node('dl', undefined, 'fields');
    for (const [key, value] of Object.entries(p.details || {})) {
      const pair = node('div');
      pair.append(node('dt', fieldLabels[key] || key), node('dd', valueLabels[value] || String(value)));
      fields.append(pair);
    }
    if (fields.childElementCount) body.append(fields);
    else body.append(node('p', '此阶段未记录额外过程字段。', 'muted'));
    if (p.evidence_files?.length) {
      const files = node('ul', undefined, 'evidence-files');
      for (const f of p.evidence_files) {
        const item = node('li');
        item.append(node('code', f.path));
        files.append(item);
      }
      body.append(disclosure(`证据文件 · ${p.evidence_files.length}`, files, `${r.run_id}:${name}:files`));
    }
    body.append(disclosure('原始阶段记录 · JSON', node('pre', JSON.stringify(p, null, 2)), `${r.run_id}:${name}:raw`));
  } else body.append(node('p', r.finished_at ? '本轮已结束，但没有此阶段的终态记录。' : '尚未收到此阶段的终态记录；无法据此判断是否已开始执行。', 'muted'));
  const card = disclosure('', body, `${r.run_id}:${name}`, !!p && (!accepted.includes(p.result) || ['daily', 'farming'].includes(name)));
  card.className = 'stage-card ' + result;
  card.id = 'stage-' + name;
  const summary = card.firstChild;
  const copy = node('span', undefined, 'stage-copy');
  copy.append(node('strong', phases[name] || name), node('span', phaseHeadline(r, name), 'stage-headline'));
  summary.replaceChildren(node('span', String(index + 1).padStart(2, '0'), 'stage-number'), copy, badge(result), node('span', p ? date(p.recorded_at) : '尚无终态时间', 'stage-time'));
  return card;
}

function detail(r) {
  const key = JSON.stringify(r);
  if (state.detailKey === key) return;
  state.detailKey = key;
  $('detail').hidden = false;
  const content = $('detail-content');
  content.replaceChildren();
  const heading = node('div', undefined, 'run-heading');
  heading.append(node('h3', modes[r.mode] || '运行记录'), badge(r.status));
  content.append(heading, node('p', r.run_id, 'run-id'));
  if (r.error) {
    content.append(node('p', r.error, 'notice error'));
    return;
  }
  content.append(runStory(r));
  const c = counts(r),
    metrics = node('div', undefined, 'run-metrics');
  for (const [label, value] of [
      ['已记录 / 应有阶段', `${c.recorded} / ${c.total}`],
      ['通过', c.passed],
      ['策略完成', c.policy],
      ['无需执行', c.skipped],
      ['异常', c.problem],
      ['尚无记录', c.total - c.recorded]
    ]) {
    const m = node('div');
    m.append(node('strong', String(value)), node('span', label));
    metrics.append(m);
  }
  content.append(metrics, segments(r));
  content.append(node('p', `开始 ${date(r.started_at)} · ${r.finished_at?'结束 '+date(r.finished_at)+' · 总耗时 '+duration(r):'最近事件 '+date(r.updated_at)}`, 'muted'));
  const journey = node('div', undefined, 'journey'),
    stagePane = node('section'),
    timeline = node('section', undefined, 'timeline-pane');
  stagePane.append(node('h3', '阶段一览'));
  const nav = node('nav', undefined, 'stage-nav');
  nav.setAttribute('aria-label', '定位阶段');
  const cards = [];
  for (const [i, name] of r.expected_phases.entries()) {
    const card = phaseCard(r, name, i);
    cards.push(card);
    const p = r.phases.find(p => p.phase === name),
      button = node('button', `${i+1} ${phases[name]||name}`, p?.result || 'pending');
    button.setAttribute('aria-label', `${phases[name]||name}，${labels[p?.result||'pending']||p?.result}，展开详情`);
    button.addEventListener('click', () => {
      select.value = 'all';
      filterCards();
      card.open = true;
      expanded.set(`${r.run_id}:${name}`, true);
      card.scrollIntoView({
        block: 'center'
      });
      card.firstChild.focus();
    });
    nav.append(button);
  }
  stagePane.append(nav);
  const filter = node('label', '显示阶段 ', 'stage-filter'),
    select = node('select');
  for (const [value, label] of [
      ['all', '全部阶段'],
      ['attention', '异常和尚无记录'],
      ['recorded', '已有记录']
    ]) {
    const option = node('option', label);
    option.value = value;
    select.append(option);
  }
  const filterKey = r.run_id + ':filter';
  select.value = expanded.get(filterKey) || 'all';
  const empty = node('p', '没有符合条件的阶段。', 'muted');

  function filterCards() {
    let visible = 0;
    cards.forEach((card, i) => {
      const p = r.phases.find(p => p.phase === r.expected_phases[i]);
      card.hidden = select.value === 'attention' ? !!p && accepted.includes(p.result) : select.value === 'recorded' ? !p : false;
      if (!card.hidden) visible++;
    });
    empty.hidden = visible > 0;
    expanded.set(filterKey, select.value);
  }
  select.addEventListener('change', filterCards);
  filter.append(select);
  const controls = node('div', undefined, 'stage-controls');
  controls.append(filter);
  for (const [label, open] of [['展开全部', true], ['收起全部', false]]) {
    const button = node('button', label);
    button.addEventListener('click', () => cards.forEach((card, i) => {
      if (!card.hidden) { card.open = open; expanded.set(`${r.run_id}:${r.expected_phases[i]}`, open); }
    }));
    controls.append(button);
  }
  stagePane.append(controls, ...cards, empty);
  filterCards();
  timeline.append(node('h3', '过程时间线'), node('p', '按事件时间排列 · 北京时间', 'muted'));
  const events = [{
      at: r.started_at,
      title: '本轮开始',
      text: modes[r.mode] || r.mode,
      status: 'start'
    },
    ...r.phases.map(p => ({
      at: p.recorded_at,
      title: phases[p.phase] || p.phase,
      text: phaseHeadline(r, p.phase),
      status: p.result
    })),
    ...(r.recovery || []).map(e => ({
      at: e.recorded_at,
      title: '恢复事件',
      text: ({
        'recovery-started': '恢复已开始',
        'recovery-finished': '恢复已结束'
      })[e.event_type] || e.event_type,
      status: 'recovery'
    }))
  ];
  if (r.finished_at) events.push({
    at: r.finished_at,
    title: '本轮结束',
    text: labels[r.status],
    status: r.status
  });
  events.sort((a, b) => new Date(a.at) - new Date(b.at));
  const list = node('ol', undefined, 'timeline');
  for (const event of events) {
    const item = node('li', undefined, event.status),
      time = node('time', date(event.at));
    time.dateTime = event.at;
    item.append(time, node('strong', event.title), node('p', event.text));
    if (labels[event.status]) item.append(badge(event.status));
    list.append(item);
  }
  timeline.append(list);
  if (r.recovery?.length) timeline.append(node('p', '恢复重跑作为独立运行展示；本轮原始结果保留。', 'notice'));
  journey.append(stagePane, timeline);
  content.append(journey, node('p', `代码版本 ${r.repository?.head?.slice(0,12)||'未知'} · 阶段数按已保存终态统计。事件哈希链已校验；未结束不等于仍在运行。`, 'muted'));
}
function select(id) {
  location.hash = 'run/' + encodeURIComponent(id);
}

function route(initial = false) {
  if (state.view === 'history') state.historyScroll = window.scrollY;
  const match = location.hash.match(/^#run\/([0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{8})$/);
  const view = match ? 'detail' : location.hash === '#history' ? 'history' : 'overview';
  state.view = view;
  state.selected = match ? match[1] : null;
  for (const id of ['overview', 'history', 'detail', 'breadcrumb']) {
    $(id).hidden = id === 'detail' ? !['overview', 'detail'].includes(view) : (id === 'breadcrumb' ? 'detail' : id) !== view;
  }
  $('game-overview').hidden = view !== 'overview';
  const titles = {overview:'运行概览', history:'历史记录', detail:'运行详情'};
  $('page-title').textContent = titles[view];
  document.title = titles[view] + ' · ZOOTd';
  $('page-eyebrow').textContent = 'OPERATIONS / ' + {overview:'OVERVIEW',history:'HISTORY',detail:'RUN DETAIL'}[view];
  $('page-description').textContent = {overview:'核对游戏状态与托管执行结果。',history:'查询每轮任务的结果、阶段记录与执行证据。',detail:'查看本轮阶段结果，沿时间线追踪已记录的过程。'}[view];
  for (const name of ['overview', 'history', 'detail']) {
    const link = $('nav-' + name);
    link.classList.toggle('active', name === view);
    link.classList.toggle('parent-active', name === 'history' && view === 'detail');
    if (name === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  $('nav-detail').hidden = view !== 'detail';
  if (match) $('nav-detail').href = location.hash;
  $('detail-title').textContent = view === 'overview' ? '最近一次执行' : '执行概况';
  $('close').hidden = view !== 'detail';
  $('all-history').hidden = view !== 'overview';
  if (view === 'detail') loadRun(match[1]);
  if (view === 'overview') renderLatest(state.latest);
  if (!initial) {
    window.scrollTo({top:view === 'history' ? state.historyScroll : 0, behavior:'instant'});
    const previous = view === 'history' && state.lastSelected
      ? document.querySelector(`[data-run-id="${state.lastSelected}"]`) : null;
    (previous || $('page-title')).focus({preventScroll:true});
  }
}

function renderLatest(r) {
  if (state.view !== 'overview') return;
  if (r) detail(r);
  else {
    state.detailKey = null;
    $('detail-content').replaceChildren(node('p', '暂无运行记录。', 'muted'));
  }
}

function renderUpstream() {
  const box = $('upstream'), r = state.upstream;
  box.replaceChildren();
  if (!r) { box.append(node('small', '正在查询上游稳定版…')); return; }
  box.append(node('small', r.version ? `${r.state === 'stale' ? '上次查询' : '上游稳定版'}：${r.version}` : '上游版本查询失败'));
  if (r.state === 'fresh') {
    const local = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(state.localVersion || '');
    const upstream = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(r.version || '');
    let message = '本地版本未知或非稳定版，未作比较';
    if (local && upstream) {
      const diff = [1, 2, 3].map(i => Number(local[i]) - Number(upstream[i])).find(n => n !== 0) || 0;
      message = diff < 0 ? '有新的稳定版' : diff === 0 ? '与上游稳定版一致' : '本地版本高于上游稳定版';
    }
    box.append(node('small', message), node('small', `查询于 ${date(r.fetched_at)}`));
  } else box.append(node('small', r.version ? `查询失败，保留 ${date(r.fetched_at)} 的旧结果` : '暂时无法连接上游，请稍后再试'));
  const link = node('a', '查看上游发布说明 ↗', 'view');
  // Use a fixed official destination; remote data is displayed only as text.
  link.href = 'https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases/latest';
  link.target = '_blank'; link.rel = 'noopener noreferrer';
  box.append(link);
}

async function refreshUpstream() {
  if (state.upstreamBusy) return;
  state.upstreamBusy = true;
  try { state.upstream = await api('/api/maa-release'); }
  catch (e) { state.upstream = {...state.upstream, state:state.upstream?.version ? 'stale' : 'unavailable'}; }
  finally { state.upstreamBusy = false; renderUpstream(); }
}

function sklandNow() {
  return state.sklandClock === null ? Date.now() / 1000
    : state.sklandClock + (performance.now() - state.sklandAnchor) / 1000;
}

function countdown(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  const hours = Math.floor(s / 3600), minutes = Math.floor(s % 3600 / 60);
  return [hours, minutes, s % 60].map(n => String(n).padStart(2, '0')).join(':');
}

function sanityAt(ap, now) {
  if (ap?.current == null || ap?.max == null) return null;
  if (ap.current >= ap.max || !ap.full_at) return ap.current;
  // Recovery deadline anchors partial minutes; fetching does not reset recovery.
  return Math.max(ap.current, Math.min(ap.max, ap.max - Math.ceil((ap.full_at - now) / 360)));
}

function dronesAt(drones, now) {
  if (drones?.current == null || drones?.max == null) return null;
  if (drones.current >= drones.max || drones.remaining_seconds == null || !drones.updated_at) return drones.current;
  if (drones.remaining_seconds === 0) return drones.max;
  return Math.min(drones.max, drones.current + Math.floor(Math.max(0, now - drones.updated_at)
    * (drones.max - drones.current) / drones.remaining_seconds));
}

function renderSkland() {
  const r = state.skland, now = sklandNow();
  const old = r?.snapshot && (r.state !== 'fresh' || now - r.fetched_at >= r.refresh_interval_seconds);
  const syncLabel = old ? '旧快照 · 推算中' : {fresh:'森空岛已同步', loading:'正在同步森空岛…', unavailable:'森空岛不可用'}[r?.state] || '正在读取森空岛…';
  $('skland').dataset.state = old ? 'stale' : r?.state || 'loading';
  // Announce status transitions, without repeating the same message every second.
  if ($('skland-state').textContent !== syncLabel) $('skland-state').textContent = syncLabel;
  $('skland-state').className = 'badge ' + (old || r?.error ? 'unfinished' : r?.state === 'fresh' ? 'success' : '');
  $('skland-message').hidden = !r?.error && !old;
  $('skland-message').textContent = r?.error?.message || (old ? '快照已超过同步周期，当前数字为旧数据推算，请等待同步后核对。' : '');
  const snapshot = r?.snapshot, ap = snapshot?.sanity, sanity = sanityAt(ap, now);
  $('sanity-value').replaceChildren(node('strong', sanity === null ? '—' : String(sanity)), node('span', ' / ' + (ap?.max ?? '—')));
  $('sanity-progress').max = ap?.max || 1;
  $('sanity-progress').value = sanity ?? 0;
  $('sanity-progress').classList.toggle('attention', sanity !== null && sanity >= ap.max);
  const sanityRatio = sanity === null ? null : Math.max(0, Math.min(1, sanity / ap.max));
  $('sanity-ring').setAttribute('stroke-dashoffset', ((1 - (sanityRatio ?? 0)) * 540.354).toFixed(3));
  $('sanity-gauge').dataset.state = sanity === null ? 'unknown' : sanity >= ap.max ? 'full' : 'recovering';
  $('sanity-next-time').textContent = '—';
  $('sanity-full-time').textContent = '—';
  $('sanity-percent').textContent = sanity === null ? '恢复进度未知' : `${Math.round(sanityRatio * 100)}% · 自然恢复推算`;
  if (sanity === null) $('sanity-timer').textContent = '理智数据未知';
  else if (sanity >= ap.max) {
    $('sanity-timer').textContent = '理智已满 · 可核对是否正常消耗';
    $('sanity-next-time').textContent = '已达上限';
    $('sanity-full-time').textContent = '无需恢复';
  }
  else if (ap.full_at) {
    const next = ap.full_at - (ap.max - sanity - 1) * 360;
    $('sanity-timer').textContent = `下一点 ${countdown(next - now)} · 回满 ${countdown(ap.full_at - now)}`;
    $('sanity-next-time').textContent = countdown(next - now);
    $('sanity-full-time').textContent = countdown(ap.full_at - now);
  } else $('sanity-timer').textContent = '恢复时间未知，显示接口记录';
  $('sanity-snapshot').textContent = ap?.current == null ? '接口未提供理智时，保留未知状态'
    : `接口记录 ${ap.current} / ${ap.max ?? '—'} · 仅推算自然恢复`;
  const slots = snapshot?.recruit;
  $('recruit-slots').replaceChildren();
  let running = 0, due = 0;
  for (let index = 0; index < 4; index++) {
    const slot = slots?.[index], card = node('div', undefined, 'recruit-slot');
    let progress = null;
    let label = '状态未知', timer = '—', style = '';
    if (slot) {
      // Preserve the raw state for diagnostics. Only a reported deadline implies a countdown.
      if (slot.state === 1) { label = '招募已结束'; timer = '接口已确认'; due++; style = 'attention'; progress = 1; }
      else if (slot.state != null && slot.started_at > 0 && slot.finished_at > 0) {
        if (slot.finished_at > slot.started_at) progress = (now - slot.started_at) / (slot.finished_at - slot.started_at);
        if (slot.finished_at > now) { label = '招募中'; timer = countdown(slot.finished_at - now); running++; style = 'running'; }
        else { label = '计时已结束'; timer = '待同步确认'; due++; style = 'attention'; }
      } else if (slot.state === 0 && !slot.started_at && !slot.finished_at) { label = '未在招募'; timer = '暂无计时'; }
      else { timer = '时间未知'; }
    }
    card.classList.add(style || 'neutral');
    const head = node('div', undefined, 'slot-heading');
    head.append(node('span', `SLOT ${String(index + 1).padStart(2, '0')}`, 'label'), node('span', undefined, 'slot-dot'));
    const symbol = node('div', undefined, 'slot-icon');
    symbol.append(icon(style === 'attention' ? 'shield' : 'clock'));
    card.append(head, symbol, node('strong', label), node('span', timer, 'slot-timer'), resourceRail(progress),
      node('small', slot?.state == null ? '接口状态未知' : `接口状态 ${slot.state}`));
    $('recruit-slots').append(card);
  }
  $('recruit-summary').textContent = slots == null ? '数据未知' : slots.length === 0 ? '接口无槽位记录' : `${running} 招募中 · ${due} 需关注`;
  const base = $('base-status');
  base.replaceChildren();
  const droneValue = dronesAt(snapshot?.drones, now), tired = snapshot?.tired_operators;
  const trading = snapshot?.trading;
  const knownTrading = trading != null && trading.every(t => t.stored != null && t.limit != null);
  const stock = knownTrading ? trading.reduce((n, t) => n + t.stored, 0) : null;
  const limit = knownTrading ? trading.reduce((n, t) => n + t.limit, 0) : null;
  const full = knownTrading ? trading.filter(t => t.limit > 0 && t.stored >= t.limit).length : 0;
  for (const [title, value, hint, attention, symbol] of [
    ['无人机', droneValue === null ? '—' : `${droneValue} / ${snapshot.drones.max}`, '按恢复时间推算', droneValue !== null && droneValue >= snapshot.drones.max, 'energy'],
    ['疲劳干员', tired ?? '—', '森空岛快照', tired > 0, 'users'],
    ['交易站订单', stock === null ? '—' : `${stock} / ${limit}`, knownTrading ? `${full} 个交易站已满 · 快照库存` : '数据未知', full > 0, 'box'],
    ['最近在线', snapshot?.last_online_at ? date(new Date(snapshot.last_online_at * 1000)) : '—', '森空岛记录时间', false, 'clock']
  ]) {
    const item = node('div', undefined, 'base-item' + (attention ? ' attention' : ''));
    item.append(icon(symbol), node('span', title, 'label'), node('strong', String(value)), node('small', hint));
    base.append(item);
  }
  $('skland-age').textContent = r?.fetched_at ? `同步于 ${date(new Date(r.fetched_at * 1000))} · ${Math.max(0, Math.floor((now - r.fetched_at) / 60))} 分钟前` : '尚无成功同步的快照';
  $('skland-next').textContent = r?.next_refresh_at ? `下次同步 ${countdown(r.next_refresh_at - now)} · 每 20 分钟`
    : '每 20 分钟同步 · 倒计时每秒更新';
}

async function refreshSkland() {
  if (state.sklandBusy) return;
  state.sklandBusy = true;
  try {
    const r = await api('/api/skland');
    state.skland = r;
    state.sklandClock = r.observed_at;
    state.sklandAnchor = performance.now();
  } catch (e) {
    state.skland = {...state.skland, state:state.skland?.snapshot ? 'stale' : 'unavailable',
      error:{message:'面板连接失败，当前显示上次快照；请检查面板服务。'}};
  } finally { state.sklandBusy = false; renderSkland(); }
}

async function loadRun(id) {
  state.selected = id;
  state.lastSelected = id;
  state.detailKey = null;
  render();
  $('detail').hidden = false;
  $('detail-content').textContent = '正在读取阶段详情…';
  try {
    const r = await api('/api/runs/' + encodeURIComponent(id));
    if (state.selected === id) detail(r);
  } catch (e) {
    if (state.selected === id) $('detail-content').textContent = '详情读取失败，请稍后重试。';
  }
}
async function refresh() {
  if (state.busy) return;
  state.busy = true;
  $('refresh').disabled = true;
  render();
  try {
    const [status, list] = await Promise.all([api('/api/status'), api(`/api/runs?offset=${state.offset}&limit=30`)]);
    state.runs = list.runs;
    state.total = list.total;
    const activity = status.activity;
    renderAttendance(status.attendance);
    $('service').textContent = {
      running: '运行中',
      idle: '空闲',
      unknown: '状态未知'
    } [activity.state] || '状态未知';
    $('service-card').dataset.state = activity.state;
    $('service-detail').textContent = activity.units.length ? activity.units.map(s => `${s.unit} · ${s.SubState} · PID ${s.MainPID}`).join('；') : activity.state === 'idle' ? '当前没有正在执行的定时托管任务' : '部分服务状态无法读取';
    state.latest = status.latest_run;
    renderLatest(state.latest);
    $('latest-result').textContent = state.latest ? (labels[state.latest.status] || state.latest.status) : '暂无记录';
    $('latest-card').dataset.state = state.latest?.status || 'unknown';
    $('latest-detail').textContent = state.latest ? `${modes[state.latest.mode]||'未知模式'} · ${date(state.latest.started_at)}${state.latest.finished_at?' · 耗时 '+duration(state.latest):''}` : '尚无托管运行记录';
    $('latest-view').hidden = !state.latest;
    const failures = status.service_failures;
    const incomplete = Object.values(status.services).some(s => !s.available);
    $('service-failures').textContent = failures.length ? `${failures.length} 个服务` : incomplete ? '状态未知' : '无';
    $('failure-card').dataset.state = failures.length ? 'failed' : incomplete ? 'unknown' : 'success';
    $('failure-detail').replaceChildren();
    for (const s of failures) {
      $('failure-detail').append(node('div', `${s.unit} · ${s.Result} · 退出码 ${s.ExecMainStatus||'未知'}`), node('div', s.ExecMainExitTimestamp || '退出时间未知'));
    }
    $('failure-detail').append(node('div', failures.length ? '保留的上次失败状态；不代表当前仍在运行。' : incomplete ? '部分服务状态无法读取。' : 'systemd 未保留失败状态；请在历史记录页查看运行结果。'));
    const receipt = status.runtime.receipt;
    state.localVersion = receipt?.core?.active_version || null;
    renderUpstream();
    $('core').textContent = receipt?.core?.active_version || '暂无记录';
    $('runtime').textContent = receipt ? `receipt：${receipt.status||'未知'} · ${date(receipt.checked_at)}` : '暂无 runtime receipt';
    $('connection').textContent = `● 数据已更新 · ${date(status.observed_at)}`;
    if (state.selected) {
      const r = await api('/api/runs/' + encodeURIComponent(state.selected));
      if (r.run_id === state.selected) detail(r);
    }
  } catch (e) {
    $('connection').textContent = '连接失败 · 当前显示为上次快照，请检查面板服务。';
  } finally {
    state.busy = false;
    $('refresh').disabled = false;
    render();
  }
}
$('latest-view').addEventListener('click', () => {
  if (state.latest) $('detail').scrollIntoView({block:'start'});
});
$('refresh').addEventListener('click', () => { refresh(); refreshUpstream(); refreshSkland(); });
$('search').addEventListener('input', render);
for (const b of document.querySelectorAll('[data-filter]')) b.addEventListener('click', () => {
  state.filter = b.dataset.filter;
  for (const x of document.querySelectorAll('[data-filter]')) {
    x.classList.toggle('selected', x === b);
    x.setAttribute('aria-pressed', String(x === b));
  }
  render();
});
$('prev').addEventListener('click', () => {
  state.offset = Math.max(0, state.offset - 30);
  refresh();
});
$('next').addEventListener('click', () => {
  state.offset += 30;
  refresh();
});
$('close').addEventListener('click', () => { location.hash = 'history'; });
window.addEventListener('hashchange', () => route());
window.history.scrollRestoration = 'manual';
route(true);
refresh();
refreshUpstream();
refreshSkland();
setInterval(() => { if (!document.hidden) renderSkland(); }, 1000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { refresh(); refreshUpstream(); refreshSkland(); }
});
setInterval(() => {
  if (!document.hidden) { refresh(); refreshUpstream(); refreshSkland(); }
}, 10000);
