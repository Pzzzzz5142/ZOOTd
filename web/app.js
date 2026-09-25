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
  lastSelected: null
};
const expanded = new Map();

function node(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (className) n.className = className;
  return n;
}

function date(value) {
  return value ? new Date(value).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false
  }) : '—';
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
  'no-authorized-fight': '没有已授权的刷图任务'
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
  exit_status: '退出码'
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

function phaseCard(r, name, index) {
  const p = r.phases.find(p => p.phase === name),
    result = p?.result || 'pending';
  const body = node('div', undefined, 'stage-body');
  if (p) {
    body.append(node('p', outcomes[p.outcome] || `已记录结果：${p.outcome}`, 'outcome'));
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
  const card = disclosure('', body, `${r.run_id}:${name}`, !!p && !accepted.includes(p.result));
  card.className = 'stage-card ' + result;
  card.id = 'stage-' + name;
  const summary = card.firstChild;
  summary.replaceChildren(node('span', String(index + 1).padStart(2, '0'), 'stage-number'), node('strong', phases[name] || name), badge(result), node('span', p ? date(p.recorded_at) : '尚无终态时间', 'stage-time'));
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
  content.append(node('p', '阶段数按已记录的终态统计，不代表战斗次数或耗时进度。中间过程仅展示审计已保存的字段；未结束不等于仍在运行。', 'notice'));
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
  stagePane.append(filter, ...cards, empty);
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
      text: outcomes[p.outcome] || p.outcome,
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
  content.append(journey, node('p', `代码版本 ${r.repository?.head?.slice(0,12)||'未知'} · 事件哈希链已校验（不代表重新校验证据文件内容）`, 'muted'));
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
  for (const id of ['overview', 'latest-panel', 'history', 'detail', 'breadcrumb']) {
    $(id).hidden = (id === 'latest-panel' ? 'overview' : id === 'breadcrumb' ? 'detail' : id) !== view;
  }
  const titles = {overview:'运行概览', history:'历史记录', detail:'运行详情'};
  $('page-title').textContent = titles[view];
  document.title = titles[view] + ' · ZOOTd';
  $('page-eyebrow').textContent = 'OPERATIONS / ' + {overview:'OVERVIEW',history:'HISTORY',detail:'RUN DETAIL'}[view];
  $('page-description').textContent = {overview:'查看服务状态与最近一次运行。',history:'查询每轮任务的结果、阶段记录与执行证据。',detail:'查看本轮阶段结果，沿时间线追踪已记录的过程。'}[view];
  for (const name of ['overview', 'history', 'detail']) {
    const link = $('nav-' + name);
    link.classList.toggle('active', name === view);
    link.classList.toggle('parent-active', name === 'history' && view === 'detail');
    if (name === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  $('nav-detail').hidden = view !== 'detail';
  if (match) $('nav-detail').href = location.hash;
  if (view === 'detail') loadRun(match[1]);
  if (!initial) {
    window.scrollTo({top:view === 'history' ? state.historyScroll : 0, behavior:'instant'});
    const previous = view === 'history' && state.lastSelected
      ? document.querySelector(`[data-run-id="${state.lastSelected}"]`) : null;
    (previous || $('page-title')).focus({preventScroll:true});
  }
}

function renderLatest(r) {
  const content = $('latest-summary');
  content.replaceChildren();
  if (!r) {content.append(node('p', '暂无运行记录。', 'muted')); return;}
  const heading = node('div', undefined, 'run-heading');
  heading.append(node('h3', modes[r.mode] || '运行记录'), badge(r.status));
  content.append(heading, node('p', `${date(r.started_at)} · ${r.run_id}`, 'run-id'));
  if (r.status !== 'invalid') {
    const c = counts(r);
    content.append(node('p', `已记录 ${c.recorded} / ${c.total} 个阶段 · 通过 ${c.passed} · 策略完成 ${c.policy} · 无需执行 ${c.skipped} · 异常 ${c.problem}`), segments(r));
  } else content.append(node('p', '运行证据无法核验，请查看详情。', 'muted'));
  const open = node('a', '查看本轮详情 →', 'button-link');
  open.href = '#run/' + encodeURIComponent(r.run_id);
  content.append(open);
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
    $('service').textContent = {
      running: '运行中',
      idle: '空闲',
      unknown: '状态未知'
    } [activity.state] || '状态未知';
    $('service-detail').textContent = activity.units.length ? activity.units.map(s => `${s.unit} · ${s.SubState} · PID ${s.MainPID}`).join('；') : activity.state === 'idle' ? '当前没有正在执行的定时托管任务' : '部分服务状态无法读取';
    state.latest = status.latest_run;
    renderLatest(state.latest);
    $('latest-result').textContent = state.latest ? (labels[state.latest.status] || state.latest.status) : '暂无记录';
    $('latest-detail').textContent = state.latest ? `${modes[state.latest.mode]||'未知模式'} · ${date(state.latest.started_at)}${state.latest.finished_at?' · 耗时 '+duration(state.latest):''}` : '尚无托管运行记录';
    $('latest-view').hidden = !state.latest;
    const failures = status.service_failures;
    const incomplete = Object.values(status.services).some(s => !s.available);
    $('service-failures').textContent = failures.length ? `${failures.length} 个服务` : incomplete ? '状态未知' : '无';
    $('failure-detail').replaceChildren();
    for (const s of failures) {
      $('failure-detail').append(node('div', `${s.unit} · ${s.Result} · 退出码 ${s.ExecMainStatus||'未知'}`), node('div', s.ExecMainExitTimestamp || '退出时间未知'));
    }
    $('failure-detail').append(node('div', failures.length ? '保留的上次失败状态；不代表当前仍在运行。' : incomplete ? '部分服务状态无法读取。' : 'systemd 未保留失败状态；请在历史记录页查看运行结果。'));
    const receipt = status.runtime.receipt;
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
  if (state.latest) select(state.latest.run_id);
});
$('refresh').addEventListener('click', refresh);
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
setInterval(() => {
  if (!document.hidden) refresh();
}, 10000);
