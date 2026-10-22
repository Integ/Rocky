# Execution architecture

Rocky v0.1 targets one owner and one host. The model proposes one step at a time;
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
   Each request may take up to 300 seconds; the response cap
   (`ROCKY_PLAN_RESPONSE_TOKENS`, default 16384, doubled for high reasoning effort)
   includes reasoning tokens, and hitting it returns actionable feedback.
   The instructions and schema form an identical prefix on every step: Anthropic
   requests mark it with `cache_control`, official OpenAI requests send a stable
   `prompt_cache_key`, and provider clients are reused across steps.
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
`/execution-events` reads the append-only audit trail. Deletion purges both with
the existing task/outbox records, and tombstones prevent resurrection.

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
Memory remains untrusted evidence and cannot grant authority or establish success.

The durable handoff approach is informed by Anthropic's
[long-running agent harness](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)
and extension hooks by LangChain's
[custom harness middleware](https://www.langchain.com/blog/how-to-build-a-custom-agent-harness).
This v0.1 release does not support replaying unfinished Temporal executions from
the former legacy or six-phase loops, or earlier versions of the adaptive loop.
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
- `blocked` waits for owner input or exact-operation approval; configured task
  deadlines still apply. No models run while waiting. A real wake resets the
  no-progress counter and preserves usage/history; wake does not grant approval.
- Terminal results: `succeeded`, `failed`, `cancelled`, `budget_exhausted` and
  `deadline_exceeded`. Terminal goals reject new commands; submit a new contract.
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
which defaults to `full`. In full mode all eight tools are available even when an
existing goal records narrower legacy allowlists. File tools accept absolute,
relative, hidden and symlink paths. Shell commands run as the current OS user.
HTTP requests accept all methods and arbitrary HTTP(S) destinations, including
private/local services and custom ports, and follow redirects. Named per-goal
cookie jars persist privately outside the task's artifact directory. Full tools
can reach all locations the OS user can access; this is an execution policy, not
an OS sandbox. Output excerpts remain bounded to fit model context.

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
classifies tool/method, so it cannot recognize a mutating GET endpoint; ordinary
file writes retain the selected filesystem policy.

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

## macOS client and local API boundary

The SwiftUI macOS application is the only client. It bundles the Python backend,
uv and Temporal, prepares a private runtime in Application Support, and owns the
API, worker and Temporal processes it starts. It connects to a loopback API using
a fresh Bearer token generated for each launch. The API serves JSON and task
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
approval waits also observe that deadline.

Observed token usage is checked before further calls/actions. An in-flight
request may exceed the token threshold, and missing provider usage cannot be
reconstructed: attempted model calls still count. Per-request timeouts, response
sizes and context limits remain separate technical controls. Reaching a bound
is an explicit terminal result rather than a retry loop.

Ended tasks may be archived and restored through separate recent/archived lists.
Deleting removes the API projection, events, commands and model snapshot while
preserving user workspace files. A minimal identity tombstone prevents a late
activity or an old idempotency key from resurrecting a deleted task.

An in-flight model request may incur cost after
cancel. A file write already in progress may complete; cancellation prevents
subsequent actions and never claims to reverse existing effects.

## Current scope and next work

Current tools: filesystem reads/writes/listing, HTTP retrieval, general HTTP
requests with authenticated sessions and shell execution. Browser automation can
be run through shell-installed tools. Curated cross-goal memory is supported;
there is no built-in browser-control tool or multi-host execution. Running
Workflow code changes need Temporal versioning/replay validation; preserve the
locked dependencies for active executions.

The Compose Temporal dev server persists SQLite history and is suitable for local
evaluation. Production requires an authenticated Temporal deployment, TLS/secrets
configuration, migrations, monitoring, quotas, backup/restore drills and storage
coordination beyond this single-host prototype. Ports bind only to loopback.
