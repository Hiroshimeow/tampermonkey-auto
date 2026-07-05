# PLAN Skill

Mode-sensitive planning behavior.

If the call is mode 2, role works with agent: use only supplied prompt/uploads, synthesize or critique directly, and do not write `.plan/*.md` or route JSON.

If the call is mode 1, role works on local: turn the current goal and feedback into the next DEV step. Include the objective, assumptions, acceptance criteria, expected evidence, and exact DEV instruction. In the 3-agent workflow, PLAN routes to DEV. On every 4th PLAN execution, request a DEV handoff with a HANDOFF section and `"command":"handoff"`.

PLAN does not implement, review, or finish.
