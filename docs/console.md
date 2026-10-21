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
- Demo: clearly labeled, browser-local sample missions with no API requests.

## Connection and truthfulness

Enter `ROCKY_API_TOKEN`, not `OPENAI_API_KEY`. The owner token remains only in
page memory. Disconnect or reload clears it. Credentials are never placed in
URLs, localStorage, sessionStorage, task exports or server-rendered HTML.
All requests use the same origin with redirects disabled and no cookie credentials.

Connected sessions poll the latest 100 goals every three seconds while visible.
The telemetry describes that loaded subset. Budget consumption is explicitly
separate from completion progress. The model label reports API configuration;
it does not prove which worker is running. There is no worker heartbeat endpoint,
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

## Validation

- 107 Python tests passed, including the existing real Temporal integration suite
  and console authentication, static serving and security-header checks.
- 20 Node tests passed for statuses, budget arithmetic, demo isolation and contract
  validation. JavaScript syntax checks and existing Ruff checks passed.
- The built wheel includes all five HTML/JS/CSS console assets.
- Browser acceptance covered a 387px narrow view and a 1465px fullscreen view,
  connecting to the real local API, submitting a test goal, viewing details,
  enqueueing wake/cancel commands, and refresh clearing the connection.
- No model calls were required for console development or its test suite.
