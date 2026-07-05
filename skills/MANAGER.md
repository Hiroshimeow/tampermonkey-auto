# MANAGER Skill

Mode-sensitive coordination behavior.

If the call is mode 2, role works with agent: synthesize uploaded workflow state or role responses and answer directly. Do not inspect local repo, write `.plan/*.md`, or route JSON.

If the call is mode 1, role works on local: coordinate the workflow. MANAGER may call one role or several independent roles in parallel. Parallel roles report back to MANAGER before the workflow moves on. MANAGER owns reset, handoff, new-chat decisions, and final FINISH.

Default local flow: PLAN -> DEV -> REVIEW -> AUDIT -> MANAGER -> FINISH.
