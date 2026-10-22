# Ops review — 2026-10-06

Attendees: Aiko, Lena, Samir, Noor, Elias

## Notes

Aiko: People agreed the meeting ran long and that the agenda should be circulated a day in advance.
Noor: We should probably double-check the dependency licence report before the external audit next month.
Samir: The team compared three dashboard layouts but did not choose one; mock-ups will be shared asynchronously.
Samir: The analytics events for the settings page are firing twice; there is a ticket but it has no owner yet.
Elias: The cost report shows storage growing faster than compute, mostly from retained build artefacts.
Elias: We should probably double-check the dependency licence report before the external audit next month.
Aiko: Two engineers described a flaky integration test that fails roughly once in twenty runs.
Samir: The incident retrospective format was praised, and the facilitator offered to run the next one as well.
Elias: I looked at the error budget for last month and we used about sixty percent of it, mostly during the DNS incident.
Aiko: Budget for the next quarter was mentioned briefly; finance will send the final numbers separately.
Elias: The canary deployment caught a memory leak last week, which is a good argument for keeping it.
Samir: I can pair with the new hire on the deployment pipeline this week if someone else covers code review.
Noor: Feature flags older than six months should be cleaned up; there are about fifteen of them.
Elias: Two engineers described a flaky integration test that fails roughly once in twenty runs.

Decision (DEC-11): add a second on-call rotation for EU hours. Owner: Aiko.

Noor: The security questionnaire from the enterprise prospect has forty questions; most answers already exist.
Lena: I looked at the error budget for last month and we used about sixty percent of it, mostly during the DNS incident.
Aiko: The canary deployment caught a memory leak last week, which is a good argument for keeping it.
Aiko: The mobile crash rate dropped after the last patch, but Android 12 devices are still overrepresented.
Elias: People agreed the meeting ran long and that the agenda should be circulated a day in advance.
Samir: The canary deployment caught a memory leak last week, which is a good argument for keeping it.
Aiko: We should probably double-check the dependency licence report before the external audit next month.
Elias: I can pair with the new hire on the deployment pipeline this week if someone else covers code review.
Aiko: The load test from Tuesday peaked at roughly four thousand requests per second before latency climbed.
Elias: The load test from Tuesday peaked at roughly four thousand requests per second before latency climbed.
Noor: I can pair with the new hire on the deployment pipeline this week if someone else covers code review.
Noor: My understanding is that the design review is still pending, so I would not start implementation yet.

DEC-12 replaces DEC-06: nightly reports now run at 04:30 UTC instead. Lena takes over ownership from Samir.

Elias: The vendor contract renewal is still with legal and is not expected before the end of the month.
Lena: Could we get a rough estimate for the accessibility fixes so product can plan around them?
Noor: The incident retrospective format was praised, and the facilitator offered to run the next one as well.
Elias: The security questionnaire from the enterprise prospect has forty questions; most answers already exist.
Samir: The cost report shows storage growing faster than compute, mostly from retained build artefacts.
Elias: The group reviewed last week's action items and confirmed that most were closed on time.
Samir: My understanding is that the design review is still pending, so I would not start implementation yet.
Lena: The canary deployment caught a memory leak last week, which is a good argument for keeping it.
Elias: Feature flags older than six months should be cleaned up; there are about fifteen of them.
Samir: Documentation for the internal CLI is scattered across three places and should be consolidated.
Noor: I am not convinced the new queue library is mature enough, but the benchmark results look promising.
Lena: There is a backlog of thirty-odd low-priority bugs that nobody has triaged since August.

## Action items

- Circulate these notes.
- Update the project tracker.
