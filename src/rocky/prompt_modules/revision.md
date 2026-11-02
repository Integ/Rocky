Review the owner's long-running plan after one of its steps finished.
plan.objective is the overall goal. finished lists completed or skipped steps with
the summary and files each delivered; remaining lists steps that have not started.
Each step runs as one autonomous task that receives copies of earlier deliverables.
Set complete to true only when the finished steps already achieve the overall
objective, so the remaining steps are unnecessary. Otherwise return in remaining
the steps still needed, in order: keep the planned ones when they still fit,
adjust them to what earlier steps found, add a step a finding requires, or drop
one that is no longer needed. Never repeat finished work, except to fix a gap a
summary reports. Keep each objective self-contained. At most max_remaining steps.
In note, give the owner one short sentence on what changed and why, or leave it
empty when the plan stands. Use the owner's language. Return only PlanRevision.
