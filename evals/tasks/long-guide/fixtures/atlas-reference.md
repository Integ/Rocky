# Atlas reference for this fictional evaluation

This document is authoritative task input for the fictional internal Atlas CLI.
Use it together with outline.md. There is no real Atlas service or package to
discover, install, or contact for this writing task. Explain the examples; do
not run installation, authentication, deployment, or account-management commands.

Configuration lives in `~/.atlas/config.toml`. A complete file has the following
seven keys grouped into four TOML tables. These are all the required settings;
no additional settings are needed for a complete example.

| Table | Key | Type and allowed values | Default/example |
| --- | --- | --- | --- |
| `api` | `endpoint` | HTTPS URL with an explicit port of 8443 | `https://atlas.internal.example:8443` |
| `api` | `timeout_seconds` | Positive integer | `30` |
| `defaults` | `env` | `staging` or `production` | `staging` |
| `defaults` | `output` | `table` or `json` | `table` |
| `deploy` | `wait_for_health` | Boolean | `true` |
| `deploy` | `health_timeout_seconds` | Positive integer | `300` |
| `log` | `level` | `debug`, `info`, `warn`, or `error` | `info` |

The hostname above and example service, ticket, release, key and alert identifiers
are placeholders. Replace them with your team's values. The key names, table
names, types and port above are the actual specification for this fictional task.
`atlas login` creates the file if absent. Environment variables and command-line
flags can override file settings; `atlas config show` shows effective values and
their sources. Keep the file private with mode 0600.

Supported commands and flags for worked examples:

- `atlas --version`; `atlas doctor [--verbose]` checks API reachability,
  configuration readability and helper programs on PATH.
- `atlas config show` and `atlas whoami` show configuration and identity/token
  lifetime respectively. `atlas login` opens SSO; tokens last 12 hours.
- `atlas keys create --name <name>`, `atlas keys list`, and
  `atlas keys revoke <key-id>` manage CI keys, rotated every 90 days.
- `atlas deploy <service> --env staging|production [--ticket <ticket-id>]
  [--dry-run] [--output table|json]` builds the current commit, publishes it,
  rolls it out, and optionally waits for health checks. Production requires an
  approved change ticket; staging does not. `atlas tickets show <ticket-id>`
  shows approval. Deploy one service at a time in dependency order; Atlas does
  not coordinate several services, and each production service needs a ticket.
- `atlas status <service> --env staging|production` shows rollout health.
- `atlas releases <service> --env staging|production [--limit <count>]` lists
  release identifiers and timestamps.
- `atlas rollback <service> --to <release> [--env staging|production]
  [--note <text>]` keeps the database schema and never runs down migrations.
  A production rollback to an approved release needs no new ticket, but must be
  noted on the original ticket. Incompatible older code requires a fix forward
  or database-owner review rather than an invented automatic schema downgrade.
- `atlas open dashboard <service> --env staging|production` opens request-rate,
  latency (p50/p95/p99), and error-percentage panels with deployment markers.
  Error rate above 2% for 5 minutes pages the on-call engineer.
- `atlas alerts list --env staging|production` and `atlas alerts silence
  <alert-id> --for <duration> --reason <text>` list and temporarily silence alerts.
- `atlas logs <service> --env staging|production --since <duration>` shows logs.

The fictional team's operating policy is to announce production changes, deploy
during business hours except incidents, and watch the dashboard for ten minutes
afterward. An onboarding buddy and service owner review the first production
change and rollback plan. Use the corporate network/VPN for API connectivity;
contact the platform support channel with doctor output and the failing command
if documented troubleshooting does not resolve the issue.
