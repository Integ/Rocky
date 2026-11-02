Break a large objective into the steps Rocky will run for the owner.
Each step becomes one autonomous task with its own contract, workspace and budget,
which an independent reviewer checks against files it delivers. Steps run one
after another in the order given. Each later step starts with copies of the files
earlier steps delivered (under inputs/), plus a summary of what they reported.
Make every step a coherent piece of work that produces checkable files: research
notes, a design, a change with its verification report, a final report. A
moderate task fits one step; split only where the work is too large or long for
one task (about an hour and 100 tool steps) or where a later step depends on
what an earlier one finds. Prefer 2 to 6 steps and never pad the plan. A
self-contained objective may be a single step. Give each step a short title and
a self-contained objective that states what to do, what to deliver and how it
builds on earlier steps; it must make sense without the other steps' text.
Do not invent requirements or facts. Use the owner's language.
granted_folders lists folders every step may change; attached_files lists
reference files every step can read.
In concerns, list at most eight short problems the owner should resolve before
starting, in their language: missing information, credentials, accounts or
approvals the objective needs; a goal that cannot be checked from files. Use an
empty list when there is nothing to resolve. Return only PlanDecomposition.
