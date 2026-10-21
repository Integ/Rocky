import { STATUS_META, TERMINAL, summarize, budgetPercent, relativeTime, goalTitle, buildContract, demoGoals } from './core.js';
import { startVisuals } from './visuals.js';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const ui = {
  mode: 'demo', token: null, epoch: 0, goals: demoGoals(), selectedId: 'demo-001', filter: 'all',
  info: null, latency: null, synced: null, stale: false, polling: false, dataVersion: 0,
  detailEvents: [], detailMore: false, historyLoading: false, historyError: '',
  detailVersion: 0, connectionAttempt: 0, submission: null, pending: new Set(),
};
const controllers = new Set();
let toastTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function write(selector, value) { $(selector).textContent = String(value); }
function selectedGoal() { return ui.goals.find(goal => goal.goal_id === ui.selectedId) || null; }
function meta(status) { return STATUS_META[status] || { label: '未知状态', code: 'UNKNOWN', glyph: '?' }; }
function number(value) { return new Intl.NumberFormat('en', { notation: value >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(value || 0); }
function timestamp(value) { const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : '—'; }
function toast(message) {
  clearTimeout(toastTimer);
  write('#toast', message);
  $('#toast').classList.remove('is-hidden');
  toastTimer = setTimeout(() => $('#toast').classList.add('is-hidden'), 5500);
}
function openDialog(id) { const dialog = $(id); if (!dialog.open) dialog.showModal(); }
function busy(button, value, normalLabel, pendingLabel = '正在发送…') {
  button.disabled = value;
  button.textContent = value ? pendingLabel : normalLabel;
}
function badge(status) {
  const item = element('span', 'mission-status', meta(status).label);
  item.dataset.state = status;
  return item;
}
function abortRequests() { for (const controller of controllers) controller.abort(); controllers.clear(); }
function clearSession(message = '连接已断开，访问令牌已从页面清除。') {
  ui.epoch++; ui.connectionAttempt++; ui.token = null; abortRequests(); ui.polling = false;
  ui.mode = 'offline'; ui.goals = []; ui.selectedId = null; ui.info = null;
  ui.pending.clear(); ui.detailEvents = []; ui.submission = null; ui.stale = false;
  $('#api-token').value = '';
  for (const dialog of $$('dialog[open]')) dialog.close();
  render(); toast(message);
}
function enterDemo() {
  clearSession('已进入演示空间，未连接真实任务。');
  ui.mode = 'demo'; ui.goals = demoGoals(); ui.selectedId = ui.goals[0].goal_id; render();
}

async function api(path, options = {}) {
  const token = options.token ?? ui.token;
  const controller = new AbortController();
  controllers.add(controller);
  const timeout = setTimeout(() => controller.abort(), 12000);
  const start = performance.now();
  try {
    const response = await fetch(path, {
      method: options.method || 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error',
      signal: controller.signal,
      headers: { 'Authorization': `Bearer ${token}`, ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
      ...(options.body ? { body: JSON.stringify(options.body) } : {}),
    });
    if (!response.ok) {
      const messages = { 401: '访问令牌无效，请重新连接。', 403: '当前令牌没有此操作的权限。', 404: '目标不存在，请刷新任务列表。', 409: '任务已结束，或请求与原提交冲突。', 422: '任务参数未通过校验，请检查表单。', 503: '服务尚未配置访问令牌，请检查 ROCKY_API_TOKEN。' };
      const error = new Error(messages[response.status] || `服务返回 HTTP ${response.status}，请稍后重试。`);
      error.status = response.status; throw error;
    }
    return { data: await response.json(), latency: Math.round(performance.now() - start) };
  } catch (error) {
    if (error.name === 'AbortError' || error instanceof TypeError) {
      if (options.method === 'POST' && path.endsWith('/wake')) throw new Error('唤醒结果未确认，请先刷新任务状态，避免重复唤醒。');
      if (options.method === 'POST' && path === '/goals') throw new Error('提交结果未确认。可使用原表单重试，原幂等键会避免重复创建。');
      if (options.method === 'POST') throw new Error('命令结果未确认，请先刷新任务状态。');
      throw new Error('无法连接当前 Rocky 服务，请检查网络或服务状态。');
    }
    throw error;
  } finally { clearTimeout(timeout); controllers.delete(controller); }
}

function render() {
  const summary = summarize(ui.goals);
  const goal = selectedGoal();
  const isLive = ui.mode === 'live';
  const isDemo = ui.mode === 'demo';
  const connection = $('#connection-badge');
  connection.dataset.state = isLive && !ui.stale ? 'running' : 'waiting';
  connection.replaceChildren(element('span', 'status-dot'), document.createTextNode(isDemo ? 'DEMO MODE' : isLive ? ui.stale ? 'LINK DEGRADED' : 'API CONNECTED' : 'OFFLINE'));
  $('#connection-button').textContent = isLive ? '断开连接 ↗' : '连接代理 ↗';
  write('#mode-notice-text', isDemo ? '演示空间 · 下方为模拟任务。连接 Rocky 后显示真实运行数据。' : isLive ? ui.stale ? '链路暂时中断 · 当前显示上次同步的数据，写入操作暂时锁定。' : '实时工作区 · 每 3 秒同步任务状态。API 已连接，执行器在线状态需另行核实。' : '尚未连接 · 输入控制台访问令牌，或进入演示空间。');
  write('#notice-connect', isDemo ? '建立连接 →' : isLive ? '刷新链路 ↻' : '查看演示 →');
  write('#metric-active', String(summary.active).padStart(2, '0'));
  write('#metric-total', `/ ${String(summary.total).padStart(2, '0')}`);
  write('#metric-calls', number(summary.modelCalls));
  write('#metric-tokens', number(summary.tokens));
  const cap = ui.goals.reduce((sum, item) => sum + item.contract.max_model_calls, 0);
  const percent = budgetPercent(summary.modelCalls, cap);
  write('#budget-label', `${summary.modelCalls} / ${cap} CALLS`);
  $('#budget-fill').style.width = percent + '%';
  $('#budget-meter').setAttribute('aria-label', `已载入任务的模型调用预算用量 ${Math.round(percent)}%`);
  write('#budget-percent', Math.round(percent) + '%');
  write('#configured-model', isDemo ? 'DEMO / SIMULATED' : ui.info?.model || '—');
  write('#api-link', isLive ? ui.stale ? 'SYNC LOST' : `CONNECTED / ${ui.latency ?? '—'} MS` : 'NOT CONNECTED');
  write('#mission-count', String(ui.goals.length).padStart(2, '0'));
  write('#core-status-label', isDemo ? 'SIMULATION ACTIVE' : goal ? meta(goal.state.status).code : 'AWAITING MISSION');
  $('.core-live-status').dataset.state = isDemo ? 'demo' : goal?.state.status || 'offline';
  $('#core-rounds').replaceChildren(document.createTextNode(String(goal?.state.steps || 0).padStart(2, '0') + ' '), element('small', '', '/ ' + (goal?.contract.max_steps || '—')));
  write('#core-goal-id', goal ? goal.goal_id.slice(0, 12).toUpperCase() : 'NO VECTOR');
  write('#core-goal-label', goal ? goal.state.reason || goalTitle(goal) : '等待你定义下一个目标');
  $('#inspect-current').disabled = !goal;
  write('#mission-caption', isDemo ? '演示数据 · 选择任务查看执行详情' : isLive ? `载入最近 ${ui.goals.length} 个任务（最多 100 个）· ${ui.stale ? '上次同步 ' : '已同步 '}${relativeTime(ui.synced)}` : '连接代理后显示真实任务');
  write('#sync-caption', isDemo ? 'DEMO TELEMETRY / NO LIVE REQUESTS' : isLive ? ui.stale ? 'UPLINK INTERRUPTED / CACHED SNAPSHOT' : 'SNAPSHOT SYNC / 3 SEC INTERVAL' : 'AWAITING SECURE UPLINK');
  write('#footer-state', isDemo ? 'DEMO ENVIRONMENT' : isLive ? ui.stale ? 'LINK DEGRADED' : 'OWNER SESSION / CONNECTED' : 'OFFLINE / NO CREDENTIALS');
  renderMissions(); renderEvents();
  if ($('#detail-dialog').open && goal) renderDetail();
  if ($('#artifacts-dialog').open) renderArtifacts();
  $('#new-goal').disabled = ui.mode === 'offline' || ui.stale;
}

function renderMissions() {
  const list = $('#mission-list');
  const goals = ui.goals.filter(goal => ui.filter === 'all' || (ui.filter === 'completed' ? goal.state.status === 'succeeded' : !TERMINAL.has(goal.state.status)));
  const nodes = goals.map(goal => {
    const row = element('button', 'mission-row' + (goal.goal_id === ui.selectedId ? ' selected' : ''));
    row.type = 'button'; row.setAttribute('aria-label', `查看任务：${goalTitle(goal)}，${meta(goal.state.status).label}`);
    const icon = element('span', 'mission-icon', meta(goal.state.status).glyph); icon.dataset.state = goal.state.status;
    const copy = element('span', 'mission-copy');
    copy.append(element('span', 'mission-title', goalTitle(goal)), element('span', 'mission-meta mono', `${goal.goal_id.slice(0, 8).toUpperCase()} · ${relativeTime(goal.state.updated_at || goal.created_at)}`));
    const budget = element('span', 'mission-budget');
    budget.append(element('span', 'mono', `${String(goal.state.steps).padStart(2, '0')} / ${goal.contract.max_steps}`));
    const progress = element('span', 'mission-progress');
    const fill = element('span', 'mission-progress-fill');
    fill.style.width = budgetPercent(goal.state.steps, goal.contract.max_steps) + '%'; progress.append(fill); budget.append(progress);
    row.append(icon, copy, budget, badge(goal.state.status), element('span', 'mission-arrow', '↗'));
    row.addEventListener('click', () => inspectGoal(goal.goal_id));
    return row;
  });
  if (!nodes.length) nodes.push(element('div', 'empty-state', ui.mode === 'offline' ? '等待连接你的 Rocky' : ui.filter === 'all' ? '轨道已就绪。发起你的第一个任务。' : '此筛选下暂时没有任务。'));
  list.replaceChildren(...nodes);
}

function renderEvents() {
  const goals = [...ui.goals].sort((a, b) => Date.parse(b.state.updated_at || b.created_at) - Date.parse(a.state.updated_at || a.created_at)).slice(0, 5);
  const nodes = goals.map(goal => {
    const item = element('div', 'event-item');
    const dot = element('span', 'event-indicator'); dot.dataset.state = goal.state.status;
    const copy = element('div', 'event-copy');
    copy.append(element('div', 'event-label', goal.state.reason || meta(goal.state.status).label), element('div', 'event-detail mono', `${goal.goal_id.slice(0, 8).toUpperCase()} / ${goal.state.observations?.at(-1)?.tool || 'workflow'}`));
    item.append(dot, copy, element('time', 'event-time mono', relativeTime(goal.state.updated_at || goal.created_at)));
    return item;
  });
  $('#event-list').replaceChildren(...(nodes.length ? nodes : [element('div', 'empty-state', '收到任务后，执行信号将在这里出现。')]));
}

function detailSection(title, content) {
  const section = element('section', 'detail-section');
  section.append(element('h3', '', title), content); return section;
}
function artifactCard(artifact, goal) {
  const card = element('div', 'artifact-card');
  card.append(element('div', 'artifact-name mono', artifact.path), element('div', 'artifact-meta', `${number(artifact.size)} bytes · ${goalTitle(goal)}`));
  const hash = element('code', 'artifact-hash mono', `SHA256 ${artifact.sha256}`); card.append(hash);
  return card;
}
function renderDetail() {
  const goal = selectedGoal(); if (!goal) return;
  const body = $('#detail-body'); const scroll = body.scrollTop;
  write('#detail-id', `${ui.mode === 'demo' ? 'DEMO / ' : ''}MISSION ${goal.goal_id}`);
  write('#detail-title', goalTitle(goal));
  const grid = element('div', 'detail-grid');
  for (const [label, value] of [['状态', meta(goal.state.status).label], ['步骤预算', `${goal.state.steps} / ${goal.contract.max_steps}`], ['模型调用', `${goal.state.model_calls} / ${goal.contract.max_model_calls}`], ['运行期限', `${goal.contract.deadline_seconds / 3600} 小时 / 从执行开始`]]) {
    const stat = element('div', 'detail-stat'); stat.append(element('span', 'muted', label), element('strong', '', value)); grid.append(stat);
  }
  const objective = element('p', 'detail-objective', goal.contract.objective);
  const criteria = element('ul', 'criteria-list');
  for (const item of goal.contract.acceptance_criteria) criteria.append(element('li', '', item));
  const artifacts = element('div', 'artifact-list');
  artifacts.append(...(goal.state.artifacts?.length ? goal.state.artifacts.map(item => artifactCard(item, goal)) : [element('p', 'muted', '尚无产物记录。')]));
  const scope = element('p', 'mono scope-text', `${goal.contract.allowed_tools.join(' · ')}\n域名：${goal.contract.allowed_domains.join(', ') || '未授权网络读取'}`);
  const observations = element('div', 'observation-list');
  for (const observation of (goal.state.observations || []).slice(-6).reverse()) {
    const item = element('div', 'observation-item');
    item.append(element('span', 'eyebrow', `STEP ${observation.step} / ${observation.tool} / ${observation.success ? 'OK' : 'CHECK'}`), element('pre', '', observation.content)); observations.append(item);
  }
  if (!observations.children.length) observations.append(element('p', 'muted', '等待执行器产生第一条观察记录。'));
  const history = element('div', 'detail-timeline');
  const events = ui.mode === 'demo' ? [{ event_id: goal.state.revision, state: goal.state }] : ui.detailEvents;
  for (const event of events) {
    const item = element('div', 'timeline-item');
    item.append(element('span', 'mono muted', `#${event.state.revision} / ${timestamp(event.state.updated_at)}`), element('p', '', `${meta(event.state.status).label} · ${event.state.reason || '已保存检查点'}`)); history.append(item);
  }
  if (ui.historyLoading) history.append(element('p', 'muted', '正在读取检查点…'));
  if (ui.historyError) history.append(element('p', 'form-error', ui.historyError));
  if (!events.length && !ui.historyLoading && !ui.historyError) history.append(element('p', 'muted', '尚无持久检查点，等待执行器首次处理。'));
  if (ui.detailMore && !ui.historyLoading) {
    const more = element('button', 'btn btn-ghost', '加载更多检查点'); more.type = 'button';
    more.addEventListener('click', () => loadHistory(goal.goal_id)); history.append(more);
  }
  const nodes = [grid, detailSection('目标', objective), detailSection('当前执行状态', element('p', 'detail-reason', goal.state.reason || '已入队，等待派发。')), detailSection('验收标准', criteria), detailSection('执行范围', scope), detailSection('产物记录', artifacts), detailSection('最近观察', observations), detailSection('持久检查点', history), element('p', 'field-note', `创建于 ${timestamp(goal.created_at)}。文件位于 data/workspaces/${goal.goal_id}/；此处仅展示路径、大小与校验值。`)];
  body.replaceChildren(...nodes); body.scrollTop = scroll;
  const disabled = TERMINAL.has(goal.state.status) || ui.stale;
  $('#wake-goal').disabled = disabled || ui.pending.has(goal.goal_id + ':wake');
  $('#cancel-goal').disabled = disabled || ui.pending.has(goal.goal_id + ':cancel');
}

async function loadHistory(goalId, reset = false) {
  if (ui.mode !== 'live' || ui.historyLoading) return;
  const epoch = ui.epoch; const version = ui.detailVersion;
  if (reset) ui.detailEvents = [];
  const after = ui.detailEvents.at(-1)?.event_id || 0;
  ui.historyLoading = true; ui.historyError = ''; renderDetail();
  try {
    const { data } = await api(`/goals/${encodeURIComponent(goalId)}/events?after=${after}`);
    if (epoch !== ui.epoch || version !== ui.detailVersion) return;
    ui.detailEvents.push(...data); ui.detailMore = data.length === 100;
  } catch (error) {
    if (epoch === ui.epoch && version === ui.detailVersion) ui.historyError = error.message;
    if (error.status === 401 && epoch === ui.epoch) clearSession(error.message);
  } finally {
    if (epoch === ui.epoch && version === ui.detailVersion) { ui.historyLoading = false; if ($('#detail-dialog').open) renderDetail(); }
  }
}

function inspectGoal(goalId) {
  ui.selectedId = goalId; ui.detailVersion++; ui.detailEvents = []; ui.detailMore = false; ui.historyLoading = false; ui.historyError = '';
  render(); renderDetail(); openDialog('#detail-dialog'); loadHistory(goalId, true);
}

function renderArtifacts() {
  const cards = ui.goals.flatMap(goal => (goal.state.artifacts || []).map(item => artifactCard(item, goal)));
  $('#artifacts-body').replaceChildren(...(cards.length ? cards : [element('div', 'empty-state', '尚无产物记录。完成文件写入后，路径与校验值会显示在这里。')]));
}

async function refresh() {
  if (ui.mode !== 'live' || ui.polling || document.hidden) return;
  const epoch = ui.epoch; const dataVersion = ui.dataVersion; ui.polling = true;
  try {
    const { data, latency } = await api('/goals?limit=100');
    if (epoch !== ui.epoch || dataVersion !== ui.dataVersion) return;
    const previous = new Map(ui.goals.map(goal => [goal.goal_id, goal.state.revision]));
    ui.goals = data; ui.latency = latency; ui.synced = new Date().toISOString(); ui.stale = false;
    for (const goal of data) if (goal.state.revision > (previous.get(goal.goal_id) ?? -1)) ui.pending.delete(goal.goal_id + ':wake');
    if (!selectedGoal()) {
      for (const id of ['#detail-dialog', '#wake-dialog', '#cancel-dialog']) $(id).close();
      ui.detailVersion++; ui.historyLoading = false;
      ui.selectedId = ui.goals.find(goal => !TERMINAL.has(goal.state.status))?.goal_id || ui.goals[0]?.goal_id || null;
    }
    for (const goal of ui.goals) if (TERMINAL.has(goal.state.status)) { ui.pending.delete(goal.goal_id + ':cancel'); ui.pending.delete(goal.goal_id + ':wake'); }
    render();
  } catch (error) {
    if (epoch !== ui.epoch) return;
    if (error.status === 401) clearSession(error.message);
    else { ui.stale = true; render(); }
  } finally { if (epoch === ui.epoch) ui.polling = false; }
}

function showConnection() {
  if (ui.mode === 'live') { toast('当前已连接。右上角可断开连接并清除页面令牌。'); return; }
  $('#connect-error').textContent = ''; openDialog('#connect-dialog');
}
$('#connection-button').addEventListener('click', () => ui.mode === 'live' ? clearSession() : showConnection());
$('#fullscreen-toggle').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch { toast('当前浏览器不支持沉浸模式，可在独立浏览器中打开控制台。'); }
});
document.addEventListener('fullscreenchange', () => $('#fullscreen-toggle').setAttribute('aria-label', document.fullscreenElement ? '退出沉浸模式' : '切换沉浸模式'));
$('#rail-connect').addEventListener('click', showConnection);
$('#notice-connect').addEventListener('click', () => ui.mode === 'live' ? refresh() : ui.mode === 'offline' ? enterDemo() : showConnection());
$('#connect-form').addEventListener('submit', async event => {
  event.preventDefault();
  const token = $('#api-token').value.trim(); if (!token) return;
  if (/^sk-/.test(token)) { write('#connect-error', '这里需要 ROCKY_API_TOKEN，请勿输入模型 API 密钥。'); return; }
  const attempt = ++ui.connectionAttempt;
  busy($('#connect-submit'), true, '建立安全连接 ↗', '正在验证链路…'); write('#connect-error', '');
  try {
    const [info, goals] = await Promise.all([api('/console-info', { token }), api('/goals?limit=100', { token })]);
    if (attempt !== ui.connectionAttempt || !$('#connect-dialog').open) return;
    ui.epoch++; ui.token = token; ui.mode = 'live'; ui.info = info.data; ui.goals = goals.data; ui.latency = goals.latency;
    ui.selectedId = ui.goals[0]?.goal_id || null; ui.synced = new Date().toISOString(); ui.stale = false; ui.pending.clear(); ui.submission = null;
    $('#api-token').value = ''; $('#connect-dialog').close(); render(); toast('链路已建立，当前显示真实任务数据。');
  } catch (error) { if (attempt === ui.connectionAttempt) write('#connect-error', error.message); }
  finally { busy($('#connect-submit'), false, '建立安全连接 ↗'); }
});
$('#connect-dialog').addEventListener('close', () => { ui.connectionAttempt++; $('#api-token').value = ''; });
$('#demo-button').addEventListener('click', () => { if (ui.mode !== 'demo') enterDemo(); });

for (const button of $$('[data-close]')) button.addEventListener('click', () => button.closest('dialog').close());
for (const dialog of $$('dialog')) dialog.addEventListener('click', event => {
  if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); }
});
for (const button of $$('[data-filter]')) button.addEventListener('click', () => {
  ui.filter = button.dataset.filter;
  for (const candidate of $$('[data-filter]')) { const active = candidate === button; candidate.classList.toggle('active', active); candidate.setAttribute('aria-pressed', String(active)); }
  renderMissions();
});
for (const button of $$('[data-nav]')) button.addEventListener('click', () => {
  for (const candidate of $$('[data-nav]')) candidate.classList.toggle('active', candidate === button);
  if (button.dataset.nav === 'artifacts') { renderArtifacts(); openDialog('#artifacts-dialog'); return; }
  const target = button.dataset.nav === 'missions' ? '#missions-section' : button.dataset.nav === 'events' ? '#events-section' : '#main-content';
  $(target).scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
});
$('#refresh-button').addEventListener('click', () => ui.mode === 'live' ? refresh() : toast('当前为演示数据。连接 Rocky 后会自动同步。'));
$('#inspect-current').addEventListener('click', () => { if (selectedGoal()) inspectGoal(ui.selectedId); });

function openCreate() {
  if (ui.mode === 'offline') { showConnection(); return; }
  write('#create-eyebrow', ui.mode === 'demo' ? 'NEW MISSION / DEMO' : 'NEW MISSION / LIVE');
  write('#create-submit', ui.mode === 'demo' ? '创建演示任务 ↗' : '发射任务 ↗');
  write('#create-error', ''); openDialog('#create-dialog');
}
$('#new-goal').addEventListener('click', openCreate);
document.addEventListener('keydown', event => {
  if (event.key.toLowerCase() === 'n' && !event.ctrlKey && !event.metaKey && !event.altKey && !$$('dialog[open]').length && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) { event.preventDefault(); openCreate(); }
});
$('#allow-fetch').addEventListener('change', () => $('#domains-field').classList.toggle('is-hidden', !$('#allow-fetch').checked));
for (const button of $$('[data-preset]')) button.addEventListener('click', () => {
  const form = $('#create-form'); const research = button.dataset.preset === 'research';
  form.elements.objective.value = research ? '读取 https://docs.temporal.io/temporal.md，整理一份解释持久执行、重启恢复和幂等性的中文报告。区分来源事实与建议，并附上原始链接。' : '创建 readiness.txt，文件内容为 Rocky is ready.';
  form.elements.criteria.value = research ? '每项核心结论有来源\n说明重启后如何恢复\n解释外部操作为什么需要幂等性' : 'readiness.txt 内容为 Rocky is ready.\n产物通过文件检查与独立核验';
  form.elements.deliverables.value = research ? 'reports/summary.md' : 'readiness.txt';
  form.elements.allowFetch.checked = research; form.elements.domains.value = research ? 'docs.temporal.io' : '';
  $('#domains-field').classList.toggle('is-hidden', !research);
});

$('#create-form').addEventListener('submit', async event => {
  event.preventDefault(); write('#create-error', '');
  const form = event.currentTarget; const values = Object.fromEntries(new FormData(form)); values.allowFetch = form.elements.allowFetch.checked;
  let contract; try { contract = buildContract(values); } catch (error) { write('#create-error', error.message); return; }
  if (ui.mode === 'demo') {
    const goal = demoGoals()[0]; goal.goal_id = 'demo-' + crypto.randomUUID().slice(0, 8); goal.contract = contract;
    goal.created_at = new Date().toISOString(); goal.state = { ...goal.state, goal_id: goal.goal_id, status: 'queued', reason: '演示任务已创建，未提交到真实执行器。', revision: 0, steps: 0, model_calls: 0, input_tokens: 0, output_tokens: 0, observations: [], artifacts: [], updated_at: goal.created_at };
    ui.goals.unshift(goal); ui.selectedId = goal.goal_id; $('#create-dialog').close(); render(); toast('已创建演示任务。连接 Rocky 后才能提交真实目标。'); return;
  }
  if (ui.mode !== 'live' || ui.stale) { write('#create-error', '请先恢复与 Rocky 的连接。'); return; }
  const serialized = JSON.stringify(contract);
  if (ui.submission?.body !== serialized) ui.submission = { body: serialized, key: crypto.randomUUID() };
  const epoch = ui.epoch; ui.dataVersion++; busy($('#create-submit'), true, '发射任务 ↗');
  try {
    const { data } = await api('/goals', { method: 'POST', body: contract, headers: { 'Idempotency-Key': ui.submission.key } });
    if (epoch !== ui.epoch) return;
    ui.submission = null; ui.selectedId = data.goal_id;
    ui.goals = [data, ...ui.goals.filter(goal => goal.goal_id !== data.goal_id)].slice(0, 100);
    $('#create-dialog').close(); render(); toast('目标已持久保存并入队，等待执行器派发。'); refresh();
  } catch (error) {
    if (epoch !== ui.epoch) return;
    if (error.status === 401) clearSession(error.message); else write('#create-error', error.message);
  } finally { busy($('#create-submit'), false, '发射任务 ↗'); }
});

$('#wake-goal').addEventListener('click', () => { write('#wake-error', ''); busy($('#wake-submit'), false, '发送信号 ↗'); openDialog('#wake-dialog'); });
$('#wake-form').addEventListener('submit', async event => {
  event.preventDefault(); const goal = selectedGoal(); if (!goal || TERMINAL.has(goal.state.status)) return;
  const message = new FormData(event.currentTarget).get('message').trim(); if (!message) return;
  write('#wake-error', '');
  if (ui.mode === 'demo') { goal.state.status = 'queued'; goal.state.reason = '演示信号：' + message; goal.state.updated_at = new Date().toISOString(); $('#wake-dialog').close(); render(); toast('演示信号已发送，未调用真实 API。'); return; }
  if (ui.mode !== 'live' || ui.stale) { write('#wake-error', '请先恢复连接。'); return; }
  if (ui.pending.has(goal.goal_id + ':wake')) { write('#wake-error', '已有信号待确认，请先刷新任务状态。'); return; }
  const epoch = ui.epoch; ui.dataVersion++; ui.pending.add(goal.goal_id + ':wake'); busy($('#wake-submit'), true, '发送信号 ↗');
  try {
    await api(`/goals/${encodeURIComponent(goal.goal_id)}/wake`, { method: 'POST', body: { message } });
    if (epoch !== ui.epoch) return;
    ui.pending.add(goal.goal_id + ':wake'); $('#wake-dialog').close(); render(); toast('唤醒信号已持久入队，等待执行器处理。'); refresh();
  } catch (error) { if (epoch === ui.epoch) {
    if (error.status) ui.pending.delete(goal.goal_id + ':wake');
    if (error.status === 401) clearSession(error.message);
    else if (!error.status) { $('#wake-dialog').close(); render(); toast(error.message); }
    else write('#wake-error', error.message);
  } }
  finally { busy($('#wake-submit'), false, '发送信号 ↗'); }
});
$('#cancel-goal').addEventListener('click', () => { write('#cancel-error', ''); openDialog('#cancel-dialog'); });
$('#confirm-cancel').addEventListener('click', async () => {
  const goal = selectedGoal(); if (!goal || TERMINAL.has(goal.state.status)) return;
  if (ui.mode === 'demo') { goal.state.status = 'cancelled'; goal.state.reason = '演示任务已取消。'; goal.state.updated_at = new Date().toISOString(); $('#cancel-dialog').close(); render(); toast('演示任务已取消。'); return; }
  if (ui.mode !== 'live' || ui.stale) { write('#cancel-error', '请先恢复连接。'); return; }
  const epoch = ui.epoch; ui.dataVersion++; busy($('#confirm-cancel'), true, '确认取消任务');
  try {
    await api(`/goals/${encodeURIComponent(goal.goal_id)}/cancel`, { method: 'POST' });
    if (epoch !== ui.epoch) return;
    ui.pending.add(goal.goal_id + ':cancel'); $('#cancel-dialog').close(); render(); toast('取消命令已入队。已有产物将保留。'); refresh();
  } catch (error) { if (epoch === ui.epoch) { if (error.status === 401) clearSession(error.message); else write('#cancel-error', error.message); } }
  finally { busy($('#confirm-cancel'), false, '确认取消任务'); }
});
$('#export-goal').addEventListener('click', () => {
  const goal = selectedGoal(); if (!goal) return;
  const blob = new Blob([JSON.stringify({ mode: ui.mode, goal, loaded_checkpoints: ui.detailEvents }, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob); const link = element('a'); link.href = url; link.download = `rocky-${goal.goal_id}.json`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast('任务记录已导出，包含当前载入的检查点。');
});

function updateClock() { write('#utc-clock', new Date().toISOString().slice(11, 19) + ' UTC'); }
updateClock(); const clock = setInterval(updateClock, 1000);
const polling = setInterval(refresh, 3000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
const stopVisuals = startVisuals({ canvas: $('#core-canvas'), getStatus: () => ui.mode === 'demo' ? 'demo' : ui.mode === 'offline' ? 'offline' : selectedGoal()?.state.status || 'waiting' });
window.addEventListener('pagehide', () => { clearInterval(clock); clearInterval(polling); stopVisuals(); abortRequests(); ui.token = null; });
window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
render();
