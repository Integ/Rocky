# Security review — 2026-09-08

Attendees: Aiko, Tomás, Grace, Noor, Elias

## Notes

Aiko: Feature flags older than six months should be cleaned up; there are about fifteen of them.
Tomás: Somebody should confirm whether the old reporting cluster can be decommissioned after the migration.
Tomás: A question about weekend deploys was parked for a future meeting because key people were absent.
Noor: I talked to two customers about the import feature and both asked for better error messages first.
Grace: I would like the retro action items tracked in the same board as sprint work so they do not get lost.
Tomás: I can pair with the new hire on the deployment pipeline this week if someone else covers code review.
Aiko: The on-call handbook still describes the old paging tool; nobody volunteered to update it today.
Aiko: I can pair with the new hire on the deployment pipeline this week if someone else covers code review.
Noor: Can we make sure the release notes mention the changed default timeout? Support was surprised by it last time.
Aiko: A short demo showed the new search filters, which received positive feedback but no commitment yet.
Elias: I can pair with the new hire on the deployment pipeline this week if someone else covers code review.
Tomás: We might want a lightweight RFC process for changes that touch more than two services.
Noor: Documentation for the internal CLI is scattered across three places and should be consolidated.
Tomás: I looked at the error budget for last month and we used about sixty percent of it, mostly during the DNS incident.

Decision (DEC-03): service API keys are rotated every 90 days (owner: Aiko).

Tomás: Questions about data residency were collected and will be answered by the compliance team in writing.
Elias: Somebody should confirm whether the old reporting cluster can be decommissioned after the migration.
Grace: We need a clearer owner for the shared component library; right now changes sit in review for days.
Noor: We need a clearer owner for the shared component library; right now changes sit in review for days.
Noor: Let's not reopen the naming discussion; the current names are fine and changing them costs a migration.
Tomás: The vendor contract renewal is still with legal and is not expected before the end of the month.
Elias: We might want a lightweight RFC process for changes that touch more than two services.
Aiko: A customer escalation from the previous sprint was summarised, including the timeline and the follow-up email.
Aiko: Hiring for the open backend role continues; three candidates are in the final round.
Tomás: The canary deployment caught a memory leak last week, which is a good argument for keeping it.
Elias: The team compared three dashboard layouts but did not choose one; mock-ups will be shared asynchronously.
Noor: I talked to two customers about the import feature and both asked for better error messages first.

Someone asked whether DEC-01 affects the audit database; no change was made.

Elias: Documentation for the internal CLI is scattered across three places and should be consolidated.
Elias: I looked at the error budget for last month and we used about sixty percent of it, mostly during the DNS incident.
Elias: The group reviewed last week's action items and confirmed that most were closed on time.
Aiko: Somebody should confirm whether the old reporting cluster can be decommissioned after the migration.
Grace: We should probably double-check the dependency licence report before the external audit next month.
Aiko: My understanding is that the design review is still pending, so I would not start implementation yet.
Noor: A question about weekend deploys was parked for a future meeting because key people were absent.
Noor: The group reviewed last week's action items and confirmed that most were closed on time.
Grace: There is a backlog of thirty-odd low-priority bugs that nobody has triaged since August.
Tomás: There is a backlog of thirty-odd low-priority bugs that nobody has triaged since August.
Aiko: The load test from Tuesday peaked at roughly four thousand requests per second before latency climbed.
Elias: Feature flags older than six months should be cleaned up; there are about fifteen of them.

DEC-04 — production access requires a hardware security key from now on. Tomás will drive the rollout.

Tomás: Hiring for the open backend role continues; three candidates are in the final round.
Grace: A question about weekend deploys was parked for a future meeting because key people were absent.
Elias: The mobile crash rate dropped after the last patch, but Android 12 devices are still overrepresented.
Aiko: Feature flags older than six months should be cleaned up; there are about fifteen of them.
Grace: We might want a lightweight RFC process for changes that touch more than two services.
Aiko: Two engineers described a flaky integration test that fails roughly once in twenty runs.
Grace: The load test from Tuesday peaked at roughly four thousand requests per second before latency climbed.
Tomás: A customer escalation from the previous sprint was summarised, including the timeline and the follow-up email.
Tomás: The security questionnaire from the enterprise prospect has forty questions; most answers already exist.
Aiko: Someone noted that the internal wiki page for this area is out of date and links to a retired runbook.
Noor: The team compared three dashboard layouts but did not choose one; mock-ups will be shared asynchronously.
Elias: The vendor contract renewal is still with legal and is not expected before the end of the month.

## Action items

- Circulate these notes.
- Update the project tracker.
