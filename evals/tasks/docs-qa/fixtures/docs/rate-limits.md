# Rate limits

Northwind Cloud applies request limits per API key. Limits are measured over a
rolling 60-second window and apply to every endpoint except `/health`.

| Plan       | Requests per minute | Burst allowance |
|------------|--------------------:|----------------:|
| Free       | 60                  | 10              |
| Team       | 600                 | 100             |
| Enterprise | 3000                | 500             |

Burst allowance lets a client briefly exceed the steady rate, for example when a
job starts. Unused burst capacity refills at one request per second.

## When a limit is exceeded

The API rejects the request with HTTP status **429 Too Many Requests**. The
response includes a `Retry-After` header with the number of seconds to wait
before retrying, and a JSON body with `error.code` set to `rate_limited`.
Clients should back off exponentially and must not retry immediately.

## Raising limits

Enterprise customers can request a higher limit through their account manager.
Limits are never raised for Free or Team plans; upgrade the plan instead.
