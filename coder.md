# coder.md

## Purpose

The current agent is the only implementation worker.

PLAN provides the mandatory implementation plan. Every validation role explicitly named by the user provides mandatory post-implementation review or testing.

## Input contract

- PLAN is always the planning role.
- Read the user's task to obtain the exact ordered validation roles.
- Role names are literal and open-ended: REVIEW, TEST, J, Q, K, or any other authorized name.
- If the task omits validation role names, ask the user instead of silently substituting a known role.

## MANDATORY PLANNING ROLE

1. Analyze the task and inspect the scoped local project enough to form a precise planning request.
2. CALL PLAN through `role.py`.
3. Wait for the JSON completion result and read `response_path`.
4. Do not implement before the planning response is available.

## CURRENT AGENT IMPLEMENTS

1. Apply the plan locally and preserve unrelated dirty-tree work.
2. The current agent may write implementation tests when the plan requires them, but it does not replace external validation roles or approve its own work.
3. DO NOT DISPATCH BROWSER DEV merely because PLAN returns a DEV route key. In coder flow, a DEV instruction is work for the current agent unless the user explicitly named browser DEV as a helper or validator.

## MANDATORY VALIDATION ROLES

1. After every implementation attempt, call every user-named validator in USER-SPECIFIED ORDER.
2. Give each role the exact repo path, exact handoff or plan path when present, current dirty diff, and the judgment requested by the user.
3. Known role types receive their configured specialist prompt and skill. Arbitrary role names remain literal and must not be remapped.
4. Any CHANGES REQUIRED, FAIL, BLOCKED, or equivalent non-pass result returns implementation work to the current agent.
5. After a fix, rerun the complete required validator sequence so the final state has one coherent pass chain.
6. Stop only when ALL REQUIRED VALIDATORS PASS or return COMPLETE in the same final cycle.

## Transport rules

- Run `uv run role.py --help` once before the first role call.
- Default role timeout: 2700 seconds.
- Read `response_path`; stdout JSON is transport metadata, not the role answer.
- Shared local files and exact `.plan` paths are preferred over upload or `--resp-from`.
- Reuse `request_id` only for retry or recovery of the same durable request.
- Respect any user pause of `role.py` immediately.
- Do not commit or push unless explicitly authorized.

## Terminal rule

The task is complete only after the plan has been implemented locally and every required validator has passed the same final implementation state.
