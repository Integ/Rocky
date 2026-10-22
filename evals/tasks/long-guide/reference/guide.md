# Atlas onboarding guide

This guide is for engineers in their first week. It explains how to install,
configure and use Atlas, the internal command-line tool for deploying services,
and what to do when something goes wrong. Each section after the overview
includes commands you can copy and run.

## Overview

Atlas is the single supported way to build, release and roll back services
across staging and production. It wraps the build system, the deployment
controller and the change-ticket workflow behind one command-line interface, so
every deploy is recorded, reviewed where necessary and reversible.

Use Atlas if you own or contribute to a service that runs on the shared
platform. You do not need Atlas for local development: run services locally the
way the service README describes. You will need Atlas as soon as you want your
change to reach staging, and later production.

Atlas talks to a central API. Everything you do through the CLI is attributed
to your identity, which is why authentication and token hygiene matter. The
on-call engineer can see every deploy and rollback in the audit log.

## Installation

Atlas supports macOS 14 or later. Install it with Homebrew:

```bash
brew install atlas-cli
```

Confirm the installation and check the version:

```bash
atlas --version
atlas doctor
```

`atlas doctor` checks that your shell can reach the Atlas API, that your
configuration file is readable and that required helper tools are on your
`PATH`. Fix anything it reports before your first deploy. To upgrade later:

```bash
brew upgrade atlas-cli
```

If you previously installed a pre-release build from a tarball, remove it from
`/usr/local/bin` first so the Homebrew version is the one on your `PATH`:

```bash
which -a atlas
```

## Configuration

Atlas reads its settings from `~/.atlas/config.toml`. The file is created the
first time you run `atlas login`, but you can also write it yourself. The API
endpoint listens on port 8443; do not change the port unless the platform team
asks you to. A complete example file:

```toml
# ~/.atlas/config.toml
[api]
endpoint = "https://atlas.internal.example:8443"
timeout_seconds = 30

[defaults]
env = "staging"
output = "table"

[deploy]
wait_for_health = true
health_timeout_seconds = 300

[log]
level = "info"
```

Check which settings Atlas is actually using, including values that come from
environment variables:

```bash
atlas config show
```

You can override a single value for one command with a flag, for example
`atlas deploy billing --env staging --output json`. Keep the configuration file
private; it may contain your API key reference. Restrict its permissions:

```bash
chmod 600 ~/.atlas/config.toml
```

## Authentication

Sign in through single sign-on:

```bash
atlas login
```

This opens your browser for SSO and stores a short-lived access token. Access
tokens expire after 12 hours, so expect to run `atlas login` again at the start
of most working days. Check your current identity and token lifetime:

```bash
atlas whoami
```

Automation such as CI jobs uses API keys instead of interactive tokens. API
keys must be rotated every 90 days. Create a replacement before the old key
expires, update the CI secret, then revoke the old key:

```bash
atlas keys create --name ci-billing
atlas keys list
atlas keys revoke <key-id>
```

Never paste tokens or API keys into chat, tickets or shell history. If a key is
exposed, revoke it immediately and tell the security team.

## Deploying a service

Deploy a service to an environment with:

```bash
atlas deploy <service> --env staging
```

Staging deploys need no approval. Use them freely to test changes. Atlas builds
the current commit, pushes the image, rolls it out and waits for health checks.
Watch progress or reattach to a running deploy:

```bash
atlas deploy billing --env staging
atlas status billing --env staging
```

Production deploys need an approved change ticket. Reference the ticket when
you deploy:

```bash
atlas deploy billing --env production --ticket CHG-1234
```

Atlas refuses a production deploy without an approved ticket, and it records
the ticket in the audit log. Deploy during business hours unless you are
handling an incident, and announce production deploys in the team channel.
To see what will change before you deploy, run a dry run:

```bash
atlas deploy billing --env production --ticket CHG-1234 --dry-run
```

### Before your first production deploy

Work through this checklist once with your onboarding buddy:

1. Deploy the service to staging and confirm `atlas status` reports healthy.
2. Open the change ticket, link the pull request and describe the rollback plan.
3. Ask the service owner to approve the ticket.
4. Run the production dry run and read the summary of changes.
5. Deploy, then watch the dashboard for ten minutes.

```bash
atlas status billing --env staging
atlas tickets show CHG-1234
atlas deploy billing --env production --ticket CHG-1234
```

### Deploying several services

Deploy services one at a time, in dependency order, and wait for each to become
healthy before starting the next. Atlas does not coordinate deploys across
services, so a combined change needs one ticket per service:

```bash
atlas deploy accounts --env production --ticket CHG-1240
atlas deploy billing --env production --ticket CHG-1241
```

## Rollbacks

If a release misbehaves, roll back to a known good release:

```bash
atlas releases billing --env production
atlas rollback billing --to <release>
```

`atlas releases` lists recent releases with their identifiers and timestamps.
Rollbacks keep the previous database schema: Atlas does not run down
migrations. This means a rollback is safe only if the older code works with the
newer schema. Write migrations to be backwards compatible so that rollbacks are
always possible.

Roll back first and investigate afterwards. A rollback in production does not
need a new change ticket when it restores a release that was already approved,
but you must note it on the original ticket:

```bash
atlas rollback billing --to rel-2026-10-14-03 --env production --note "Error spike after rel-2026-10-21-01"
```

## Monitoring and alerts

Every service deployed with Atlas reports request rate, latency and error rate.
Open the service dashboard from the terminal:

```bash
atlas open dashboard billing --env production
```

The on-call engineer is paged when the error rate stays above 2% for 5
minutes. Short spikes below that duration do not page but do appear on the
dashboard. Check active alerts and silence a known issue while you fix it:

```bash
atlas alerts list --env production
atlas alerts silence <alert-id> --for 30m --reason "Known issue, rollback in progress"
```

Only silence alerts you are actively handling, and always give a reason. After
a deploy, watch the dashboard for at least ten minutes before moving on.

### Reading the dashboard

The dashboard has three panels. **Requests** shows traffic per minute and helps
you tell a real error spike from a traffic spike. **Latency** shows the 50th,
95th and 99th percentiles; a rising 99th percentile often comes before errors.
**Errors** shows the error rate as a percentage of requests, with a line at the
2% paging threshold. Compare the panels with the deploy marker that Atlas adds
at each release:

```bash
atlas releases billing --env production --limit 5
```

## Troubleshooting

**`atlas: command not found` after installation.** Homebrew's bin directory is
not on your `PATH`. Run `eval "$(/opt/homebrew/bin/brew shellenv)"` and add it
to your shell profile, then open a new terminal.

**`401 Unauthorized` on any command.** Your access token has expired; tokens
last 12 hours. Run `atlas login` and try again. If it persists, run
`atlas whoami` to confirm you are in the right SSO group.

**Connection refused or timeouts.** Check that `endpoint` in
`~/.atlas/config.toml` uses port 8443 and that you are on the corporate network
or VPN. `atlas doctor` reports which step fails:

```bash
atlas doctor --verbose
```

**Production deploy rejected: missing approval.** The change ticket is not
approved yet, or you passed the wrong ticket. Check its status with
`atlas tickets show CHG-1234` and ask the approver to review it.

**Deploy stuck waiting for health checks.** The new release is not becoming
healthy. Inspect its logs with `atlas logs billing --env staging --since 15m`,
fix the problem and deploy again, or roll back with
`atlas rollback billing --to <release>`.

**CI job fails with an expired API key.** API keys must be rotated every 90
days. Create a new key with `atlas keys create`, update the CI secret, and
revoke the old key with `atlas keys revoke <key-id>`.

**Rollback refused because of a newer schema.** Rollbacks keep the previous
database schema, and the older release cannot run against it. Deploy a fix
forward instead, or ask the database owner to review a manual migration.

**Config changes have no effect.** Atlas may be reading an environment variable
that overrides the file. Run `atlas config show` to see where each value comes
from, and unset the variable if it is stale.

When none of these apply, collect the output of `atlas doctor --verbose` and
ask in the platform support channel, including the command you ran and the
exact error message.
