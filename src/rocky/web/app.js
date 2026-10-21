import { STATUS_META, TERMINAL, summarize, budgetPercent, relativeTime, goalTitle, buildContract, demoGoals, modelCatalog, reasoningChoices, normalizeModelSelection, reasoningLabel, recordedModelSettings, modelSettingsLabel, authorizationHeaders, connectionView } from './core.js';
import { startVisuals } from './visuals.js';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const ui = {
  mode: 'connecting', token: null, authMode: null, connecting: false, connectionError: '',
  epoch: 0, goals: [], selectedId: null, filter: 'all',
  info: null, latency: null, synced: null, stale: false, polling: false, dataVersion: 0,
  detailEvents: [], detailMore: false, historyLoading: false, historyError: '',
  detailVersion: 0, connectionAttempt: 0, submission: null, pending: new Set(),
  modelReady: false, modelLoading: false, modelSaving: false, modelUncertain: false,
  modelRequestVersion: 0, modelDraft: null,
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
function clearSession(message = '已断开 Rocky 连接。点击连接可重新建立链路。', { announce = true } = {}) {
  ui.epoch++; ui.connectionAttempt++; ui.token = null; abortRequests(); ui.polling = false;
  ui.mode = 'offline'; ui.authMode = null; ui.connecting = false; ui.connectionError = '';
  ui.goals = []; ui.selectedId = null; ui.info = null;
  ui.pending.clear(); ui.detailEvents = []; ui.submission = null; ui.stale = false;
  ui.modelRequestVersion++; ui.modelReady = false; ui.modelLoading = false; ui.modelSaving = false; ui.modelUncertain = false; ui.modelDraft = null;
  $('#api-token').value = '';
  for (const dialog of $$('dialog[open]')) dialog.close();
  render(); if (announce && message) toast(message);
}
function enterDemo() {
  clearSession('已进入演示空间，未连接真实任务。');
  ui.mode = 'demo'; ui.goals = demoGoals(); ui.selectedId = ui.goals[0].goal_id; render();
}

async function api(path, options = {}) {
  const token = options.anonymous ? null : options.token ?? ui.token;
  const controller = new AbortController();
  controllers.add(controller);
  const timeout = setTimeout(() => controller.abort(), 12000);
  const start = performance.now();
  try {
    const response = await fetch(path, {
      method: options.method || 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error',
      signal: controller.signal,
      headers: { ...authorizationHeaders(token), ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
      ...(options.body ? { body: JSON.stringify(options.body) } : {}),
    });
    if (!response.ok) {
      const messages = { 401: '此服务需要登录，请重新连接。', 403: '当前连接不允许此操作，请确认从本机控制台访问。', 404: '目标不存在，请刷新任务列表。', 409: '任务已结束，或请求与原提交冲突。', 422: '任务参数未通过校验，请检查表单。', 503: 'Rocky 服务尚未就绪，请检查运行配置。' };
      const error = new Error(messages[response.status] || `服务返回 HTTP ${response.status}，请稍后重试。`);
      error.status = response.status; throw error;
    }
    return { data: await response.json(), latency: Math.round(performance.now() - start) };
  } catch (error) {
    if (error.name === 'AbortError' || error instanceof TypeError) {
      if (options.method === 'PUT' && path === '/model-settings') throw new Error('保存结果未确认，请重新读取模型设置后再提交新任务。');
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
  const connectionText = connectionView({ mode: ui.mode, authMode: ui.authMode, stale: ui.stale, error: ui.connectionError });
  connection.dataset.state = ui.connecting ? 'connecting' : isLive && !ui.stale ? 'running' : 'waiting';
  connection.replaceChildren(element('span', 'status-dot'), document.createTextNode(connectionText.badge));
  $('#connection-button').textContent = ui.connecting ? '正在连接…' : isLive ? '断开连接 ↗' : ui.mode === 'token_required' ? '登录服务 ↗' : ui.connectionError ? '重试连接 ↻' : '连接本机 ↗';
  $('#connection-button').disabled = ui.connecting;
  write('#mode-notice-text', connectionText.notice);
  $('#mode-notice').classList.toggle('is-hidden', isLive && !ui.stale);
  write('#notice-connect', connectionText.action);
  $('#notice-connect').disabled = ui.connecting;
  $('#notice-demo').classList.toggle('is-hidden', isLive || isDemo);
  $('#rail-connect').disabled = ui.connecting;
  write('#metric-active', String(summary.active).padStart(2, '0'));
  write('#metric-total', `/ ${String(summary.total).padStart(2, '0')}`);
  write('#metric-calls', number(summary.modelCalls));
  write('#metric-tokens', number(summary.tokens));
  const cap = ui.goals.reduce((sum, item) => sum + item.contract.max_model_calls, 0);
  const percent = budgetPercent(summary.modelCalls, cap);
  write('#budget-label', `${summary.modelCalls} / ${cap} 次`);
  $('#budget-fill').style.width = percent + '%';
  $('#budget-meter').setAttribute('aria-label', `已载入任务的模型调用预算用量 ${Math.round(percent)}%`);
  write('#budget-percent', Math.round(percent) + '%');
  write('#configured-model', isDemo ? '演示模型' : ui.modelUncertain ? '配置待确认' : ui.info?.model || '—');
  write('#configured-reasoning', isDemo ? '演示配置' : ui.modelUncertain ? '待重新读取' : ui.info ? reasoningLabel(ui.info.reasoning_effort) : '—');
  renderCreateModelSummary();
  if ($('#model-dialog').open) renderModelControls();
  write('#api-link', isLive ? ui.stale ? '连接中断' : `${ui.authMode === 'local' ? '本机' : '已连接'} · ${ui.latency ?? '—'} ms` : ui.connecting ? '连接中' : '未连接');
  write('#mission-count', String(ui.goals.length).padStart(2, '0'));
  write('#core-status-label', ui.connecting ? '连接中' : isDemo ? '演示模式' : goal ? meta(goal.state.status).label : '等待任务');
  $('.core-live-status').dataset.state = isDemo ? 'demo' : goal?.state.status || 'offline';
  $('#core-rounds').replaceChildren(document.createTextNode(String(goal?.state.steps || 0).padStart(2, '0') + ' '), element('small', '', '/ ' + (goal?.contract.max_steps || '—')));
  write('#core-goal-label', goal ? goalTitle(goal) : ui.connecting ? '正在读取任务' : isLive ? '等待新任务' : '等待连接 Rocky');
  $('#inspect-current').disabled = !goal;
  write('#mission-caption', isDemo ? '演示任务' : isLive ? `最近 ${ui.goals.length} 个任务 · ${ui.stale ? '上次同步 ' + relativeTime(ui.synced) : '自动同步'}` : ui.connecting ? '正在读取任务…' : '连接后查看任务');
  write('#sync-caption', isDemo ? '模拟记录' : isLive ? ui.stale ? '上次同步的数据' : '每 3 秒自动同步' : '等待连接');
  renderMissions(); renderEvents();
  if ($('#detail-dialog').open && goal) renderDetail();
  if ($('#artifacts-dialog').open) renderArtifacts();
  $('#new-goal').disabled = !['live', 'demo'].includes(ui.mode) || ui.stale || ui.connecting;
}

function renderMissions() {
  const list = $('#mission-list');
  const goals = ui.goals.filter(goal => ui.filter === 'all' || (ui.filter === 'completed' ? goal.state.status === 'succeeded' : !TERMINAL.has(goal.state.status)));
  const nodes = goals.map(goal => {
    const row = element('button', 'mission-row' + (goal.goal_id === ui.selectedId ? ' selected' : ''));
    row.type = 'button'; row.setAttribute('aria-label', `查看任务：${goalTitle(goal)}，${meta(goal.state.status).label}`);
    const icon = element('span', 'mission-icon', meta(goal.state.status).glyph); icon.dataset.state = goal.state.status;
    const copy = element('span', 'mission-copy');
    copy.append(element('span', 'mission-title', goalTitle(goal)), element('span', 'mission-meta', relativeTime(goal.state.updated_at || goal.created_at)));
    const budget = element('span', 'mission-budget');
    budget.append(element('span', 'mono', `${String(goal.state.steps).padStart(2, '0')} / ${goal.contract.max_steps}`));
    const progress = element('span', 'mission-progress');
    const fill = element('span', 'mission-progress-fill');
    fill.style.width = budgetPercent(goal.state.steps, goal.contract.max_steps) + '%'; progress.append(fill); budget.append(progress);
    row.append(icon, copy, budget, badge(goal.state.status), element('span', 'mission-arrow', '↗'));
    row.addEventListener('click', () => inspectGoal(goal.goal_id));
    return row;
  });
  if (!nodes.length) nodes.push(element('div', 'empty-state', ui.connecting ? '正在连接本机 Rocky，读取任务…' : !['live', 'demo'].includes(ui.mode) ? '连接 Rocky 后显示真实任务' : ui.filter === 'all' ? '轨道已就绪。发起你的第一个任务。' : '此筛选下暂时没有任务。'));
  list.replaceChildren(...nodes);
}

function renderEvents() {
  const goals = [...ui.goals].sort((a, b) => Date.parse(b.state.updated_at || b.created_at) - Date.parse(a.state.updated_at || a.created_at)).slice(0, 5);
  const nodes = goals.map(goal => {
    const item = element('div', 'event-item');
    const dot = element('span', 'event-indicator'); dot.dataset.state = goal.state.status;
    const copy = element('div', 'event-copy');
    copy.append(element('div', 'event-label', meta(goal.state.status).label), element('div', 'event-detail mono', `${goal.goal_id.slice(0, 8).toUpperCase()} / ${goal.state.observations?.at(-1)?.tool || 'workflow'}`));
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
  const profile = recordedModelSettings(goal);
  for (const [label, value] of [['任务模型', profile?.model || '未记录'], ['思考级别', profile ? reasoningLabel(profile.reasoning_effort) : '未记录']]) {
    const stat = element('div', 'detail-stat'); stat.append(element('span', 'muted', label), element('strong', 'model-detail-value', value)); grid.append(stat);
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
  const nodes = [grid, detailSection('目标', objective), detailSection('当前执行状态', element('p', 'detail-reason', goal.state.reason || '已入队，等待派发。')), detailSection('验收标准', criteria), detailSection('执行范围', scope), detailSection('模型配置记录', element('p', 'field-note', profile ? '此任务使用提交时保存的配置；当前默认设置的调整不会改变它。' : ui.mode === 'demo' ? '演示任务未绑定真实模型。' : '此任务未记录模型配置，无法从当前默认设置推断。')), detailSection('产物记录', artifacts), detailSection('最近观察', observations), detailSection('持久检查点', history), element('p', 'field-note', `创建于 ${timestamp(goal.created_at)}。文件位于 data/workspaces/${goal.goal_id}/；此处仅展示路径、大小与校验值。`)];
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
  const epoch = ui.epoch; const dataVersion = ui.dataVersion; const modelVersion = ui.modelRequestVersion; ui.polling = true;
  try {
    const [{ data, latency }, info] = await Promise.all([api('/goals?limit=100'), api('/console-info')]);
    if (epoch !== ui.epoch || dataVersion !== ui.dataVersion) return;
    // A poll that began before a settings read/save must not replace its result.
    // Keep the editable draft untouched even when another tab updates defaults.
    if (modelVersion === ui.modelRequestVersion && !ui.modelSaving && !ui.modelLoading && !ui.modelUncertain) ui.info = info.data;
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

async function connectLocalFirst({ announce = true } = {}) {
  if (ui.connecting) return;
  const reopenModels = $('#model-dialog').open;
  clearSession('', { announce: false });
  const epoch = ui.epoch; const attempt = ++ui.connectionAttempt;
  ui.mode = 'connecting'; ui.connecting = true; render();
  try {
    const info = await api('/console-info', { anonymous: true });
    if (epoch !== ui.epoch || attempt !== ui.connectionAttempt) return;
    const goals = await api('/goals?limit=100', { anonymous: true });
    if (epoch !== ui.epoch || attempt !== ui.connectionAttempt) return;
    ui.mode = 'live'; ui.authMode = info.data.auth_mode || 'local'; ui.token = null;
    ui.info = info.data; ui.goals = goals.data; ui.latency = goals.latency;
    ui.selectedId = ui.goals[0]?.goal_id || null; ui.synced = new Date().toISOString(); ui.stale = false;
    render(); if (announce) toast('已通过本机直连连接 Rocky。');
    if (reopenModels) openModelSettings();
  } catch (error) {
    if (epoch !== ui.epoch || attempt !== ui.connectionAttempt) return;
    if (error.status === 401) {
      ui.mode = 'token_required'; ui.authMode = 'token';
      write('#connect-error', ''); openDialog('#connect-dialog');
    } else {
      ui.mode = 'offline'; ui.connectionError = error.message;
    }
  } finally {
    if (epoch === ui.epoch && attempt === ui.connectionAttempt) { ui.connecting = false; render(); }
  }
}
function showConnection() {
  if (ui.mode === 'live' && !ui.stale) { toast(ui.authMode === 'local' ? '当前已通过本机直连连接 Rocky。' : '当前服务已连接。'); return; }
  connectLocalFirst();
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
$('#notice-connect').addEventListener('click', () => ui.mode === 'live' && !ui.stale ? refresh() : showConnection());
$('#notice-demo').addEventListener('click', enterDemo);
$('#connect-form').addEventListener('submit', async event => {
  event.preventDefault();
  const token = $('#api-token').value.trim(); if (!token) return;
  if (/^sk-/.test(token)) { write('#connect-error', '这里需要 ROCKY_API_TOKEN，请勿输入模型 API 密钥。'); return; }
  const attempt = ++ui.connectionAttempt;
  busy($('#connect-submit'), true, '建立安全连接 ↗', '正在验证链路…'); write('#connect-error', '');
  try {
    const [info, goals] = await Promise.all([api('/console-info', { token }), api('/goals?limit=100', { token })]);
    if (attempt !== ui.connectionAttempt || !$('#connect-dialog').open) return;
    ui.epoch++; ui.token = info.data.auth_mode === 'local' ? null : token; ui.authMode = info.data.auth_mode || 'token';
    ui.mode = 'live'; ui.connecting = false; ui.connectionError = ''; ui.info = info.data; ui.goals = goals.data; ui.latency = goals.latency;
    ui.selectedId = ui.goals[0]?.goal_id || null; ui.synced = new Date().toISOString(); ui.stale = false; ui.pending.clear(); ui.submission = null;
    $('#api-token').value = ''; $('#connect-dialog').close(); render(); toast('链路已建立，当前显示真实任务数据。');
    if ($('#model-dialog').open) loadModelSettings();
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
  if (target === '#events-section') $(target).open = true;
  $(target).scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
});
$('#refresh-button').addEventListener('click', () => ui.mode === 'live' ? refresh() : toast('当前为演示数据。连接 Rocky 后会自动同步。'));
$('#inspect-current').addEventListener('click', () => { if (selectedGoal()) inspectGoal(ui.selectedId); });

function renderCreateModelSummary() {
  write('#create-model-summary', ui.mode === 'demo' ? '演示任务 · 未绑定真实模型' : ui.modelSaving ? '默认配置正在保存…' : ui.modelUncertain ? '配置待确认，请重新读取模型设置' : ui.info ? modelSettingsLabel(ui.info) : '连接代理后读取默认模型配置');
}

function renderModelControls() {
  const live = ui.mode === 'live' && !ui.stale;
  const pending = ui.modelLoading || ui.modelSaving;
  const known = modelCatalog(ui.info?.models).some(item => item.id === ui.modelDraft?.model);
  const enabled = live && ui.modelReady && !pending;
  $('#model-selection').disabled = !enabled;
  $('#reasoning-selection').disabled = !enabled || !reasoningChoices(ui.info?.models, ui.modelDraft?.model).length;
  $('#model-save').disabled = !enabled || !known;
  $('#model-save').textContent = ui.modelSaving ? '正在保存…' : '保存设置';
  $('#model-reload').disabled = !live || pending;
  $('#model-connect').classList.toggle('is-hidden', ui.mode === 'live');
  write('#model-access-note', ui.mode === 'demo' ? '演示模式。连接后可修改设置。' : ui.mode !== 'live' ? '连接后可修改设置。' : ui.stale ? '连接中断，暂时无法保存。' : ui.modelSaving ? '正在保存…' : ui.modelLoading ? '正在读取模型…' : ui.modelReady ? '' : '请重新读取配置。');
}

function renderModelForm() {
  const catalog = modelCatalog(ui.info?.models);
  const model = $('#model-selection');
  const entries = catalog.map(item => {
    const option = element('option', '', item.label === item.id ? item.id : `${item.label} / ${item.id}`);
    option.value = item.id; return option;
  });
  if (ui.modelDraft?.model && !catalog.some(item => item.id === ui.modelDraft.model)) {
    const option = element('option', '', `${ui.modelDraft.model}（当前配置，不在可选列表中）`);
    option.value = ui.modelDraft.model; entries.unshift(option);
  }
  model.replaceChildren(...(entries.length ? entries : [element('option', '', '连接后读取可选模型')]));
  if (!entries.length) model.options[0].value = '';
  model.value = ui.modelDraft?.model || '';
  const reasoning = $('#reasoning-selection');
  const defaultOption = element('option', '', '模型默认'); defaultOption.value = '';
  const choices = reasoningChoices(catalog, ui.modelDraft?.model);
  reasoning.replaceChildren(defaultOption, ...choices.map(effort => {
    const option = element('option', '', reasoningLabel(effort)); option.value = effort; return option;
  }));
  reasoning.value = ui.modelDraft?.reasoning_effort || '';
  write('#reasoning-help', !ui.modelDraft?.model ? '连接后读取可选档位。' : !catalog.some(item => item.id === ui.modelDraft.model) ? '请选择列表中的模型。' : !choices.length ? '此模型使用默认思考级别。' : '思考级别越高，通常耗时和用量越多。');
  renderModelControls();
}

async function loadModelSettings() {
  if (ui.modelSaving) { renderModelControls(); return; }
  if (ui.mode !== 'live' || ui.stale) {
    ui.modelReady = false; ui.modelDraft = null; renderModelForm(); return;
  }
  const epoch = ui.epoch; const version = ++ui.modelRequestVersion;
  ui.modelLoading = true; ui.modelReady = false;
  write('#model-error', ''); write('#model-selection-note', ''); renderModelForm();
  try {
    const [info, settings] = await Promise.all([api('/console-info'), api('/model-settings')]);
    if (epoch !== ui.epoch || version !== ui.modelRequestVersion) return;
    ui.info = { ...info.data, ...settings.data };
    ui.modelDraft = { model: settings.data.model, reasoning_effort: settings.data.reasoning_effort ?? null };
    const catalog = modelCatalog(ui.info.models);
    if (catalog.some(item => item.id === ui.modelDraft.model)) {
      const normalized = normalizeModelSelection(catalog, ui.modelDraft);
      if (normalized.reasoning_effort !== ui.modelDraft.reasoning_effort) write('#model-selection-note', '当前配置的思考档位不可用，已显示“模型默认”；保存后才应用调整。');
      ui.modelDraft = normalized;
    }
    ui.modelReady = true; ui.modelUncertain = false;
  } catch (error) {
    if (epoch !== ui.epoch || version !== ui.modelRequestVersion) return;
    if (error.status === 401) clearSession(error.message);
    else write('#model-error', error.message);
  } finally {
    if (epoch === ui.epoch && version === ui.modelRequestVersion) {
      ui.modelLoading = false; renderModelForm(); render();
    }
  }
}

function openModelSettings() {
  write('#model-error', ''); write('#model-selection-note', '');
  openDialog('#model-dialog'); loadModelSettings();
}
$('#model-settings-button').addEventListener('click', openModelSettings);
$('#create-model-settings').addEventListener('click', openModelSettings);
$('#model-connect').addEventListener('click', showConnection);
$('#model-reload').addEventListener('click', loadModelSettings);
$('#model-dialog').addEventListener('close', () => {
  ui.modelRequestVersion++; ui.modelLoading = false;
});
$('#model-selection').addEventListener('change', () => {
  const previous = ui.modelDraft?.reasoning_effort || null;
  try {
    ui.modelDraft = normalizeModelSelection(ui.info?.models, { model: $('#model-selection').value, reasoning_effort: previous });
    write('#model-selection-note', previous && ui.modelDraft.reasoning_effort !== previous ? `新模型不支持${reasoningLabel(previous)}，已切换为“模型默认”。` : '');
    write('#model-error', ''); renderModelForm();
  } catch (error) { write('#model-error', error.message); }
});
$('#reasoning-selection').addEventListener('change', () => {
  if (ui.modelDraft) ui.modelDraft.reasoning_effort = $('#reasoning-selection').value || null;
  write('#model-selection-note', '');
});
$('#model-form').addEventListener('submit', async event => {
  event.preventDefault(); write('#model-error', '');
  if (ui.mode !== 'live' || ui.stale || !ui.modelReady || ui.modelLoading || ui.modelSaving) {
    write('#model-error', '请先连接并读取最新配置，再保存模型设置。'); return;
  }
  let settings;
  try { settings = normalizeModelSelection(ui.info?.models, { model: $('#model-selection').value, reasoning_effort: $('#reasoning-selection').value || null }); }
  catch (error) { write('#model-error', error.message); return; }
  const epoch = ui.epoch; const version = ++ui.modelRequestVersion;
  ui.modelSaving = true; renderModelControls(); renderCreateModelSummary();
  try {
    const { data } = await api('/model-settings', { method: 'PUT', body: settings });
    if (epoch !== ui.epoch) return;
    ui.info = { ...ui.info, ...data }; ui.modelDraft = { model: data.model, reasoning_effort: data.reasoning_effort ?? null };
    ui.modelReady = true; ui.modelUncertain = false;
    const sameDialog = $('#model-dialog').open && version === ui.modelRequestVersion;
    ui.modelRequestVersion++;
    if (sameDialog) $('#model-dialog').close();
    render(); toast('默认模型设置已保存，仅新任务生效。');
  } catch (error) {
    if (epoch !== ui.epoch) return;
    if (error.status === 401) clearSession(error.message);
    else {
      ui.modelRequestVersion++;
      ui.modelUncertain = !error.status || error.status >= 500;
      if (ui.modelUncertain) ui.modelReady = false;
      write('#model-error', error.status === 422 ? '模型或思考级别不受支持，请重新读取可选配置。' : error.message);
    }
  } finally {
    if (epoch === ui.epoch) { ui.modelSaving = false; render(); if ($('#model-dialog').open) renderModelForm(); }
  }
});

function openCreate() {
  if (ui.modelSaving) { toast('模型设置正在保存，请稍后创建任务。'); return; }
  if (ui.modelUncertain) { openModelSettings(); return; }
  if (!['live', 'demo'].includes(ui.mode)) { showConnection(); return; }
  write('#create-eyebrow', ui.mode === 'demo' ? '演示任务' : '新任务');
  write('#create-submit', ui.mode === 'demo' ? '创建演示任务' : '启动任务');
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
  if (ui.modelSaving || ui.modelUncertain) { write('#create-error', '请先确认模型设置已保存，再提交新任务。'); return; }
  const serialized = JSON.stringify(contract);
  if (ui.submission?.body !== serialized) ui.submission = { body: serialized, key: crypto.randomUUID() };
  const epoch = ui.epoch; ui.dataVersion++; busy($('#create-submit'), true, '启动任务');
  try {
    const { data } = await api('/goals', { method: 'POST', body: contract, headers: { 'Idempotency-Key': ui.submission.key } });
    if (epoch !== ui.epoch) return;
    ui.submission = null; ui.selectedId = data.goal_id;
    ui.goals = [data, ...ui.goals.filter(goal => goal.goal_id !== data.goal_id)].slice(0, 100);
    $('#create-dialog').close(); render(); toast('目标已持久保存并入队，等待执行器派发。'); refresh();
  } catch (error) {
    if (epoch !== ui.epoch) return;
    if (error.status === 401) clearSession(error.message); else write('#create-error', error.message);
  } finally { busy($('#create-submit'), false, '启动任务'); }
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

const polling = setInterval(refresh, 3000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
const stopVisuals = startVisuals({ canvas: $('#core-canvas'), getStatus: () => ui.mode === 'demo' ? 'demo' : ui.mode === 'offline' ? 'offline' : selectedGoal()?.state.status || 'waiting' });
window.addEventListener('pagehide', () => { clearInterval(polling); stopVisuals(); abortRequests(); ui.token = null; });
window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
render();
connectLocalFirst({ announce: false });
