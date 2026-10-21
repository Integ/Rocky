import assert from 'node:assert/strict';
import test from 'node:test';

import {
  STATUS_META,
  TERMINAL,
  budgetPercent,
  buildContract,
  demoGoals,
  goalTitle,
  relativeTime,
  summarize,
  modelCatalog,
  reasoningChoices,
  normalizeModelSelection,
  reasoningLabel,
  recordedModelSettings,
  modelSettingsLabel,
  authorizationHeaders,
  connectionView,
} from '../../src/rocky/web/core.js';

const NOW = Date.parse('2026-10-21T03:00:00Z');

test('anonymous local requests never manufacture bearer credentials from missing values', () => {
  for (const token of [undefined, null, '', '   ', 'null', 'undefined', ' NULL ', 0, false]) {
    assert.deepEqual(authorizationHeaders(token), {});
    assert.equal('Authorization' in authorizationHeaders(token), false);
  }
});

test('explicit token-mode credentials are passed only when a real value is supplied', () => {
  assert.deepEqual(authorizationHeaders('  owner-session-sentinel  '), {
    Authorization: 'Bearer owner-session-sentinel',
  });
  assert.deepEqual(authorizationHeaders(null), {}, 'later anonymous requests must not retain an earlier credential');
});

test('local connected presentation has no token gate and is distinct from demo', () => {
  const local = connectionView({ mode: 'live', authMode: 'local' });
  assert.equal(local.badge, '本机直连');
  assert.match(local.notice, /真实任务/);
  assert.doesNotMatch(Object.values(local).join(' '), /令牌|DEMO|模拟/);
  assert.notEqual(local.badge, connectionView({ mode: 'demo' }).badge);
});

test('startup and failed connections remain real-data states until demo is explicitly selected', () => {
  const loading = connectionView({ mode: 'connecting' });
  const failed = connectionView({ mode: 'offline', error: '服务暂时不可用' });
  assert.match(loading.notice, /正在连接本机/);
  assert.match(failed.action, /重试/);
  assert.doesNotMatch(failed.notice, /模拟|演示|令牌/);
  assert.doesNotMatch(connectionView().notice, /演示|模拟/);
  assert.match(connectionView({ mode: 'demo' }).notice, /模拟任务/);
});

test('degraded local connections identify cached data and locked writes during retry', () => {
  const degraded = connectionView({ mode: 'live', authMode: 'local', stale: true });
  assert.match(degraded.notice, /上次同步/);
  assert.match(degraded.notice, /写入操作暂时锁定/);
  assert.match(degraded.notice, /自动重试/);
  assert.match(degraded.footer, /CACHED/);
});

test('only an explicitly required token mode presents the owner login state', () => {
  assert.match(connectionView({ mode: 'token_required' }).notice, /令牌访问模式/);
  assert.doesNotMatch(connectionView({ mode: 'offline' }).notice, /令牌/);
  assert.doesNotMatch(connectionView({ mode: 'connecting' }).notice, /令牌/);
});

function fields(overrides = {}) {
  return {
    objective: '  整理指定主题的证据并交付报告  ',
    criteria: '每个结论具有来源\n\n明确标注未解决的问题',
    deliverables: 'report.md\nnotes/sources.txt',
    domains: '',
    maxSteps: '20',
    maxCalls: '30',
    deadlineHours: '24',
    allowFetch: false,
    ...overrides,
  };
}

function goal(status, calls = 0, inputTokens = 0, outputTokens = 0) {
  return {
    goal_id: `test-${status}`,
    contract: {
      objective: `Goal ${status}`,
      max_steps: 20,
      max_model_calls: 30,
    },
    state: {
      goal_id: `test-${status}`,
      status,
      steps: 0,
      model_calls: calls,
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      observations: [],
      artifacts: [],
      updated_at: new Date(NOW).toISOString(),
    },
    created_at: new Date(NOW).toISOString(),
  };
}

function rejects(overrides, message) {
  assert.throws(() => buildContract(fields(overrides)), Error, message);
}

test('every backend status has a distinct, readable presentation', () => {
  const statuses = [
    'queued', 'running', 'waiting', 'blocked', 'succeeded', 'failed',
    'cancelled', 'budget_exhausted', 'deadline_exceeded',
  ];
  for (const status of statuses) {
    assert.equal(typeof STATUS_META[status], 'object', `missing ${status}`);
    assert.equal(typeof STATUS_META[status].label, 'string');
    assert.ok(STATUS_META[status].label.trim().length > 0);
  }
  assert.notEqual(STATUS_META.waiting.label, STATUS_META.blocked.label);
  assert.notEqual(STATUS_META.running.label, STATUS_META.succeeded.label);
});

test('waiting and blocked goals remain resumable while exhausted budgets are terminal', () => {
  for (const status of ['queued', 'running', 'waiting', 'blocked']) {
    assert.equal(TERMINAL.has(status), false, status);
  }
  for (const status of ['succeeded', 'failed', 'cancelled', 'budget_exhausted', 'deadline_exceeded']) {
    assert.equal(TERMINAL.has(status), true, status);
  }
});

test('summary keeps active, waiting, blocked and successful goals separate', () => {
  const goals = [
    goal('running', 3, 100, 20),
    goal('waiting', 2, 50, 10),
    goal('blocked', 4, 200, 30),
    goal('succeeded', 6, 300, 40),
  ];
  const snapshot = structuredClone(goals);
  assert.deepEqual(summarize(goals), {
    total: 4,
    active: 1,
    waiting: 1,
    blocked: 1,
    completed: 1,
    modelCalls: 15,
    tokens: 750,
  });
  assert.deepEqual(goals, snapshot, 'calculating a summary must not mutate live data');
});

test('empty data produces zero summary values', () => {
  assert.deepEqual(summarize([]), {
    total: 0, active: 0, waiting: 0, blocked: 0, completed: 0, modelCalls: 0, tokens: 0,
  });
});

test('budget percentages measure consumption and do not mark a running goal complete', () => {
  assert.equal(budgetPercent(3, 4), 75);
  assert.equal(budgetPercent(40, 30), 100);
  assert.equal(budgetPercent(-1, 30), 0);
  assert.equal(budgetPercent(10, 0), 0);
  const current = goal('running', 30);
  assert.equal(budgetPercent(current.state.model_calls, current.contract.max_model_calls), 100);
  assert.equal(summarize([current]).completed, 0);
  assert.equal(current.state.status, 'running');
});

test('invalid numeric budget values never produce a NaN or infinite progress width', () => {
  for (const [used, maximum] of [[NaN, 20], [undefined, 20], [2, NaN], [2, undefined]]) {
    const percent = budgetPercent(used, maximum);
    assert.ok(Number.isFinite(percent));
    assert.ok(percent >= 0 && percent <= 100);
  }
});

test('goal titles use the objective first line and keep long text bounded', () => {
  const current = goal('running');
  current.contract.objective = 'Research evidence\nDo not include this second line';
  assert.equal(goalTitle(current), 'Research evidence');
  current.contract.objective = 'x'.repeat(200);
  assert.ok(goalTitle(current).length <= 73, '72 characters plus optional ellipsis');
  assert.ok(goalTitle(current).startsWith('x'));
});

test('goal titles preserve text as data for DOM textContent rendering', () => {
  const current = goal('running');
  current.contract.objective = '<img src=x onerror=alert(1)>';
  assert.equal(goalTitle(current), '<img src=x onerror=alert(1)>');
});

test('relative time accepts a fixed clock and handles missing/invalid dates gracefully', () => {
  const justNow = relativeTime(new Date(NOW).toISOString(), NOW);
  const older = relativeTime(new Date(NOW - 2 * 60_000).toISOString(), NOW);
  assert.equal(typeof justNow, 'string');
  assert.equal(typeof older, 'string');
  assert.notEqual(older, justNow);
  assert.equal(relativeTime(new Date(NOW - 2 * 60_000).toISOString(), NOW), older);
  for (const value of ['', undefined, 'not-a-date']) {
    const rendered = relativeTime(value, NOW);
    assert.equal(typeof rendered, 'string');
    assert.ok(!/NaN|Invalid Date/.test(rendered));
  }
});

test('contract builder trims form text and produces backend field names', () => {
  const contract = buildContract(fields());
  assert.equal(contract.objective, '整理指定主题的证据并交付报告');
  assert.deepEqual(contract.acceptance_criteria, ['每个结论具有来源', '明确标注未解决的问题']);
  assert.deepEqual(contract.deliverables.map((item) => item.path), ['report.md', 'notes/sources.txt']);
  assert.deepEqual(contract.allowed_tools, ['read_file', 'write_file', 'list_files']);
  assert.deepEqual(contract.allowed_domains, []);
  assert.equal(contract.max_steps, 20);
  assert.equal(contract.max_model_calls, 30);
  assert.equal(contract.deadline_seconds, 86400);
  assert.equal('maxCalls' in contract, false);
  assert.equal('allowFetch' in contract, false);
});

test('fetch permission is opt-in and requires exact normalized domains', () => {
  const contract = buildContract(fields({
    allowFetch: true,
    domains: '  DOCS.EXAMPLE.COM  \nexample.org\n',
  }));
  assert.ok(contract.allowed_tools.includes('fetch_url'));
  assert.deepEqual(contract.allowed_domains, ['docs.example.com', 'example.org']);
  rejects({ allowFetch: true, domains: '  \n ' }, 'fetch needs an explicit domain allowlist');
});

test('objective and acceptance criteria are mandatory and bounded', () => {
  rejects({ objective: '  ' });
  rejects({ objective: 'x'.repeat(8001) });
  assert.equal(buildContract(fields({ objective: 'x'.repeat(8000) })).objective.length, 8000);
  rejects({ criteria: '\n\n' });
  rejects({ criteria: 'x'.repeat(2001) });
  rejects({ criteria: Array.from({ length: 21 }, (_, i) => `criterion ${i}`).join('\n') });
});

test('deliverable counts and relative file paths are checked before submission', () => {
  rejects({ deliverables: '' });
  rejects({ deliverables: Array.from({ length: 21 }, (_, i) => `report-${i}.md`).join('\n') });
  rejects({ deliverables: 'x'.repeat(241) });
  for (const path of [
    '../secret', '/etc/passwd', './report.md', '.env', 'nested/.hidden',
    'nested/../secret', 'nested\\report.md', 'report\u0000.md',
  ]) {
    rejects({ deliverables: path }, `unsafe deliverable: ${JSON.stringify(path)}`);
  }
});

test('domain allowlists reject schemes, paths, credentials, wildcards and ports', () => {
  for (const domain of [
    'https://example.com', 'example.com/path', 'user@example.com',
    '*.example.com', 'example.com:443', 'example.com\\path',
    'example.com?query', 'example.com#fragment',
  ]) {
    rejects({ allowFetch: true, domains: domain }, `unsafe domain: ${domain}`);
  }
  rejects({
    allowFetch: true,
    domains: Array.from({ length: 31 }, (_, i) => `source-${i}.example.com`).join('\n'),
  });
});

test('integer step and model-call budgets accept backend boundaries', () => {
  const minimum = buildContract(fields({ maxSteps: '1', maxCalls: '1' }));
  assert.equal(minimum.max_steps, 1);
  assert.equal(minimum.max_model_calls, 1);
  const maximum = buildContract(fields({ maxSteps: '200', maxCalls: '300' }));
  assert.equal(maximum.max_steps, 200);
  assert.equal(maximum.max_model_calls, 300);
});

test('integer budgets reject empty, fractional, nonnumeric and out-of-range input', () => {
  for (const value of ['', ' ', '1.5', '20abc', 'NaN', 'Infinity', '-1', '0', '201']) {
    rejects({ maxSteps: value }, `invalid step budget: ${value}`);
  }
  for (const value of ['', ' ', '2.5', '30abc', 'NaN', 'Infinity', '-1', '0', '301']) {
    rejects({ maxCalls: value }, `invalid model-call budget: ${value}`);
  }
});

test('deadline hours convert to seconds and preserve the backend minimum and maximum', () => {
  assert.equal(buildContract(fields({ deadlineHours: String(1 / 360) })).deadline_seconds, 10);
  assert.equal(buildContract(fields({ deadlineHours: '720' })).deadline_seconds, 2_592_000);
  assert.equal(buildContract(fields({ deadlineHours: '1.5' })).deadline_seconds, 5400);
  for (const value of ['', ' ', '0', '-1', '0.001', '720.1', '24hours', 'NaN', 'Infinity']) {
    rejects({ deadlineHours: value }, `invalid deadline: ${value}`);
  }
});

test('form validation errors are readable Chinese rather than raw backend errors', () => {
  for (const badFields of [{ objective: '' }, { maxSteps: '0' }, { deliverables: '../secret' }]) {
    assert.throws(() => buildContract(fields(badFields)), (error) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /[\u3400-\u9fff]/);
      return true;
    });
  }
});

test('demo goals are explicitly namespaced and have internally consistent finite data', () => {
  const goals = demoGoals(NOW);
  assert.equal(goals.length, 6);
  assert.equal(new Set(goals.map((item) => item.goal_id)).size, goals.length);
  for (const item of goals) {
    assert.match(item.goal_id, /^demo-/);
    assert.equal(item.state.goal_id, item.goal_id);
    assert.ok(STATUS_META[item.state.status]);
    assert.ok(item.contract.objective.length > 0);
    assert.ok(Number.isFinite(Date.parse(item.created_at)));
    assert.ok(Number.isFinite(Date.parse(item.state.updated_at)));
    for (const number of [item.state.steps, item.state.model_calls, item.state.input_tokens, item.state.output_tokens]) {
      assert.ok(Number.isFinite(number) && number >= 0);
    }
  }
});

test('demo snapshots are independent and cannot mutate a later demo or live list', () => {
  const live = [goal('running', 1)];
  const before = structuredClone(live);
  const first = demoGoals(NOW);
  const second = demoGoals(NOW);
  assert.deepEqual(first, second, 'fixed-clock demo data should be deterministic');
  first[0].contract.objective = 'Changed locally';
  first[0].state.model_calls = 9999;
  assert.notEqual(second[0].contract.objective, 'Changed locally');
  assert.notEqual(second[0].state.model_calls, 9999);
  assert.deepEqual(live, before);
  assert.ok(live.every((item) => !item.goal_id.startsWith('demo-')));
});

const MODELS = [
  { id: 'gpt-4.1-mini', label: 'GPT-4.1 mini', reasoning_efforts: [] },
  { id: 'reasoning-small', label: 'Reasoning small', reasoning_efforts: ['low', 'medium', 'high'] },
  { id: 'reasoning-large', label: 'Reasoning large', reasoning_efforts: ['none', 'low', 'medium', 'high', 'xhigh'] },
];

test('reasoning choices come from each model capability list, never a universal set', () => {
  assert.deepEqual(reasoningChoices(MODELS, 'gpt-4.1-mini'), []);
  assert.deepEqual(reasoningChoices(MODELS, 'reasoning-small'), ['low', 'medium', 'high']);
  assert.deepEqual(reasoningChoices(MODELS, 'unknown-model'), []);
  assert.equal(reasoningChoices(MODELS, 'reasoning-small').includes('xhigh'), false);
});

test('switching models preserves supported effort but resets unsupported effort to model default', () => {
  assert.deepEqual(normalizeModelSelection(MODELS, { model: 'reasoning-small', reasoning_effort: 'high' }), {
    model: 'reasoning-small', reasoning_effort: 'high',
  });
  assert.deepEqual(normalizeModelSelection(MODELS, { model: 'reasoning-small', reasoning_effort: 'xhigh' }), {
    model: 'reasoning-small', reasoning_effort: null,
  });
  assert.deepEqual(normalizeModelSelection(MODELS, { model: 'gpt-4.1-mini', reasoning_effort: 'high' }), {
    model: 'gpt-4.1-mini', reasoning_effort: null,
  });
});

test('model default stays null and is distinct from supported none reasoning', () => {
  for (const effort of [null, undefined, '']) {
    assert.equal(normalizeModelSelection(MODELS, { model: 'reasoning-large', reasoning_effort: effort }).reasoning_effort, null);
  }
  assert.equal(normalizeModelSelection(MODELS, { model: 'reasoning-large', reasoning_effort: 'none' }).reasoning_effort, 'none');
  assert.equal(reasoningLabel(null), '模型默认');
  assert.equal(reasoningLabel('max'), '最高（max）');
  assert.notEqual(reasoningLabel('none'), reasoningLabel(null));
});

test('unknown models cannot be submitted as a settings selection', () => {
  assert.throws(() => normalizeModelSelection(MODELS, { model: 'invented-model', reasoning_effort: 'high' }), /请选择/);
  assert.throws(() => normalizeModelSelection([], { model: 'reasoning-small' }), /请选择/);
});

test('legacy tasks never inherit current model defaults in their displayed record', () => {
  const legacy = goal('succeeded');
  assert.equal(recordedModelSettings(legacy), null);
  assert.equal(modelSettingsLabel(recordedModelSettings(legacy)), '未记录');
  legacy.model_settings = null;
  assert.equal(recordedModelSettings(legacy), null);
  const currentDefault = { model: 'reasoning-large', reasoning_effort: 'xhigh' };
  legacy.model_settings = { model: 'gpt-4.1-mini', reasoning_effort: null };
  const historical = recordedModelSettings(legacy);
  assert.deepEqual(historical, { model: 'gpt-4.1-mini', reasoning_effort: null });
  assert.equal(modelSettingsLabel(historical), 'gpt-4.1-mini · 模型默认');
  assert.notEqual(modelSettingsLabel(historical), modelSettingsLabel(currentDefault));
});

test('catalog normalization removes duplicate choices without mutating service data', () => {
  const incoming = [...MODELS, { id: 'reasoning-small', label: 'Duplicate', reasoning_efforts: ['xhigh'] }, null, { id: '' }];
  const before = structuredClone(incoming);
  assert.equal(modelCatalog(incoming).length, 3);
  assert.deepEqual(incoming, before);
  const catalog = modelCatalog([{ id: 'custom', reasoning_efforts: ['high', 'high', null, ''] }]);
  assert.deepEqual(catalog, [{ id: 'custom', label: 'custom', reasoning_efforts: ['high'] }]);
});
