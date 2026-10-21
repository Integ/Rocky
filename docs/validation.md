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
- Frontend suite: **26 passed**; syntax, Ruff and diff whitespace checks passed.
- Added coverage for authenticated settings, invalid model/effort combinations,
  SQL persistence/reopening, concurrent first writes, idempotent submission after
  defaults change, legacy databases and legacy tool-receipt fingerprints.
- Browser acceptance: 387px and 1465px layouts; demo writes disabled; select,
  save and reopen model settings; submit a real goal with GPT-6 Luna / `low`.
- Live goal: **succeeded**, 3 model calls, 2,633 input tokens, 194 output tokens.
  The actual file contained `Rocky is ready.` with an allowed trailing newline.
  The original GPT-4.1 mini default was restored after verification; the completed
  goal retained its recorded model and effort.
- The existing local API and worker were restarted with the new code. Temporal,
  the existing completed goal, databases and artifacts were retained.

## Token-free local console

- Full Python suite: **268 passed**, including real Temporal integration tests.
  Frontend suite: **32 passed**; JavaScript syntax, Ruff and diff checks passed.
- Local-access tests cover anonymous goal/settings operations, raw peer checks,
  strict Host validation, duplicate headers, DNS rebinding authorities, origin
  scheme/host/port matching, IPv6, spoofed proxy headers, Fetch Metadata, and the
  narrow static-entry navigation exception. Token-mode authentication remains
  covered explicitly, including failure without a configured credential.
- The native API was restarted in `local` mode without a provider key or owner
  token in its environment. Anonymous console-info/goals calls returned 200;
  cross-origin and cross-site API calls returned 403. Local OpenAPI has no Bearer
  requirement. Both existing completed goals remained available.
- Browser acceptance verified initial automatic connection, reload, explicit
  disconnect/reconnect, and saving the unchanged default model without entering
  a token. The console showed its local connection and real task data.
- The real native CLI listed goals with `ROCKY_API_TOKEN` removed from its
  environment. Docker Compose configuration validation passed in explicit token
  mode. No additional model calls were used for this change.
