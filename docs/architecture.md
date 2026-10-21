# Execution architecture

Rocky v0.1 targets one owner and one host. The model proposes one step at a time;
ordinary code owns state, execution scope, budgets and completion gates.

## Durable boundaries

1. API commits a goal and submission outbox record before returning HTTP 202.
2. The worker dispatcher starts a Temporal Workflow with the goal ID and
   `REJECT_DUPLICATE`. If start succeeds but database acknowledgement fails,
   retry finds the same execution instead of starting a second goal.
3. Model and verifier calls reserve budget in the Workflow before dispatch.
   SDK retries and model Activity retries are disabled. A failed/unknown-result
   attempt conservatively consumes its reservation.
4. Tools run as separate Activities. A per-goal file lock and a request-fingerprinted,
   atomic receipt make single-host retries idempotent. A file write completed
   before its receipt can be safely replayed with the same content. This does not
   establish exactly-once semantics for arbitrary future payment or messaging tools.
5. Every snapshot has a monotonic revision. The projection uses an atomic
   conditional SQL update, so a delayed retry cannot replace a newer state.
6. Cancel/wake commands have a durable outbox. Cancellation is idempotent; wake
   includes a command ID and the Workflow ignores duplicate delivery.

Temporal history is the source of execution progress. PostgreSQL (or local SQLite)
holds API projections and submission/command outboxes. Files and tool receipts
live on a separate persistent workspace volume. Back up all three together;
deleting any one cannot be repaired by merely restarting the worker.

## Lifecycles

`queued` → `running` ↔ `waiting` / `blocked` → terminal result.

- `waiting` is a model-requested bounded timer, interrupted by owner input/cancel.
- `blocked` waits for explicit owner input, cancellation or the original deadline.
  It does not spend model budget while waiting. Wake resets the no-progress
  counter, and preserves elapsed time, steps and model budget.
- Terminal results: `succeeded`, `failed`, `cancelled`, `budget_exhausted`,
  `deadline_exceeded`. Terminal goals reject new commands; submit a new contract.
- All planning rounds count as steps, including wait/finish requests. Verification
  reserves an additional model call, including when deterministic file checks fail.
- No-progress detection compares output fingerprints; changed text is not proof
  of useful progress. Step and model-call caps remain the final bounds.

## Completion and trust

A finish decision invokes independent verification. Deterministic checks read
declared files, check minimum size/required text and hash their bytes. A separate
structured model review checks actual artifact excerpts against the objective
and criteria. It cannot prove factual truth or browse sources independently;
research deliverables still require appropriate evidence and human review.

External text never grants permissions. The tool gateway enforces exact tool and
hostname allowlists. Fetch resolves public addresses once and connects to a pinned
validated IP with TLS hostname validation. Redirects and compression are rejected.
Filesystem access uses descriptor-relative operations, no-follow opens, atomic
writes, and link checks. Model-visible files cannot reach control receipts.

## Mission Control boundary

`GET /` and `/console-assets/` serve a public, static console shell. They contain
no model key, owner token or server-side task data. Before connecting, the console
labels its seeded local examples as DEMO. A connection uses the owner's
`ROCKY_API_TOKEN` against the same-origin API; the browser retains it only in page
memory. It must never request the worker's `OPENAI_API_KEY`.

Goal creation, list/detail/events, wake and cancel keep the existing Bearer
authentication boundary. Authenticated `/console-info` reports the configured
model, application version, supported tool names and a three-second polling
interval; it is not a worker-health signal. API and worker model configuration
must agree when deployed separately.

Console responses use a self-only Content Security Policy, with data images
allowed and embedded objects/framing prohibited. No external scripts, fonts or
styles are required. Entry HTML and JSON responses are marked `no-store`;
responses also set `nosniff` and `no-referrer` headers. Swagger documentation
retains its separate asset behavior. Animation respects reduced-motion settings.

## Budgets and cancellation

Budgets cap planning rounds, attempted model calls, per-call generated tokens,
context size, and wall-clock duration. They are not an exact currency spend cap;
input tokens also incur costs. An in-flight model request may incur cost after
cancel. A file write already in progress may complete; cancellation prevents
subsequent actions and never claims to reverse existing effects.

## Current scope and next work

Current tools: scoped UTF-8 files, bounded directory listing and allowlisted
public text retrieval. There is no arbitrary shell, browser control, general web
search, long-term cross-goal memory, self-modifying runtime, or multi-host execution.
Add these as separate capabilities with evidence-based evaluators. Running
Workflow code changes need Temporal versioning/replay validation; preserve the
locked dependencies for active executions.

The Compose Temporal dev server persists SQLite history and is suitable for local
evaluation. Production requires an authenticated Temporal deployment, TLS/secrets
configuration, migrations, monitoring, quotas, backup/restore drills and storage
coordination beyond this single-host prototype. Ports bind only to loopback.
