// Pure presentation and contract logic, shared by the browser and Node tests.
export const STATUS_META = {
  queued: { label: '待派发', code: 'QUEUED', glyph: '◇' },
  running: { label: '执行中', code: 'EXECUTING', glyph: '◈' },
  waiting: { label: '定时等待', code: 'STANDBY', glyph: '◌' },
  blocked: { label: '等待输入', code: 'AWAITING INPUT', glyph: '△' },
  succeeded: { label: '已完成', code: 'VERIFIED', glyph: '✓' },
  failed: { label: '执行失败', code: 'FAILED', glyph: '×' },
  cancelled: { label: '已取消', code: 'CANCELLED', glyph: '−' },
  budget_exhausted: { label: '预算耗尽', code: 'BUDGET LIMIT', glyph: '□' },
  deadline_exceeded: { label: '已到截止时间', code: 'DEADLINE', glyph: '◷' },
};

export const TERMINAL = new Set([
  'succeeded', 'failed', 'cancelled', 'budget_exhausted', 'deadline_exceeded',
]);

export function authorizationHeaders(token) {
  if (typeof token !== 'string' || !token.trim() || /^(null|undefined)$/i.test(token.trim())) return {};
  return { Authorization: `Bearer ${token.trim()}` };
}

export function connectionView({ mode, authMode, stale = false, error = '' } = {}) {
  if (mode === 'demo') return {
    badge: 'DEMO MODE', notice: '演示空间 · 下方为模拟任务，不会提交到真实执行器。',
    action: '连接本机 →', footer: 'DEMO ENVIRONMENT',
  };
  if (mode === 'connecting') return {
    badge: 'CONNECTING', notice: '正在连接本机 Rocky，读取任务与运行配置…',
    action: '正在连接…', footer: 'ESTABLISHING LOCAL LINK',
  };
  if (mode === 'live') {
    const local = authMode === 'local';
    return {
      badge: stale ? 'LINK DEGRADED' : local ? '本机直连' : 'API CONNECTED',
      notice: stale ? '链路暂时中断 · 当前显示上次同步的数据，写入操作暂时锁定。系统将自动重试。' : `${local ? '本机直连' : '实时工作区'} · 每 3 秒同步真实任务。执行器在线状态需另行核实。`,
      action: stale ? '重试连接 ↻' : '刷新数据 ↻',
      footer: stale ? 'LINK DEGRADED / CACHED SNAPSHOT' : local ? 'LOCAL DIRECT / CONNECTED' : 'OWNER SESSION / CONNECTED',
    };
  }
  if (mode === 'token_required') return {
    badge: 'LOGIN REQUIRED', notice: '此服务启用了令牌访问模式，请登录后查看真实任务。',
    action: '连接服务 →', footer: 'AWAITING OWNER LOGIN',
  };
  return {
    badge: 'OFFLINE', notice: error ? `连接未成功 · ${error}` : '已断开 Rocky。点击连接可重新读取本机任务。',
    action: error ? '重试连接 ↻' : '连接本机 →', footer: 'OFFLINE / DISCONNECTED',
  };
}

const REASONING_LABELS = {
  none: '不启用推理（none）',
  minimal: '最少（minimal）',
  low: '低（low）',
  medium: '中（medium）',
  high: '高（high）',
  xhigh: '超高（xhigh）',
  max: '最高（max）',
};

export function reasoningLabel(effort) {
  return effort == null || effort === '' ? '模型默认' : REASONING_LABELS[effort] || String(effort);
}

export function modelCatalog(models) {
  const seen = new Set();
  return (Array.isArray(models) ? models : []).filter(item => {
    if (!item || typeof item.id !== 'string' || !item.id || seen.has(item.id)) return false;
    seen.add(item.id); return true;
  }).map(item => ({
    id: item.id,
    label: typeof item.label === 'string' && item.label ? item.label : item.id,
    reasoning_efforts: [...new Set((Array.isArray(item.reasoning_efforts) ? item.reasoning_efforts : []).filter(effort => typeof effort === 'string' && effort.length > 0))],
  }));
}

export function reasoningChoices(models, model) {
  return modelCatalog(models).find(item => item.id === model)?.reasoning_efforts || [];
}

export function normalizeModelSelection(models, selection) {
  const selected = modelCatalog(models).find(item => item.id === selection?.model);
  if (!selected) throw new Error('请选择服务提供的一个模型。');
  const effort = selection.reasoning_effort || null;
  return {
    model: selected.id,
    reasoning_effort: selected.reasoning_efforts.includes(effort) ? effort : null,
  };
}

export function recordedModelSettings(goal) {
  const settings = goal?.model_settings;
  if (!settings || typeof settings.model !== 'string' || !settings.model) return null;
  return { model: settings.model, reasoning_effort: settings.reasoning_effort || null };
}

export function modelSettingsLabel(settings) {
  if (!settings || typeof settings.model !== 'string' || !settings.model) return '未记录';
  return `${settings.model} · ${reasoningLabel(settings.reasoning_effort)}`;
}

const nonnegative = (number) => Number.isFinite(Number(number)) ? Math.max(0, Number(number)) : 0;

export function summarize(goals) {
  const result = { total: goals.length, active: 0, waiting: 0, blocked: 0, completed: 0, modelCalls: 0, tokens: 0 };
  for (const { state } of goals) {
    if (['queued', 'running'].includes(state.status)) result.active++;
    if (state.status === 'waiting') result.waiting++;
    if (state.status === 'blocked') result.blocked++;
    if (state.status === 'succeeded') result.completed++;
    result.modelCalls += nonnegative(state.model_calls);
    result.tokens += nonnegative(state.input_tokens) + nonnegative(state.output_tokens);
  }
  return result;
}

export function budgetPercent(used, maximum) {
  const cap = nonnegative(maximum);
  return cap ? Math.min(100, Math.max(0, nonnegative(used) / cap * 100)) : 0;
}

export function goalTitle(goal) {
  const title = String(goal.contract.objective || '未命名目标').trim().split(/\r?\n/)[0];
  return title.length > 72 ? title.slice(0, 72) + '…' : title;
}

export function relativeTime(iso, now = Date.now()) {
  const stamp = Date.parse(iso);
  if (!Number.isFinite(stamp)) return '—';
  const seconds = Math.max(0, Math.floor((now - stamp) / 1000));
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function lines(value) {
  return String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}

function integer(value, minimum, maximum, label) {
  const raw = String(value ?? '').trim();
  const number = Number(raw);
  if (!raw || !Number.isInteger(number) || number < minimum || number > maximum) {
    throw new Error(`${label}必须是 ${minimum} 至 ${maximum} 的整数。`);
  }
  return number;
}

export function buildContract(fields) {
  const objective = String(fields.objective || '').trim();
  if (!objective || objective.length > 8000) throw new Error('请输入 1 至 8000 字的目标描述。');
  const criteria = lines(fields.criteria);
  if (!criteria.length || criteria.length > 20 || criteria.some(value => value.length > 2000)) {
    throw new Error('请提供 1 至 20 项验收标准，每项不超过 2000 字。');
  }
  const paths = lines(fields.deliverables);
  if (!paths.length || paths.length > 20) throw new Error('请提供 1 至 20 个交付文件路径。');
  for (const path of paths) {
    if (path.length > 240 || /[\\:\x00-\x1f\x7f]/.test(path) || path.startsWith('/') || path.split('/').some(part => !part || part.startsWith('.'))) {
      throw new Error('交付文件须使用可见的相对路径，不能包含隐藏文件、空路径段或上级目录。');
    }
  }
  const maxSteps = integer(fields.maxSteps, 1, 200, '步骤预算');
  const maxCalls = integer(fields.maxCalls, 1, 300, '模型调用上限');
  const rawHours = String(fields.deadlineHours ?? '').trim();
  const hours = Number(rawHours);
  if (!rawHours || !Number.isFinite(hours) || hours < 10 / 3600 || hours > 720) {
    throw new Error('运行期限须在 10 秒至 720 小时之间。');
  }
  const domains = [];
  if (fields.allowFetch) {
    const inputDomains = lines(fields.domains);
    if (!inputDomains.length || inputDomains.length > 30) throw new Error('请提供 1 至 30 个允许访问的精确域名。');
    for (const raw of inputDomains) {
      if (/[\s/:@*?#%\\\x00-\x1f]/.test(raw)) throw new Error('请输入精确域名，不含协议、端口、路径或通配符。');
      let host;
      try { host = new URL('https://' + raw.toLowerCase().replace(/\.$/, '')).hostname; }
      catch { throw new Error('域名格式无效。'); }
      if (host.length > 253 || host.split('.').some(part => !part || part.length > 63 || !/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(part))) {
        throw new Error('域名格式无效。');
      }
      if (!domains.includes(host)) domains.push(host);
    }
  }
  return {
    objective, acceptance_criteria: criteria,
    deliverables: [...new Set(paths)].map(path => ({ path, min_bytes: 1, contains: [] })),
    allowed_tools: ['read_file', 'write_file', 'list_files', ...(fields.allowFetch ? ['fetch_url'] : [])],
    allowed_domains: domains, max_steps: maxSteps, max_model_calls: maxCalls,
    deadline_seconds: Math.round(hours * 3600),
  };
}

export function demoGoals(baseTime = Date.now()) {
  const entries = [
    ['构建自主代理的长期研究资料库', 'running', 8, 12, '正在交叉核验资料来源，并整理可追溯的研究笔记。', 'fetch_url'],
    ['分析公开论文中的关键技术路线', 'running', 4, 6, '已读取指定资料，正在比较三种实现路线。', 'read_file'],
    ['持续追踪工作流文档的更新', 'waiting', 6, 9, '等待下一次计划检查，期间不消耗模型调用。', 'fetch_url'],
    ['为研究报告补充原始证据', 'blocked', 5, 8, '需要补充原始数据文件，收到信息后继续执行。', 'verify'],
    ['生成本周的技术雷达简报', 'succeeded', 12, 18, '简报已通过文件检查和独立语义审查。', 'write_file'],
    ['建立 Rocky 项目运行手册', 'succeeded', 7, 11, '运行手册已核验，产物与执行记录已保存。', 'write_file'],
  ];
  return entries.map(([objective, status, steps, calls, reason, tool], index) => {
    const goal_id = `demo-${String(index + 1).padStart(3, '0')}`;
    const updated_at = new Date(baseTime - (index * 7 + 2) * 60000).toISOString();
    const path = index === 4 ? 'reports/weekly-radar.md' : 'docs/operations.md';
    return {
      goal_id, created_at: new Date(baseTime - (index + 2) * 3600000).toISOString(),
      contract: {
        objective, acceptance_criteria: ['内容有清晰依据，并标注待核实的信息。', '交付指定文件并通过独立核验。'],
        deliverables: [{ path: status === 'succeeded' ? path : 'reports/research.md', min_bytes: 100, contains: [] }],
        allowed_tools: ['read_file', 'write_file', 'list_files', 'fetch_url'], allowed_domains: ['docs.temporal.io'],
        max_steps: 20, max_model_calls: 30, max_output_tokens: 4096, deadline_seconds: 86400,
      },
      state: {
        goal_id, revision: steps * 3, status, reason, steps, model_calls: calls,
        input_tokens: calls * 2380, output_tokens: calls * 420,
        observations: [{ step: steps, tool, success: status !== 'blocked', content: reason, progress_key: 'demo-only' }],
        artifacts: status === 'succeeded' ? [{ path, size: 4096 + index * 780, sha256: 'd3a0'.repeat(16) }] : [],
        wake_at: status === 'waiting' ? new Date(baseTime + 3600000).toISOString() : null,
        updated_at,
      },
    };
  });
}
