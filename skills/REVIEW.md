# REVIEW Skill

Mode-sensitive review behavior.

If the call is mode 2, role works with agent: synthesize or judge only the supplied prompt/uploads and answer directly. Do not inspect local repo, write `.plan/*.md`, or route JSON.

If the call is mode 1, role works on local: review DEV work with engineering judgment. Read the code and evidence. Prioritize correctness, regressions, missing tests, unsafe assumptions, and scope drift.

Send blockers to DEV with precise fixes. Send acceptable but incomplete work to PLAN. Send the work to FINISH only when the full goal is complete and verified.
