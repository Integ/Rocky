# v0.1 validation

Validated on 2026-10-20 (America/Vancouver), Python 3.12.15, with the committed
`uv.lock` dependency set.

- `ruff check .` and `ruff format --check .`: passed.
- `ROCKY_TEMPORAL_TEST_MODE=local pytest -q`: **90 passed** in 36.09 seconds.
  This starts real Temporal development services; only the model transport is
  replaced in automated tests. A Starlette/httpx deprecation warning remains.
- Actual model smoke test: **succeeded**, 4 model calls, 3,703 input tokens,
  183 output tokens. `readiness.txt` was generated and independently verified.
  Credentials were read from the process environment and were not saved to files.
- `docker compose config --quiet`: passed. Container build/start was not tested
  because Docker Engine was not running on the development host.

Integration coverage includes worker replacement, durable timers, blocked-state
resume, cancellation fences, finite budgets/deadlines, duplicate wake commands,
tool retry receipts, loss of dispatch acknowledgement, deterministic/semantic
completion checks, and concurrent SQLite projection races. Tool tests cover
path/link attacks, hostname restrictions, pinned DNS destinations, redirects,
compressed responses, and download size limits.

This is a single-owner prototype. The checks do not establish production
availability, multi-host coordination, or general factual accuracy of model
outputs. See architecture.md for the deployment boundary and future work.

## Model and reasoning controls

- Full Python suite: **143 passed**, including real Temporal integration tests.
  The API-to-artifact test changes defaults before dispatch and verifies that
  planning and independent review still use the goal's saved model and effort.
- Added coverage for authenticated settings, invalid model/effort combinations,
  SQL persistence/reopening, concurrent first writes, idempotent submission after
  defaults change, legacy databases and legacy tool-receipt fingerprints.
- Live goal: **succeeded**, 3 model calls, 2,633 input tokens, 194 output tokens.
  The actual file contained `Rocky is ready.` with an allowed trailing newline.
  The original GPT-4.1 mini default was restored after verification; the completed
  goal retained its recorded model and effort.
- The existing local API and worker were restarted with the new code. Temporal,
  the existing completed goal, databases and artifacts were retained.

## Standalone local API access

- Full Python suite: **268 passed**, including real Temporal integration tests.
- Local-access tests cover anonymous goal/settings operations, raw peer checks,
  strict Host validation, duplicate headers, DNS rebinding authorities, origin
  scheme/host/port matching, IPv6, spoofed proxy headers and Fetch Metadata.
  Token-mode authentication remains
  covered explicitly, including failure without a configured credential.
- The native API was restarted in `local` mode without a provider key or owner
  token in its environment. Anonymous goals calls returned 200;
  cross-origin and cross-site API calls returned 403. Local OpenAPI has no Bearer
  requirement. Both existing completed goals remained available.
- The real native CLI listed goals with `ROCKY_API_TOKEN` removed from its
  environment. Docker Compose configuration validation passed in explicit token
  mode. No additional model calls were used for this change.

## Task history and unlimited execution

Validated on 2026-10-21 (America/Vancouver):

- Python: **336 non-integration tests** and **26 integration tests** passed.
  Integration checks used the cached Temporal test service and mocked model transports.
- Ruff lint/format and diff whitespace checks passed.
- macOS: **41 Swift tests** passed; release application rebuilt and signature verified.
- Lifecycle coverage includes archived/recent list isolation, restore, terminal-only deletion,
  legacy schema upgrades, deletion/command races, stale refreshes and idempotent retries.
- Temporal replay checks cover replacing a worker with an active finite-budget history,
  exceeding its old step/call/deadline limits, and preserving a blocked task until real input.

## macOS-only client

Validated on 2026-10-21 (America/Vancouver):

- Full Python suite: **365 passed**, using the cached Temporal test service and
  mocked model transports. **41 Swift tests** passed.
- Ruff lint/format, diff whitespace and Docker Compose configuration checks passed.
- Browser routes and static assets are removed; regression tests verify 404 responses
  in both local and token modes while preserving API authentication and settings.
- Temporal runs headless in the macOS app, Compose and documented development commands.
- Rebuilt `dist/Rocky.app` and verified its signature. Its bundled API serves native
  client endpoints with no frontend assets or interactive API docs. The built Python
  wheel also contains no HTML, JavaScript or CSS assets.

## Six-phase harness and pluggable memory

Validated on 2026-10-21 (America/Vancouver):

- Full Python suite: **399 passed**, using the cached real Temporal test service,
  local HTTP fixtures and mocked model transports. No live model calls were made.
  The existing Starlette/httpx deprecation warning remains.
- Native compatibility: **41 Swift tests passed**, including state decoding with
  additional harness fields and execution history behavior.
- Regression coverage includes six-phase ordering and durable phase records,
  planning/tool/hook failures, automatic recovery before blocked sleep, distinct
  actions with identical results, real-input recovery resets, cancellation and
  wake races, worker replacement and pre-harness Temporal history replay.
- Storage checks retain 150 phase records and 51 meaningful messages over 25
  cycles, exercise idempotent/stale writes and deletion races, and recover legacy
  observations that have left the workflow's bounded state window.
- Memory checks cover history/knowledge providers, summary precedence, Chinese,
  Unicode and JSON escaping, whole-envelope budget enforcement, custom token
  counters and oversized injected memory at the model boundary.
- Ruff lint/format and diff whitespace checks passed.
- Rebuilt `dist/Rocky.app`; strict deep code-signature verification passed.
  Packaged Python sources, README and dependency manifests match the workspace.

## Workflow history limit and stale running state

Validated on 2026-10-21 (America/Vancouver):

- Incident: Temporal terminated an execution at 18:28:43 with
  `Workflow history size exceeds limit.` Its history was 52,558,335 bytes and
  5264 events; SQL still showed `running` at step 54, cycle 39, after a completed
  action. Repeated full-state Activity payloads had accumulated without rollover.
- Full Python suite: **424 passed** in 33.49 seconds with mocked model transports.
  Existing Starlette/httpx and Temporal sandbox dependency warnings remain.
- Added real Temporal tests cover history rollover chains and per-run replay,
  cumulative usage/revisions/operation IDs, recovery counters and wake-command
  deduplication across runs, pending wake/cancel at rollover, pre-maintenance
  six-phase history upgrade, completed-action checkpoint recovery without
  repeating the tool, and real engine termination followed by SQL reconciliation.
- Reconciliation unit tests cover closed engine states, bounded scanning,
  independent outbox dispatch, stale revisions/recovered runs, deleted tasks and
  sanitization of engine diagnostics. Lint, format and diff whitespace checks pass.
- Corrected the affected task's SQL projection from `running` revision 535 to
  `failed` revision 536 using the confirmed terminal engine result. Step count,
  model call count and workspace files were retained; no task restart or live
  model request was performed.


## Adaptive harness, layered context and approval gates

Implemented on 2026-10-21 (America/Vancouver). Earlier entries describe the
historical fixed-phase/unlimited behavior at those validation points; the
current architecture uses optional checkpoints and `contract.limits`.

- Full Python suite: **488 passed** in 44.99 seconds with the cached real
  Temporal test service, local HTTP fixtures and mocked model transports.
  Final context/planner/harness checks: **51 passed** after the last context
  changes. No live provider calls or credential changes were used.
- Native client: **43 Swift tests passed**, including exact-operation approval
  requests and explicit finite/unbounded execution-limit serialization.
- Context regressions cover large summaries preserving recent raw errors and
  knowledge, multiple rolling result excerpts, bounded SQL recall, multibyte
  JSON/token accounting, persistent explicit progress, accumulated summaries,
  oversized essential contracts failing before dispatch, and safe actionable
  schema errors without echoing secret input values.
- New execution checks cover optional phase dispatch, cumulative step/call/token
  limits and timeouts, artifact hashes rechecked after model review, checkpoint continuation,
  append-only events, exact-operation approval and curated memory corrections.
  Strict argument errors and unknown outcomes reach the next planning call.
- Ruff lint/format and diff whitespace checks passed. The local `dist/Rocky.app`
  was rebuilt with the updated backend and native controls; strict deep signing
  verification passed. Existing Starlette/httpx and Temporal sandbox dependency
  warnings remain. Historic live-model results above do not validate this code.

## Planner visibility and refreshed evidence

Validated on 2026-10-21 (America/Vancouver):

- Full Python suite: **496 passed**, using the cached Temporal test service and
  mocked model transports. Ruff lint/format and diff whitespace checks passed.
- Measured with the memory builder: after reading a 19,529-byte Chinese text file,
  the next planning call previously received about 600 bytes of it; it now receives
  the whole file. A 15 KB code read stays whole while older results are shortened.
- New checks cover the newest-result share, unescaped tool bodies, memory shrinking
  under a large contract instead of failing, neutral one-time refreshes, immediate
  and second repeats still entering recovery, refresh state across continue-as-new,
  and replay of an adaptive history recorded before the rule.
- No live model calls were made. Per-step input tokens can grow up to about
  24,000 conservative tokens of memory; the default 500,000-token task limit is unchanged.

## Evaluation suite

Validated on 2026-10-21 (America/Vancouver):

- Added `evals/`: ten representative tasks (data, log analysis, three code tasks,
  multi-file synthesis, document QA, long-form writing, a local login/pagination API
  and a missing-input case) with an independent grader, step metrics, reports and
  baseline comparison. `scripts/run_evals.py run` executes them in-process against a
  live model; nothing in this entry was run against a live model.
- Every task's reference solution passes its checks and its untouched fixtures fail
  them; expected answers are checked to be absent from the planner-visible contract.
- Runner integration tests drive the real workflow, tools and SQLite store with a
  scripted planner on both the time-skipping and local Temporal servers: approval
  counting and auto-approval, the cancel policy, a false success caught only by the
  grader, a missing-input stop graded as correct, and the local API task through
  Rocky's own HTTP tools.

## Evidence-based, structured review

Validated on 2026-10-21 (America/Vancouver):

- Full Python suite: **553 passed**; Ruff lint/format and diff whitespace checks passed.
- Tool results list the files each action changed; independent review receives sources,
  changed files, deliverable provenance and the action log, and returns cited claims that
  the application audits. Values the application can trace itself need no citation.
- Five live evaluation runs with `gpt-4.1-mini` and `--approvals auto`, about $2.01 in
  total at list prices. Each run is a single sample:

  | Code state | Passed | False successes |
  |---|---|---|
  | Original verifier | 3/10 | 3 (csv-summary, log-triage, meeting-synthesis) |
  | Review evidence | 4/10 | 3 (csv-summary, docs-qa, meeting-synthesis) |
  | First claim audit | 1/10 | 0 (supported claims were rejected) |
  | Application-verified values | 5/10 | 0 |
  | Final quote and citation matching | 5/10 | 2 (add-feature, rename-across-files) |

- Replaying the audit on the recorded runs: every earlier false success on a JSON
  deliverable is rejected even when the reviewer passes everything, and every correct,
  reviewer-approved JSON result passes. The two remaining false successes are code that
  fails hidden tests; non-JSON deliverables still depend on the reviewer's judgment.
- In the first run, auto-approval executed a model command whose backtick substitution
  ran `brew install atlas-cli`; the formula does not exist and nothing was installed.
  Command substitution in approved commands is not yet flagged.

## Single execution loop

Validated on 2026-10-22 (America/Vancouver):

- Removed the legacy and fixed six-phase loops, all Temporal patch markers and
  their runtime switches. `workflow.py` now has one adaptive execution loop and
  decreased from 1290 to 895 lines. Continuation checkpoints retain only state
  needed between complete rounds.
- Python suite: **624 passed** across batches: 551 non-integration tests, 60
  workflow tests and 13 other integration tests. Tests used the cached local
  Temporal test service, local HTTP fixtures and mocked model transports; no
  live model calls were made. HTTP fixtures require local port access outside
  the restricted execution sandbox. Existing dependency warnings remain.
- Current-version replay, worker replacement, cancellation, durable waits,
  approval, budgets, recovery, evidence refresh and continuation remain covered.
  The replay test also checks that new histories contain no patch markers.
- Ruff lint/format and diff whitespace checks passed. Unfinished histories from
  earlier execution versions are no longer supported; stored task records and
  workspace files are retained, without automatic history migration.
