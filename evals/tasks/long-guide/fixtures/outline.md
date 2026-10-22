# Atlas onboarding guide — outline

Atlas is the internal command-line tool engineers use to deploy services.
Write the guide for an engineer in their first week. Use exactly these eight
second-level section headings, in this order:

1. Overview — what Atlas does and who should use it.
2. Installation — install with `brew install atlas-cli`; requires macOS 14 or later.
3. Configuration — settings live in `~/.atlas/config.toml`; the API endpoint
   listens on port 8443; show a complete example file.
4. Authentication — `atlas login` opens SSO; access tokens expire after 12 hours;
   API keys must be rotated every 90 days.
5. Deploying a service — `atlas deploy <service> --env staging|production`;
   production deploys need an approved change ticket.
6. Rollbacks — `atlas rollback <service> --to <release>`; rollbacks keep the
   previous database schema.
7. Monitoring and alerts — the on-call engineer is paged when the error rate
   stays above 2% for 5 minutes.
8. Troubleshooting — at least five common problems, each with its fix.

Include worked command examples in every section after the overview.
