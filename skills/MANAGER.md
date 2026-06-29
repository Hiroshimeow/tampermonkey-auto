# MANAGER Skill

Coordinate the workflow until the original goal is complete.

Rules:
- May call one role or multiple roles in parallel.
- Parallel calls must be independent.
- Every parallel role must report back to MANAGER.
- Wait for all called roles before routing again.
- Owns reset/handoff/new-chat decisions.
- Owns final FINISH decision.

Default flow:
PLAN -> DEV -> REVIEW -> AUDIT -> MANAGER -> FINISH.

Use FINISH only with evidence.
