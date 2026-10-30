# Execution architecture

Rocky targets one owner on one Mac (current version 0.2.0). The model proposes one step at a time;
ordinary code owns state, execution scope, usage and completion gates.

## Durable boundaries

1. API commits a goal and submission outbox record before returning HTTP 202.
2. The worker dispatcher starts a Temporal Workflow with the goal ID and
   `REJECT_DUPLICATE`. If start succeeds but database acknowledgement fails,
   retry finds the same execution instead of starting a second goal.
3. Model and verifier calls record attempted usage in the Workflow before dispatch.
   SDK retries are disabled. Model Activities retry (3 attempts, honoring
   `Retry-After`) only for failures that produced no model output: HTTP 429,
   408/409, 5xx/529 overload and failed connections. Read timeouts and completed
   but unusable responses are `ModelDecisionError`s and are never repeated. One
   logical call counts once in the usage counters, however many attempts it took.
   Responses stream: a request fails after 300 seconds without any received
   bytes or 900 seconds in total, so a long answer that keeps arriving is not
   cut off. Model Activities heartbeat, so a lost worker is noticed within a
   minute despite the 930-second attempt timeout. Planning and verification Activities run
   on their own task queue (`<queue>-model`, named per run in
   `RunInput.model_task_queue`), so long model calls never take the slots that
   tool execution and projection writes need. A gateway that ignores
   `stream: true` and returns one JSON body is also accepted. The response cap
   (`ROCKY_PLAN_RESPONSE_TOKENS`, default 16384, doubled for high reasoning effort)
   includes reasoning tokens, and hitting it returns actionable feedback.
   Anthropic and OpenAI-compatible requests ask for provider-side structured
   output (`output_config.format`, or a strict `json_schema` response format)
   with a closed copy of the schema. Size and range limits are removed from that
   copy but stay in the schema text in the instructions and in local validation.
   If an endpoint rejects the format with HTTP 400/422 and the same request then
   succeeds without it, that endpoint and model use the prompt-only JSON mode for
   the rest of the process.
   Prompt caches match request prefixes, so the envelope puts what stays fixed
   for the whole task first: instructions and schema, then `run` and the
   `execution` policy, then per-step state, memory, feedback and
   `budget_remaining`. Anthropic requests end the system prompt and the stable
   envelope part at `cache_control` breakpoints; official OpenAI requests send a
   stable `prompt_cache_key`; provider clients are reused across steps. Cache-read
   input tokens are counted in `input_tokens` and also reported separately as
   `cached_input_tokens` on task state and in evaluation results.
4. Tools run as separate Activities. A per-goal file lock and a request-fingerprinted,
   atomic receipt make single-host retries idempotent. A file write completed
   before its receipt can be safely replayed with the same content. HTTP requests,
   shell commands, `append_file` and `edit_file` write a durable dispatch marker first. If interrupted before
   their final receipt, retries report an unknown outcome instead of repeating an
   external side effect; the planner must inspect state before a new operation.
5. Every snapshot has a monotonic revision. The projection uses an atomic
   conditional SQL update, so a delayed retry cannot replace a newer state.
6. Cancel/wake commands have a durable outbox. Cancellation is idempotent; wake
   includes a command ID and the Workflow ignores duplicate delivery.

Temporal history is the source of execution progress. PostgreSQL (or local SQLite)
holds API projections and submission/command outboxes. Files and tool receipts
live on a separate persistent workspace volume. Back up all three together;
deleting any one cannot be repaired by merely restarting the worker.

Projection writes (`rocky.persist`) are revision-fenced and therefore retried
without limit: a full disk, a locked database or a sleeping Mac delays a task
but never fails its Workflow. Only the engine itself ends a goal as `failed`
(reconciliation of a failed, terminated or timed-out execution); the task's own
limits end it as `budget_exhausted` or `deadline_exceeded`. `POST
/goals/{id}/recover` queues a `failed` goal again: the dispatcher starts a new
execution of the same workflow ID (`ALLOW_DUPLICATE`, only for a recovery
request) with a checkpoint built from the projection. Usage counters, step
numbers (so operation IDs never reuse old receipts), task progress, handoff
summary and workspace carry over; an undispatched approval request moves to
the declined list; the wall-clock limit restarts. The first planning round is
told that an interrupted operation may have taken effect.

`schema_version` records the SQL schema revision. `create_all` only adds
tables; a change to an existing table increments `store.SCHEMA_VERSION` and
registers `store.MIGRATIONS[n]`, which runs in one transaction with the version
update. A database from before versioning is version 1. Older code refuses a
database with a newer version.

## Single model-driven execution loop

The adaptive loop is the only execution loop. The model chooses an action, wait,
finish or blocked decision at a time; an action may batch independent read-only
operations. The harness enforces limits, persists each checkpoint and feeds
actionable failures back to the next planning call. It does not impose a six-stage plan.
Default rounds record `think` followed by the actual `act` or `complete` work;
`perceive`, `decide`, `reflect` and `complete` remain optional extension points.
No empty hook Activities are dispatched. Failed decisions are recorded and
returned to the model rather than progressing through ceremonial phases.

`HarnessHooks` handlers run in registration order in Activities. Synchronous
handlers run off the event loop; asynchronous handlers are awaited. Only
explicitly enabled checkpoints dispatch handlers: pass
`RunInput(..., hook_phases=hooks.phases)` or an explicit phase list when starting
the workflow. Registration alone does not enable dispatch. Only decision hooks
may replace decisions. Feedback and handoff summaries can be extended by hooks.
External effects must use the stable step ID for idempotency; hooks are not
retried automatically. Cancellation interrupts further dispatch.

The SQL store keeps task projections separate from execution records. Each
started/completed/failed transition appends an immutable execution event with
input, output, timestamps and elapsed milliseconds. The current step projection,
messages and task state update in the same revision-fenced transaction; retrying
a revision adds no duplicate events. `/steps` reads the mutable projection;
`/execution-events` reads the append-only audit trail.
`/goals` returns an `ETag` derived from the listed goal IDs and revisions;
polls that send it back with `If-None-Match` receive a bodiless 304 until a
goal is created, advances, or is archived, restored or deleted. A file-backed
SQLite store runs in WAL mode so API reads do not wait on worker commits. Deletion purges both with
the existing task/outbox records and removes the goal's workspace, receipts and
cookie sessions; tombstones prevent resurrection.

The contract and explicit `task_progress` (completed, pending, evidence) remain
resident. The planner can update an accumulated `handoff_summary`; it retains
older conclusions and failed approaches instead of replacing them with only
the last few observations. `MemoryManager` reserves separate shares for summary,
recent raw history and retrieved knowledge: 40/40/20 when all are present, within
a 24000-token serialized envelope. Empty or unused shares flow to later layers.
The newest tool result may use most of the recent-history share, so a typical
file read or command output reaches the next decision whole; older results are
shortened first and still keep excerpts. Tool bodies follow their metadata as raw
text rather than nested JSON strings. The planner lowers the envelope to the room
the essential layers leave inside the whole-request bound. The default
counter uses UTF-8 bytes as a conservative token upper bound; provider tokenizers
can be injected. The model boundary independently checks the entire envelope.

Task contract, resident state and feedback have separate conservative serialized
budgets of 60000, 32000 and 4000 tokens, with a 100000-token whole-request bound.
Optional feedback/artifact excerpts are visibly clipped; oversized essential
contracts fail before model dispatch rather than silently losing acceptance
criteria. Checkpoint payloads and raw observations are not duplicated outside
their assigned context layer. Independent review receives actual bounded
artifact text rather than the planner's success claim.

SQL recall is bounded at query time. It retrieves recent conversation, retained
owner clarifications, task knowledge and query-relevant curated owner memory.
Long-term records contain preferences, decisions, lessons or corrections and
source metadata; traces and model summaries are never automatically promoted.
The owner explicitly writes them through `/memories/{key}`. Revisions use
compare-and-swap updates (stale writes return 409); corrections replace the same
key, retractions mark it inactive, and inactive records are excluded from recall.
A memory query splits text into ASCII words and Chinese character bigrams (up to
64 terms), selects at most 500 active records containing any term, and ranks
them by owner priority, then BM25 relevance with keywords and keys weighted above
content, then recency. Ranking runs in Python, so SQLite and PostgreSQL behave
identically without a tokenizer extension; common terms count for little, so a
long objective does not crowd out rarer, more specific terms.
Memory remains untrusted evidence and cannot grant authority or establish success.

The durable handoff approach is informed by Anthropic's
[long-running agent harness](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)
and extension hooks by LangChain's
[custom harness middleware](https://www.langchain.com/blog/how-to-build-a-custom-agent-harness).
The current release does not support replaying unfinished Temporal executions from
the former legacy or six-phase loops of 0.1, or earlier versions of the adaptive loop.
End old running, waiting or blocked executions before upgrading and submit new
goals to continue their objectives. Stored task records remain readable. No
automatic history migration is provided; records and workspace files are retained.
Replays and worker restarts within this execution version use the same single loop.

Continuation checkpoints are captured only between complete rounds. Active state
keeps six observations of at most 2000 characters each while SQL retains complete tool results. A full
round boundary continues as a new Temporal run after 8 rounds, 8 MB or 1500 events,
or when Temporal recommends it. Task ID, cumulative counters/revisions, recovery
state, pending wake generations and command deduplication survive in a bounded
checkpoint. The checkpoint is restored before signal handlers run and is removed
from subsequent Activity inputs. This keeps historical payloads from multiplying
across the chain. No in-flight operation is rotated or assigned a new operation ID.

A separate 30-second reconciliation loop compares dispatched nonterminal SQL
tasks against their engine run. Closed failures, termination, cancellation and
timeouts update the projection under an exact revision check, and a recheck of
the run ID protects a recovered execution. Reads of engine failure diagnostics
use close events only. The history-size termination message is recognized and
sanitized; arbitrary failure text is never copied into task status. Reconciliation
does not restart closed tasks. There is no manual mid-phase checkpoint recovery
path; normal worker restart replays this version's durable execution history.

## Lifecycles

`queued` → `running` ↔ `waiting` / `blocked` → terminal result.

- `waiting` is a model-requested bounded timer, interrupted by owner input/cancel.
- `blocked` waits for owner input or exact-operation approval. The task deadline
  bounds Rocky's own work, so it moves forward by the time spent waiting for the
  owner; an unanswered approval is still declined after its own timeout, and a
  blocked question waits until the owner answers or cancels. Histories recorded
  before `owner-wait-pauses-deadline-v1` replay with the clock running. No models
  run while waiting. A real wake resets the
  no-progress counter and preserves usage/history; wake does not grant approval.
  A wake during an approval wait withdraws the request (`approval_superseded`)
  so the next round follows the owner's message.
- Terminal results: `succeeded`, `failed`, `cancelled`, `budget_exhausted` and
  `deadline_exceeded`. Terminal goals reject wake and cancel commands. Three
  explicit paths continue the same goal, workspace, progress and usage counters
  under a new execution: `POST /goals/{id}/recover` for `failed`;
  `POST /goals/{id}/extend` raises the limits of a `budget_exhausted` or
  `deadline_exceeded` goal; `POST /goals/{id}/follow-up` folds an owner request
  into the objective and criteria of a `succeeded`, `cancelled`,
  `budget_exhausted` or `deadline_exceeded` goal and grants one more original
  budget on top of usage so far. Each restarts the wall-clock limit.
- All planning rounds count as steps, including wait/finish requests. Verification
  records an additional model call, including when deterministic file checks fail.
- No-progress detection compares action parameters and result fingerprints, so
  distinct sources with identical content do not falsely count as repeats.
  At the first stalled threshold reflection requests a changed strategy and
  preserves a handoff summary. Only another stalled window without new evidence
  enters blocked sleep. Fresh results and real wake input restore the recovery
  allowance; a timer alone does not. Planning memory is a bounded window, so a
  repeated successful `read_file`, `list_files` or `fetch_url` whose earlier
  result has rolled out is a neutral refresh: it is neither a stall nor progress.
  Each observation gets one refresh, never immediately after the planner saw it;
  the allowance is checkpointed across continued runs, and histories recorded
  before this rule replay with every repeat counted as a stall. Fingerprints
  detect repetition rather than semantic correctness, which remains the
  independent verifier's responsibility.

## Completion and trust

A finish decision invokes independent verification. Deterministic checks read
declared files, check minimum size/required text and hash their bytes. A separate
structured model review checks actual artifact excerpts against the objective
and criteria, together with evidence the application collects from the
execution record and the files on disk rather than from planner claims:
source files the task read or found unchanged in its workspace (size, line
count and whether `read_file` returned them completely), other files the task
created or modified (diffed against their first read content), which recorded
actions wrote each deliverable, and the chronological tool actions with output
excerpts. Every tool result lists the files it changed; shell commands report
changes under their working directory from before/after snapshots that skip
VCS, dependency and cache trees and stop at 5000 files. The review must trace
figures to sources or observed outputs and reject partial reads presented as
complete or computed results overwritten by unsupported content. Evidence is
fitted into the request bound: action outputs shrink first, then file text gets
max-min fair shares while every file keeps its metadata.

The review is structured, and the application, not the reviewer, decides
whether figures are supported. For JSON deliverables it extracts every number
and every short name, code, date or path (up to 60; plain lowercase labels such
as statuses are left to the reviewer's judgment) and first verifies what it
can itself: a value is computed when the deliverable was last written by a
command that does not state it literally; a distinctive value (not a one- or
two-digit number) is stated when a single line of a source, a changed file or
an action output contains it, or every name, code and number of a paraphrase;
a cited path holds when that file states the value paired with it (a record
sibling, or the value under the same key in a parallel object), however small. A field of a
record in an array counts only on a line that also names an identifier of that
record. The reviewer sees these checked values and returns claims only for the
rest, plus the figures, facts and properties of other deliverables: each claim
has a kind (quoted, computed or judgment), a location and a verbatim quote.
Locations are matched loosely (`source:`, `file:`, `artifact:`, bare or
directory-relative paths, several locations). Each quoted line must appear in
order at the location, so skipped lines are tolerated, as are ellipses, JSON
escapes, Markdown emphasis and dash variants; a record field and its record
identifier must still share one quoted line. Quoting the very file a citation
names supports that citation. The audit then rejects a pass
when a claim is unsupported, cites a missing location or a quote that is not
there, judges a number, judges a name that the evidence states, cites a
deliverable for its own reported values, attributes a computed value to
something other than a command whose output or written deliverable shows it,
omits a value nobody verified, or disputes a value the application verified.
Rejections list each value with the reason, so the planner can compute or
correct it. The audit checks provenance, not arithmetic: a real but buggy
computation still passes, and deliverables that are not JSON still depend on
the reviewer's judgment for their content.
Review still cannot prove factual truth or browse sources independently;
research deliverables require appropriate evidence and human review.

External text never grants permissions. The owner configures `ROCKY_TOOL_ACCESS`,
which defaults to `full`. In full mode all nine tools are available even when an
existing goal records narrower legacy allowlists. File tools accept absolute,
relative, hidden and symlink paths. Shell commands run as the current OS user.
HTTP requests accept all methods and arbitrary HTTP(S) destinations, including
private/local services and custom ports, and follow redirects. A redirect to
another origin is never followed for a method other than GET/HEAD/OPTIONS, since
a 307/308 would resend an approved body to an origin nobody reviewed. Named per-goal
cookie jars persist privately outside the task's artifact directory. Full tools
can reach all locations the OS user can access, subject to the file gate below;
this is an execution policy, not an OS sandbox. Output excerpts remain bounded to fit model context.

`ROCKY_APPROVAL_MODE=required` is the default. Every shell command and HTTP
method except GET/HEAD/OPTIONS returns a concrete approval requirement before
side-effect dispatch. The workflow persists the full action and waits without
model calls. Owner approval binds the goal, operation ID and exact action
fingerprint; it resumes that same operation. Changed parameters require a new
approval. An approval outbox and command deduplication survive delivery retries.
The macOS client displays the operation with approve/cancel controls; the API
uses `POST /goals/{id}/approve` with the current fingerprint. A pending shell
command also carries a `command_prefix` (for example `uv run pytest`); approving
with `"scope": "task"` additionally lets later commands in the same task that
start with that prefix run without pausing. `ROCKY_AUTO_APPROVE_COMMANDS`
(comma-separated or JSON list, e.g. `ls,git status,uv run pytest`) is an
owner allowlist of such prefixes for every task. Prefixes only match *simple*
commands: anything containing `;`, `&`, `|`, `<`, `>`, `(`, `)`, `$`, backticks
or a newline always needs exact approval, and wrappers such as `sudo`, `env` or
`xargs` and inline interpreter code (`python -c`) are never offered as a class.
A prefix approves what follows it: `python3 build.py` approves any arguments to
that script, including after the script changes. Only the owner's
explicit `ROCKY_APPROVAL_MODE=trusted` configuration bypasses the gate. The gate
classifies tool/method, so it cannot recognize a mutating GET endpoint. A
GET/HEAD/OPTIONS request with a body or a URL longer than 2048 characters is
gated like a write so file contents cannot ride along; a short GET can still
carry a little data, which is why personal-file reads are gated.

`run_command` waits 30 seconds by default; an action may request
`timeout_seconds` up to 900, and processes left behind when it returns are
killed. `background: true` starts a managed job instead: the call returns a job
ID, output goes to `.rocky-jobs/<id>.log` and the exit code to
`.rocky-jobs/<id>.exit`. A goal runs at most 4 jobs of at most 6 hours each,
and all are stopped when the goal ends or is deleted. Both fields are part of
the approved action.

Full-access file tools do not run inside Seatbelt, so they have their own gate.
A goal contract may grant existing folders (`folders`, read/write) and attach
reference files (`files`, read-only); neither may be the root, the home
directory itself, or overlap Rocky's own data, code or credential stores.
Seatbelt also lets sandboxed commands write granted folders.
A write, append, edit or HTTP download whose resolved path (after symlinks) is
outside the task directory and granted folders, a read or listing of personal
files under the home directory outside those places, and a read or listing of a credential store
(`tools.SENSITIVE_HOME_PATHS`: `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`,
`~/.config/gh`, keychains, browser profiles, ...), needs the owner's exact
approval, like an unsandboxed command: owner rules and task grants never match
it, and it offers no reusable scope. Seatbelt denies reads of the same
credential stores, so commands that need them must request `sandbox=false`.
`rocky serve` state (API token, Temporal database, `run/` and log directories)
is protected like the credential file and SQLite database: no file tool or
sandboxed command can read or write it. `trusted` mode bypasses the file gate
but not these protections.

Before pausing, a gated action passes through approval tiers
(`rocky/approval_policy.py`), evaluated by the execute Activity against the
owner's current policy in the database:

1. **Read-only commands** (`command_policy.read_only_command`): sandboxed simple
   commands from a fixed catalog (`ls`, `cat`, `grep`, `rg`, `find`, `stat`,
   `jq`, ...) with per-program denied options (`find -exec/-delete`, `sort -o`,
   `tail -f`, `rg --pre`, GNU long-option abbreviations included), no unquoted
   glob or leading `~`/`=`, and a program that resolves outside the workspace
   root, with no argument or working directory pointing at personal files under
   the home directory outside the task and granted folders. `git` is excluded
   because repository config can run programs.
   `ROCKY_AUTO_APPROVE_READ_ONLY` sets the default.
2. **Owner rules** (`approval_rules` table): command prefixes, or non-GET
   methods at an exact origin under a path prefix (dot segments and encoded
   slashes never match). Rules are written only through the authenticated API
   (`/approval-rules`, or `"scope": "always"` on an approval), never by tools.
3. **Task grants**: command prefixes and HTTP method + origin approved
   with `"scope": "task"`.

`ToolResult.approved_by` records which tier let an action run. Unsandboxed
commands and gated file access skip every tier. An unanswered request is declined, never approved,
after `approval_timeout_seconds` (from `ROCKY_APPROVAL_TIMEOUT_MINUTES` or the
stored policy, reported on the gated result so the workflow stays
deterministic). The workflow returns an `approval_timeout` result to the
planner and keeps the request in `deferred_approvals`. `"scope": "deferred"`
later records the action's identity (goal + action content, without the
operation ID) in `preapproved_actions` and wakes the task; the next identical
action runs once. Three consecutive timeouts put the task in `blocked`.
`"scope": "deny"` declines the pending action. `GET /approvals` and
`POST /approvals/batch` list and answer requests across tasks.

A decision may carry `actions`: up to 8 independent read-only actions
(`read_file`, `list_files`, `fetch_url`, GET/HEAD/OPTIONS `http_request` without
a download path). They run as concurrent Activities with operation IDs
`step-NNNN-k`, share the goal lock in shared mode, and each produces its own
observation and tool message. Writes, commands and mutations stay single
actions. `append_file` adds text to a file and `edit_file` applies exact
`old_text` → `new_text` replacements atomically, so long reports and small fixes
no longer require rewriting a whole file in one response.

Tool arguments are validated before dispatch, including unused fields and HTTP
option types. Failures return safe `error_code` and `retry_hint` fields without
credentials. Unknown interrupted mutations require actual-state inspection,
never an automatic repeat. Full access is not a security sandbox.

Optional `scoped` mode retains the original tool/hostname allowlists, pinned public
GET retrieval and descriptor-relative workspace file operations. Receipt storage
and exported deliverables retain structural validation in either mode. Credentials
are never copied into error messages or HTTP response-header summaries.

## Long-running agenda

Plans and schedules (`rocky.agenda`) turn larger or recurring work into ordinary
goals; they add no new execution path. Both are rows in the goal database with
a revision column, and every change is a compare-and-set update, so owner API
calls and the worker's agenda loop (`rocky.agenda_runner`, every 10 seconds)
cannot overwrite each other.

A plan holds an objective and up to 20 sequential steps. `POST /plans/draft`
asks the model for a breakdown (`PlanDecomposition`, 1–12 steps plus concerns)
without starting anything; the owner edits it and submits `POST /plans`. When a
step's turn comes the loop drafts its contract with the same
`prepare_contract` call the client uses, from the step objective plus the plan
objective and earlier step summaries (bounded to the 8000-character objective
limit). Deliverables of finished steps are copied into the new goal's workspace
under `inputs/step-N/` before it starts. The goal is inserted in the same
transaction that marks the step running, and its ID is derived from plan, step
and attempt, so a crash cannot start a step twice.

The loop follows each running step's goal. Success records the goal's handoff
summary and delivered files; when replanning is on, the model then reviews the
plan (`PlanRevision`): it may keep, replace or extend the remaining steps, or
declare the objective achieved, at most 10 times per plan. Any other terminal
result puts the plan in `attention`: the owner retries the step (a new attempt
and goal), skips it, or continues the step's goal in place (extend, follow-up,
recover), after which the plan follows that goal again. Failed model calls back
off 2, then 4 minutes; a third failure asks the owner. Plan steps share
`ROCKY_AGENDA_CONCURRENCY` slots (default 1), granted by priority then age.
Pausing stops new steps; cancelling also cancels the running step's goal.
Drafting and review tokens are reported per plan, not charged to any goal.

A schedule stores a fixed contract and a recurrence: once, every N minutes
(at least 5), daily or weekly at a wall-clock time in an IANA time zone (the
Mac's by default; a time skipped by a DST change runs an hour later). Each run
inserts a goal whose objective names the schedule and run time, with an ID
derived from the schedule and the planned time, in the transaction that
advances `next_run_at`. Runs missed while the Mac slept collapse into one; a run
whose previous run has not ended is skipped and counted. `POST
/schedules/{id}/run` starts an extra run without changing the rhythm. Deleting
a plan or schedule keeps the goals it started.

## macOS client and local API boundary

The SwiftUI macOS application is the only client. It bundles the Python backend
with its locked dependencies, uv and Temporal, and registers the launchd user
agent `dev.rocky.agent` through `SMAppService`. The agent prepares a private
runtime in Application Support offline and runs `rocky serve`, which supervises
Temporal, the API and the worker; launchd restarts it after a crash, and it
keeps running when the window closes. The app connects to the loopback API with
the persistent token in `data/api-token` (mode 0600; `rocky rotate-token`
replaces it). The API serves JSON and task
artifacts; it has no HTML entry point, static frontend or interactive API docs.
`/openapi.json` remains available for development tooling.

Standalone backend development defaults to `ROCKY_AUTH_MODE=local`. This requires
a loopback bind, checks the actual socket peer, validates one trusted Host and a
same-origin Origin when present, and rejects cross-site Fetch Metadata. The
boundary applies to every route. Local requests need no token. The API CLI
disables proxy-header rewriting; local CLI requests ignore environment proxies.
This mode grants access to any local process. Reverse proxies or tunnels exposing
the service externally must use token mode: a real loopback peer cannot prove
that an upstream proxy is local-only.

Explicit `ROCKY_AUTH_MODE=token` applies Bearer checks to goal creation,
list/detail/events, wake/cancel, artifact access and model/provider settings.
Without a configured owner token these endpoints fail closed. The macOS app uses
this mode and stores model credentials in the system Keychain; settings responses
expose credential references rather than plaintext keys.

JSON responses are marked `no-store`, and responses set `nosniff` and
`no-referrer` headers. `/health` reports only API process liveness. The macOS
runtime waits for both API health and worker readiness before accepting work.

## Execution limits and cancellation

`contract.limits` defaults to 100 planning steps, 150 model calls, 500000 total
input/output tokens and a 3600-second task timeout. Each field accepts null to
explicitly disable that limit. Legacy flat max_steps/max_model_calls/
max_output_tokens/deadline_seconds fields remain decode-only metadata so stored
goal records remain readable; the single execution loop reads limits only.
Checkpoints preserve cumulative counters and the original deadline across
continue-as-new and worker restart. Input and
approval waits extend that deadline by their duration instead of consuming it.

Observed token usage is checked before further calls/actions. An in-flight
request may exceed the token threshold, and missing provider usage cannot be
reconstructed: attempted model calls still count. Per-request timeouts, response
sizes and context limits remain separate technical controls. Reaching a bound
is an explicit terminal result rather than a retry loop.

Ended tasks may be archived and restored through separate recent/archived lists.
Deleting removes the API projection, events, commands and model snapshot, and
the goal's workspace, receipts and cookie sessions. A minimal identity tombstone prevents a late
activity or an old idempotency key from resurrecting a deleted task.

An in-flight model request may incur cost after
cancel. A file write already in progress may complete; cancellation prevents
subsequent actions and never claims to reverse existing effects.

## Current scope and next work

Current tools: filesystem reads/writes/listing, HTTP retrieval, general HTTP
requests with authenticated sessions, a headless browser (`browse`, Playwright
driving the installed Chrome with a private per-task profile; reading is
GET-tier and blocks page-initiated mutations, interaction steps need exact
approval) and shell execution. Curated cross-goal memory is supported. Rocky
runs on one Mac by design; there is no multi-host execution or remote access.
`rocky serve` makes daily consistent backups (SQLite online backup plus the
workspaces), trims logs by size, and the API reports readiness on `/status`
and Prometheus metrics on `/metrics`. Running
Workflow code changes need Temporal versioning/replay validation; preserve the
locked dependencies for active executions.

The Compose Temporal dev server persists SQLite history and is suitable for local
evaluation. Production requires an authenticated Temporal deployment, TLS/secrets
configuration, migrations, monitoring, quotas, backup/restore drills and storage
coordination beyond this single-host prototype. Ports bind only to loopback.
