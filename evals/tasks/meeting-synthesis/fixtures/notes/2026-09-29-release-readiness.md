# Release readiness — 2026-09-29

Attendees: Marco, Priya, Tomás, Noor, Elias

## Notes

Elias: Budget for the next quarter was mentioned briefly; finance will send the final numbers separately.
Marco: Somebody should confirm whether the old reporting cluster can be decommissioned after the migration.
Priya: There is a backlog of thirty-odd low-priority bugs that nobody has triaged since August.
Tomás: The vendor contract renewal is still with legal and is not expected before the end of the month.
Tomás: The team compared three dashboard layouts but did not choose one; mock-ups will be shared asynchronously.
Priya: Let's not reopen the naming discussion; the current names are fine and changing them costs a migration.
Tomás: A question about weekend deploys was parked for a future meeting because key people were absent.
Noor: Somebody should confirm whether the old reporting cluster can be decommissioned after the migration.
Tomás: The support team asked for a weekly summary of shipped changes in plain language.
Elias: The group reviewed last week's action items and confirmed that most were closed on time.
Elias: The load test from Tuesday peaked at roughly four thousand requests per second before latency climbed.
Marco: The vendor contract renewal is still with legal and is not expected before the end of the month.
Tomás: Two engineers described a flaky integration test that fails roughly once in twenty runs.
Marco: I would like the retro action items tracked in the same board as sprint work so they do not get lost.

Decision DEC-09 supersedes DEC-02: the mobile beta moves to 2026-11-05. Owner: Marco.

Priya: Can we make sure the release notes mention the changed default timeout? Support was surprised by it last time.
Tomás: Feature flags older than six months should be cleaned up; there are about fifteen of them.
Priya: We should probably double-check the dependency licence report before the external audit next month.
Priya: I looked at the error budget for last month and we used about sixty percent of it, mostly during the DNS incident.
Elias: We might want a lightweight RFC process for changes that touch more than two services.
Tomás: A customer escalation from the previous sprint was summarised, including the timeline and the follow-up email.
Marco: The canary deployment caught a memory leak last week, which is a good argument for keeping it.
Tomás: There was a long discussion about whether the staging environment reflects production closely enough.
Marco: We should probably double-check the dependency licence report before the external audit next month.
Priya: I looked at the error budget for last month and we used about sixty percent of it, mostly during the DNS incident.
Priya: I am not convinced the new queue library is mature enough, but the benchmark results look promising.
Elias: The canary deployment caught a memory leak last week, which is a good argument for keeping it.

DEC-10 — the public API schema is frozen from 2026-10-20. Priya owns the freeze.

Elias: The group reviewed last week's action items and confirmed that most were closed on time.
Marco: The team compared three dashboard layouts but did not choose one; mock-ups will be shared asynchronously.
Priya: Somebody should confirm whether the old reporting cluster can be decommissioned after the migration.
Marco: Let's not reopen the naming discussion; the current names are fine and changing them costs a migration.
Elias: I would like the retro action items tracked in the same board as sprint work so they do not get lost.
Priya: The canary deployment caught a memory leak last week, which is a good argument for keeping it.
Priya: There was a long discussion about whether the staging environment reflects production closely enough.
Tomás: Let's not reopen the naming discussion; the current names are fine and changing them costs a migration.
Marco: The analytics events for the settings page are firing twice; there is a ticket but it has no owner yet.
Priya: Questions about data residency were collected and will be answered by the compliance team in writing.
Marco: The analytics events for the settings page are firing twice; there is a ticket but it has no owner yet.
Elias: Could we get a rough estimate for the accessibility fixes so product can plan around them?

There was a question whether DEC-04 also covers staging; it does not, and no new decision was made.

Elias: Hiring for the open backend role continues; three candidates are in the final round.
Priya: A question about weekend deploys was parked for a future meeting because key people were absent.
Priya: Could we get a rough estimate for the accessibility fixes so product can plan around them?
Marco: The incident retrospective format was praised, and the facilitator offered to run the next one as well.
Tomás: Could we get a rough estimate for the accessibility fixes so product can plan around them?
Priya: Can we make sure the release notes mention the changed default timeout? Support was surprised by it last time.
Priya: Budget for the next quarter was mentioned briefly; finance will send the final numbers separately.
Noor: There was a long discussion about whether the staging environment reflects production closely enough.
Noor: The on-call handbook still describes the old paging tool; nobody volunteered to update it today.
Priya: Documentation for the internal CLI is scattered across three places and should be consolidated.
Tomás: The load test from Tuesday peaked at roughly four thousand requests per second before latency climbed.
Priya: The on-call handbook still describes the old paging tool; nobody volunteered to update it today.

## Action items

- Circulate these notes.
- Update the project tracker.
