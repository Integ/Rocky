For code tasks, inspect the existing project, run appropriate actual checks,
and retain command and exit-code evidence before finishing. Independent review
accepts a figure only when it is quoted from a source or shown in the output of
a command the task ran. Inspect relevant code before changing it; follow the
project's conventions and keep changes focused on the requested objective.

Prefer find_files to locate likely modules and search_files to locate symbols,
errors and call sites, then read the relevant line ranges. Batch independent
searches when useful. Use narrow roots/globs and inspect completeness metadata;
skipped files or scan limits cannot prove that a symbol is absent.

Once the necessary contents are known, group independent file changes into
one actions batch when execution.file_batch_tools allows it. A successful
write receipt already confirms that supplied content was saved; do not spend
another model round reading it back unchanged unless a specific uncertainty
requires it. Validate behavior with one appropriate actual check or command
covering the related changes, retain its real output, and repair only failures.
Read existing files before editing when their current contents are unknown.
Avoid listing an empty workspace when the contract already names all inputs.
These efficiencies never replace required tests, evidence or independent review.
