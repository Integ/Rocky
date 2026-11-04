# Validation log

Chronological record of what was checked at each stage. Entries describe the
code at the time they were written; the first section covers the 0.1 foundation.
See [CHANGELOG](../CHANGELOG.md) for what changed since.

## v0.1 foundation

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

## Storage accounting and sanitized support exports

Validated on 2026-11-01 (America/Vancouver):

- Full Python suite: **1299 passed, 3 skipped**; the two existing Temporal sandbox
  import warnings remain. Native Swift suite: **105 passed**. Ruff lint/format
  and diff whitespace checks passed.
- Eleven new Python checks cover category separation, SQLite sidecars, caches,
  overlapping roots and hard links, symlink traversal and directory replacement,
  scan limits, unreadable roots, refresh coalescing, authentication, component
  failures and export allowlists. Fixtures plant private text in task data,
  filenames, logs, credentials and environment variables and verify it never
  appears in the decompressed diagnostic files.
- Three native checks cover partial-use labels, authenticated read-only requests,
  exact binary export bytes, retention of prior statistics after a failed refresh
  and an independent successful export.
- Built and restarted the bundled native application. The live database, worker
  and Temporal reported healthy; the running backend matched the new bundle.
  A complete metadata scan took 0.008 seconds on the local data set. This is one
  small-tree observation, not a large-tree performance benchmark.
- Verified all six categories and explicit manual refresh in the native sheet.
  Saved a ZIP through the native save panel to a temporary directory, checked its
  success message, CRC, fixed member names, complete statistics and absence of
  home-directory paths or endpoint URLs. No live model calls, task decisions,
  cleanup operations or external uploads were performed.

## Concurrency and owner-review fixes

Validated on 2026-11-02 (America/Vancouver):

- Reviewed the task handoff/delivery and storage/diagnostic changes. Reproduced
  cancellation during threaded model enqueue, a stale native refresh overwriting
  acceptance, an old pending delivery disappearing outside the recent page, and
  backup files disappearing during listing or before the second status read.
  Added regression coverage and fixed each path; archive/delete and review are
  mutually exclusive in the native client.
- Full regression also exposed an intermittent missing-info evaluation failure.
  Its execution record contained `operation_storage_unavailable` for one of two
  parallel first reads. A local minimal reproduction traced this to a concurrent
  `O_CREAT` open of the same new lock file returning `FileNotFoundError`, although
  the other caller's lock already existed and an immediate open succeeded.
  Tool receipts, HTTP sessions, backups and service initialization now open an
  existing lock or create it exclusively and reopen the winning inode. They
  reject symlinks, hard links and special files without blocking.
- After the lock fix, 400 fresh two-read batches (800 reads) retained every
  result. The affected tool/HTTP/backup suites passed **99 tests**. New lock
  tests also cover private, non-inheritable shared inodes and receipt replay.
- Final full Python suite: **1309 passed, 3 skipped**, with two existing Temporal
  import warnings. Full Swift suite: **107 passed**. Ruff lint/format and diff
  whitespace checks passed. The listing test now explicitly accepts completed
  fixtures before asserting that they can fall outside the recent window;
  pending deliveries are tested separately, including legacy metadata records.
- Rebuilt and restarted the native bundle. The running backend matched its build
  ID and reported healthy database, worker, Temporal and backup status. No live
  model requests, owner delivery decisions or external uploads were performed.

## 0.2.0 release candidate

Reviewed and validated on 2026-11-02 (America/Vancouver):

- Reviewed the changes since the last passing CI commit `65d119b`, including
  current uncommitted concurrency fixes. Checked delivery revision/transaction
  boundaries, immutable artifact reads, event rule quotas and pagination, model
  admission cancellation, workflow patch gates and diagnostic allowlists.
- Both failed CI runs (`41395489515` and `41397626240`) stopped while compiling
  `DeliveryHistoryView.export` on the macOS 14 runner's default Xcode 15.4.
  The function now explicitly uses `@MainActor` for `NSSavePanel`, preserving
  compatibility with older SwiftUI isolation rules.
- Full Python suite: **1309 passed, 3 skipped**, using a cached real Temporal
  test server outside the execution sandbox. Two existing Temporal import
  warnings remain. Full native Swift suite: **107 passed** on Swift 6.4.2.
  Ruff lint, formatting and diff whitespace checks passed.
- No live model calls were used for this release review. Local checks do not
  replace the macOS 14 and macOS 26 CI gates required before publication.

## Native app real usage validation

Validated on 2026-11-03 (America/Vancouver), commit `b22fce9`, bundled Rocky
0.2.0 on macOS 27.2, with real `gpt-6.1-sol` requests and real tools:

- Used the native app to draft and submit a synthetic CSV task, approve individual
  commands and a selected approval-center item, restart the backend while an
  approval was pending, extend an exhausted budget, request a revised delivery,
  preview/export history, and accept the second delivery. Checkpoints, pending
  approval identity and file hashes survived the backend restart. Work continued
  with the window closed and survived a client quit/relaunch.
- Independently ran the generated script against the original fixture and a
  changed fixture; both passed. All eight archived files across two deliveries
  matched their SHA256 values. The second report contained exactly the requested
  additional line, and its other three files were unchanged. The old report
  exported through the native save panel matched its original archive.
- Found two unresolved verification problems: consumed owner answers are absent
  from the review evidence, causing an answered name question to be asked again;
  the computed-claim audit rejects valid calculations containing expected-value
  assertions. Reproduced both behaviors offline from the collected records and
  current evidence functions. The CSV task initially exhausted its AI-estimated
  12-step budget after two review rejections, then completed after extension.
- An existing invalid saved key caused only a generic submission error. Replaced
  it with the working environment key after explicit owner authorization. No
  credential values are included in the report. Finder reopening of a closed
  window needs manual follow-up; a full client quit/relaunch restored it.
- Two task runs plus contract drafting used **29 model calls / 243065 total
  input and output tokens**. The main delivery is accepted; the failing
  clarification task is cancelled with its files retained. Final service status
  was healthy, with no active tasks or pending approvals. Product code was not
  changed and the full automated suite was not rerun for this validation.

Detailed reproduction steps, scope and structured evidence:
[real usage report](reports/2026-11-03-real-usage-validation.md).

## Real usage findings fixed

Validated on 2026-11-03 (America/Vancouver), after the real usage investigation:

- Reviews now include persisted questions and consumed owner replies, with
  auditable `question:` and `owner:` citations. Tests cover question-bound and
  ordinary replies, exclusion of unconsumed/foreign input, and whole-record
  omission under context pressure. Tool permissions remain separate.
- Python comparison assertions no longer make calculated values appear to be
  literal answers. Replayed the original rejected CSV claim unchanged and
  verified acceptance. Regression coverage still rejects literal assignments,
  printing answers, side effects inside assertions, compound commands and
  failed computation evidence, including the automatic verification fallback.
- Fixed provider error categories cross the local API without upstream text.
  Native Chinese messages distinguish authentication, access, exhausted quota,
  rate limits, connectivity and timeouts; exhausted quota is non-retryable.
  Tests exercise SDK and compatible-provider errors and hostile error bodies.
- Finder/Dock reopen now restores a main window rather than reusing a retained
  closed window. Rebuilt the complete offline-capable app and verified real
  close/reopen without changing the client process ID. Final build ID:
  `3DE29ACD-1F07-49F5-950C-6EFE038CF279`.
- Full Python suite: **1338 passed, 3 skipped**, using the cached real Temporal
  test service; two existing Temporal import warnings remain. Swift:
  **108 passed**. Ruff lint/format and diff whitespace checks passed.
- Repeated the original missing-name task with real `gpt-6.1-sol`, replying once
  through its question card and once through the ordinary composer. Both
  succeeded in **4 steps / 5 model calls**, and both deliveries were accepted.
  Their reviews cite the actual owner receipts/questions; no repeated question
  or budget increase was needed. These two runs used **48188 tokens** total.
  Final service status was healthy, with no active tasks or pending approvals;
  bundled and staged backend sources match the final workspace sources.

The [report](reports/2026-11-03-real-usage-validation.md) preserves the original
failures; [fix evidence](reports/2026-11-03-fix-validation.json) records the
successful reviews and final checks.

## Neutral macOS interface

Validated on 2026-11-03 (America/Vancouver):

- Replaced the Tiffany accent with adaptive black/white actions and neutral
  canvas, sidebar, message and input surfaces. The new-task composer is
  centered beneath a system-font heading, with compact suggestion buttons.
  Task replies retain their bottom composer. The in-app Rocky mark is monochrome.
- Referenced the [official ChatGPT macOS Chat Bar screenshots](https://help.openai.com/en/articles/9295241-how-to-launch-the-chat-bar)
  for neutral surfaces and inverse action controls; no ChatGPT branding assets
  were copied. Existing success, warning, error and conventional link colors
  remain available for their semantic roles.
- Rendered actual theme, mark and button components off-screen under Aqua and
  Dark Aqua and visually checked enabled/disabled labels and inverse send
  buttons. This was a component preview; native-window inspection used the
  current light system appearance, without changing system preferences.
- Checked the rebuilt app's new-task page, suggestion-to-input interaction,
  enabled send button, task delivery page and settings sheet. Native sidebar
  selection is neutral gray, and pressing Down still changes the selected task.
  The sidebar bridge changes only selection drawing and does not intercept hits.
- Swift: **108 passed**; complete offline app build and signature verification
  passed; diff whitespace checks passed. Final build:
  `B674B9C7-6737-417C-BC9E-03E657FB2A77`. The app is running on the new-task page.
  No model calls or task submissions were needed for this interface change.

## Workspace file batches and execution efficiency

Validated on 2026-11-03 (America/Vancouver):

- File mutations may share one planning round with ordered dispatch, per-file
  persistence and existing tool receipts. New owner input, approval and unknown
  outcomes stop later mutations. Completed work survives cancellation and
  worker replacement. Pre-upgrade history replay passed with its original
  zero-write branch.
- Full Python suite: **1365 passed, 3 skipped**, including 27 new focused checks.
  Ruff lint, format and diff whitespace checks passed.
- Matched real-model evaluation: **3/3 expected outcomes** before and after,
  no false successes. Total calls **16 → 12** and tokens **109,299 → 79,139**.
  Cross-file refactoring used a five-file mutation batch and passed hidden tests;
  its model calls fell **9 → 6** and wall time **82.46 → 53.03 seconds**.
  These are single samples, not a general completion-rate estimate.
- Rebuilt and launched the native app, build
  `153D07EF-CB29-4962-92D5-B43C35A165FA`. A four-file synthetic task succeeded
  in 3 steps and 4 model calls. Exact UTF-8 contents were independently checked
  and the delivery was accepted in the native interface.

See the [execution report](reports/2026-11-03-execution-validation.md) and
[machine-readable evidence](reports/2026-11-03-execution-validation.json).

## File discovery and literal content search

Validated on 2026-11-03 (America/Vancouver):

- Added `find_files` and `search_files` with bounded descriptor-relative
  traversal, glob filtering, line/context results, pagination and explicit
  completeness/skip metadata. Both share existing approval, isolation and
  receipt handling and support concurrent read-only batches. Search outputs
  are review evidence without marking the source completely read.
- **53** focused checks passed; full Python suite **1420 passed, 3 skipped**;
  Swift **109 passed**. Ruff, format and whitespace checks passed. Production
  batch replay and pre-upgrade history replay passed.
- Rebuilt and launched the signed app, build
  `A18E526F-2E2C-44FF-AEFE-102B8ED75B74`. Bundled sources match the workspace.
- A real-model task over 28 synthetic files called both new tools, excluded a
  distractor and delivered correct relative paths, source lines and one-based
  line numbers. It succeeded in **5 steps, 6 calls, 37,908 tokens**, with no
  commands or extra approvals. Actual artifacts were independently checked;
  native Chinese tool labels and accepted delivery were verified.

See the [tool validation report](reports/2026-11-03-tool-expansion-validation.md)
and [structured evidence](reports/2026-11-03-tool-expansion-validation.json).

## Quality review of file tools and review integration

Validated on 2026-11-03 (America/Vancouver):

- Reproduced and fixed three P2 issues: search line numbers disagreeing with
  targeted reads on non-LF separators, regular files excluded by directory
  names, and quoted search evidence rejected because of JSON escaping.
  Search evidence retains decoded source text; quote auditing prefers literal
  text while retaining compatibility with escaped quotations.
- Added **30** regression cases. Full Python suite: **1450 passed, 3 skipped**.
  Ruff, format, whitespace and legacy history replay checks passed.
- A fixed-seed check over **120 files** compared **292 matching lines**, paged
  19 at a time, with independently computed paths, line numbers and contents;
  all matched with no duplicate results.
- Rebuilt and launched the signed native app, build
  `6619D54F-18C8-46AA-9BCF-4638C83D067A`. The same reproducer using bundled
  sources passed all three cases. The API, database and worker are healthy;
  there are no pending test approvals. No additional model calls were needed.

See the [quality report](reports/2026-11-03-quality-review.md) and
[before/after evidence](reports/2026-11-03-quality-review.json).

## Repeated live evaluations and isolated service reliability

Validated on 2026-11-03 (America/Vancouver), starting from `61e7775`:

- Original clean-source suite: **24/30 passed**, three grader false-success
  labels and no runner errors, using `gpt-6.1-sol`, default reasoning and
  three repetitions per task. The writing task requested a complete fictional
  configuration without specifying its schema; meeting ownership wording was
  ambiguous against the grader's historical-owner expectation.
- Supplied an authoritative fictional configuration/command reference and a
  hidden TOML parser check, and clarified decision-specific ownership without
  changing the expected owners. Six additional runs yielded **5/6 passed**,
  zero false successes and no runner errors. One writing run exhausted its
  token budget after 69 file reads before independent review.
- The current task-version observation baseline contains all 24 attempts from
  unchanged tasks plus all six attempts from revised tasks: **29/30**, zero
  false successes, with explicit source batches and task fingerprints. Its
  absolute readiness check correctly exits **1** because long-guide is 2/3.
  Task revisions are not evidence of an execution performance improvement.
- Reproduced concurrent fresh-schema creation and SQLite WAL-conversion busy
  errors. Initialization now serializes DDL/migrations, rolls back interrupted
  SQLite DDL and retries only bounded journal-conversion contention. Five
  groups of six independent cold-start processes passed. Final full suite:
  **1470 passed, 3 skipped**; Ruff, formatting and whitespace checks passed.
- Final source passed three real subprocess cycles killing worker, API and
  Temporal in turn, checking unchanged pending approvals, exact append bytes,
  injected model 503 recovery, due scheduling, debounced file events, idle
  backup/restore, artifact hashes and both SQLite integrity checks.
- An earlier source revision ran for **1801.88 seconds**, six fault cycles
  and 168 health/resource samples. It preceded the final WAL retry patch;
  final-source evidence is separately identified. This is not a 24–72 hour
  test of a single unrestarted process, Mac sleep/reboot, launchd, Keychain,
  PostgreSQL concurrency, or notarized installation.

See the [readiness report](reports/2026-11-03-v03-readiness.md),
[structured evidence](reports/2026-11-03-v03-readiness.json),
[observed baseline](../evals/baselines/gpt-6.1-sol-v03-observed.json) and
[reliability procedure](reliability.md). Raw runs, databases, logs and exact
source archives are retained under ignored `data/validation/2026-11-03-readiness/`.

## Long-document execution and review-quote efficiency

Validated on 2026-11-03 (America/Vancouver), frozen candidate `15b22c9`:

- The planner retains completed comparisons and concrete check references,
  retrieves specific missing details, and saves reusable document checks.
  The reviewer uses atomic claims and short exact source-ordered excerpts.
  Planner/reviewer modules are now versions 2/3. The quote schema retains its
  600-character cap; audit, permissions, workflow, task versions and budgets
  were not relaxed.
- An intermediate six-run experiment passed all grades but failed the
  predeclared code mean-token cap (225935 > 200000). Its complete evidence is
  retained separately. The final code pilot and remaining nine-task batch
  both use the same clean frozen source; all 30 attempts count in the result.
- Full live suite: **30/30 passed**, zero false successes and zero runner
  errors. Absolute readiness, four critical tasks passing every attempt,
  same-version relative regression checks and code/writing mean-token caps
  of 200000 all passed. Code averaged 46802 tokens versus 333511 before;
  long-guide averaged 78057 versus 317322. Several other tasks cost more,
  including fix-bug (+81%); all per-task results are reported.
- Final code tasks each used five steps, six model calls and one successful
  independent review. Writing tasks read files 9/6/2 times and passed one
  review each; the earlier budget failure had 69 reads before review.
- Final Python suite: **1471 passed, 3 skipped**. A focused set of 134 passed,
  including rejection of fabricated quote endings, reversed fragments and
  over-limit excerpts. Ruff, formatting and whitespace checks passed.
- The same source built an isolated developer macOS app. Native tar extraction
  and offline uv installation passed using unchanged cached dependencies;
  all 51 installed runtime source files match the candidate. Reapplied ad-hoc
  signing and deep strict verification passed. The existing app was not
  replaced or launched. No Developer ID/notarization or physical long-run
  checks were performed.

See the [efficiency report](reports/2026-11-03-efficiency-validation.md),
[structured evidence](reports/2026-11-03-efficiency-validation.json) and
[new baseline](../evals/baselines/gpt-6.1-sol-v03-efficient.json). Complete raw
attempts, databases, workspaces, exact source and build/install evidence are
retained under ignored `data/validation/2026-11-03-efficiency/`. The earlier
29/30 observation baseline and its source-specific reliability results remain
unchanged; physical 24–72 hour testing is the next stage.
