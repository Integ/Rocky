# Rocky Mission Control

The console is served by `rocky api` at `/`; API documentation remains at `/docs`.
It ships inside the Python wheel, uses no frontend dependencies, and requires no
Node runtime or build step in deployment. Node is only used for development tests.

## Interface

A near-black command deck with amber orbital geometry, a rotating wireframe core,
mission queue, budget telemetry, event signals, and slide-out mission details.
The immersive-mode button requests browser fullscreen; normal narrow layouts stack
panels vertically. Reduced-motion preferences stop the animated core.

- Create: objective, criteria, deliverables, scope, steps/calls budget and deadline.
- Inspect: actual state, observations, loaded checkpoints, artifact paths/hashes.
- Intervene: supplement information/wake, or explicitly confirm cancellation.
- Export: download the selected goal and loaded checkpoints as JSON.
- Model settings: choose a model and its supported reasoning effort, then save
  defaults for newly submitted goals. Existing goals retain their own snapshots.
- Demo: clearly labeled, browser-local sample missions with no API requests.

## Connection and truthfulness

Enter `ROCKY_API_TOKEN`, not `OPENAI_API_KEY`. The owner token remains only in
page memory. Disconnect or reload clears it. Credentials are never placed in
URLs, localStorage, sessionStorage, task exports or server-rendered HTML.
All requests use the same origin with redirects disabled and no cookie credentials.

Connected sessions poll the latest 100 goals every three seconds while visible.
The telemetry describes that loaded subset. Budget consumption is explicitly
separate from completion progress. The default model label reports persisted
submission defaults; each goal's details reports its own snapshot (or explicitly
marks that a legacy goal did not record one). It does not prove which worker is
running. There is no worker heartbeat endpoint,
so the console does not claim that API connectivity means the worker is online.

Link failures preserve a visibly stale snapshot and disable write controls until
the link recovers. Authentication failures clear the owner session and task data.
Goal submissions retain an idempotency key for the same form on uncertain retry.
An unconfirmed wake is held pending instead of suggesting a blind, duplicate wake.
Pending commands remain queued until the worker processes them; the UI does not
pretend a successful HTTP acknowledgement means execution already occurred.

Task content is inserted as text, never HTML. Console responses carry a same-origin
Content Security Policy, no-referrer and nosniff headers. Entry HTML and task JSON
are not browser cached. The public shell contains no credentials or private goals.

## Model and reasoning settings

Authenticated `GET /model-settings` reads defaults and `PUT /model-settings`
atomically replaces them. The payload is `{ "model": "gpt-6-luna",
"reasoning_effort": "low" }`; use `null` for the model's default effort.
`GET /console-info` exposes the model catalog and supported efforts.

The settings form filters efforts when the model changes. Non-reasoning models
disable the effort control; unsupported model/effort combinations are also
rejected by the API. Catalog entries describe capabilities, not account access.
The API process never needs the provider key to read or update these settings.

Defaults and goal snapshots use additional SQL tables, so existing databases do
not need destructive migrations. Idempotent submission compares the original goal
contract and returns the original snapshot even if defaults changed. Planning and
independent model review both use the snapshot carried in the Temporal input.
Legacy workflow inputs without this optional field continue to decode; their
model settings are not reconstructed from today's defaults. Tool retry receipts
exclude inference settings from their stable operation fingerprint.

On a single-host upgrade, stop the old worker before restarting it with the new
code, then restart the API. This prevents an old worker from receiving newly
submitted workflow inputs that it cannot decode.

## Validation

- 143 Python tests passed, including the existing real Temporal integration suite
  and console authentication, static serving and security-header checks.
- 26 Node tests passed for statuses, budget arithmetic, demo isolation, contract
  validation and model/effort selection. JavaScript syntax checks and Ruff passed.
- The built wheel includes all five HTML/JS/CSS console assets.
- Browser acceptance covered a 387px narrow view and a 1465px fullscreen view,
  connecting to the real local API, submitting a test goal, viewing details,
  enqueueing wake/cancel commands, and refresh clearing the connection.
- Model-settings acceptance covered demo mode blocking writes, non-reasoning
  models disabling the effort selector, selecting GPT-6 Luna with `low`, saving
  and reopening settings, and submitting a goal from the new-mission form.
- The live model-settings goal succeeded with 3 calls, 2,633 input tokens and
  194 output tokens. Its `readiness.txt` was verified. Restoring the previous
  default left the goal's GPT-6 Luna / `low` snapshot unchanged. Automated tests
  do not call a model or use provider credentials.
