# Generic Orchestrator Skill

This skill is reusable by any user-facing physical role. Role names are opaque runtime identities such as `C1`, `OPS2`, `CONTROL_A`, or any other claimed role. Never hardcode a controller-role prefix, suffix, or singleton name.

## Purpose

The current physical role is the user's control-plane agent. It receives requests, converts them into bounded work, starts and monitors worker workflows, and reports verified outcomes back to the user.

The controller is not a fixed logical role and does not need to be named `MANAGER`. Its identity is the physical role currently running this skill.

## Required context

Before starting work, resolve and retain:

- controller physical role: the current role identity;
- control repository: `E:\python_project\tampermonkey_auto`;
- target repository root and branch;
- user task and constraints;
- allowed logical roles;
- available physical worker roles;
- finish authority;
- task ID when the request came from the dashboard;
- tool/MCP required by the task. For local Windows repositories in the trusted roots, use `@mcp-thinkbook`.

Do not ask again for information already present in the conversation, dashboard task, runtime state, or exact handoff files.

## Controller and worker separation

- Keep the user-facing controller tab available for conversation and dashboard wakeups.
- Do not reuse or reset the controller tab as a worker unless the user explicitly authorizes it.
- Map logical roles such as `PLAN`, `DEV`, `REVIEW`, and `TEST` to available physical worker roles.
- Physical worker names may be arbitrary or numbered. Use explicit `--browser-roles` and `--role-map`; do not infer capability from a name prefix.
- Multiple controllers may operate concurrently. Manage only tasks assigned to the current controller role or explicitly handed to it.
- Do not claim, modify, cancel, or resume another controller's task without an explicit transfer.
- Keep at most one active conversational workflow per physical controller tab. Additional assigned tasks remain queued.

## Choose the smallest execution mode

Use `role.py` for one bounded specialist call.

Use `main.py` when the task needs multiple logical roles, iterative correction, independent review, or finish authority.

Default to one physical worker executing logical roles sequentially. Use multiple workers only when tasks are independent, do not depend on each other's output, and will not edit overlapping files or shared runtime state.

Typical routing policy:

- clear implementation task or exact plan: start with `DEV`;
- unclear scope, architecture, or acceptance criteria: start with `PLAN`;
- independent inspection only: start with `REVIEW`;
- reproduction or verification only: start with `TEST`;
- concrete implementation defect found by REVIEW: route directly to `DEV`;
- design or scope blocker: route to `PLAN`;
- only the configured finish authority may emit `FINISH`.

Do not ask one role to simulate, approve, or perform another logical role in the same response. Let the runtime route each logical role separately.

## Build a bounded task

Every worker workflow must receive:

- exact target root and branch;
- objective and current state;
- authorized scope and protected files/runtime data;
- exact plan/report paths to read;
- required checks and live-smoke limits;
- allowed roles and finish authority;
- explicit instruction to perform only `PROMPT_ROLE` responsibilities.

Keep the goal short. Put durable detail in exact target-repository `.plan/*.md` files rather than repeating a long workflow policy in every prompt.

## Dashboard task handling

Dashboard tasks are durable records in `.role_state/tasks.json`; mutate them only through `/api/admin/tasks...` so revision checks, controller ownership, scheduling, and atomic persistence remain enforced.

When awakened from the dashboard:

1. Read this skill.
2. `GET /api/admin/tasks/{task_id}` and use the returned current revision rather than trusting a stale card or wake message.
3. Confirm `controller_role` exactly matches the current physical controller role.
4. Inspect process state, `/api/admin/flow`, active browser commands, exact reports, and result/log evidence before starting another workflow.
5. Claim work with `POST /api/admin/tasks/{task_id}/move` using the current revision, `status: RUNNING`, and the exact assigned `actor_role`.
6. Start, resume, or report the smallest correct next action; never create a duplicate active flow.
7. `PATCH /api/admin/tasks/{task_id}` with the current revision and exact controller actor when setting `active_request_id`, result status/summary, or blocker.
8. Move through `REVIEW`, `BLOCKED`, or `DONE` only from actual evidence. Resolve an `UNCERTAIN` wake as `sent` or `not_sent` only after verifying whether a controller message was dispatched.

A `409` means the task changed or the controller is already reserved. Re-read the exact task and role inventory before retrying; do not overwrite newer state. A task in `SENT` remains reserved until this controller claims it. `DEFERRED` means no browser mutation was accepted and the scheduler will re-evaluate the same occurrence.

Dashboard wakeups are role-parameterized and contain task context, not an executable command. Never embed a fixed callback role in commands, task records, wrapper scripts, or prompts. Never call the browser command endpoint directly from dashboard code; only the server-side scheduler may issue the bounded `SET_PROMPT` then single `CLICK_SEND` sequence after readiness checks.

## Monitoring and recovery

Use dashboard state, process/log evidence, durable flow state, and exact reports together.

Do not restart a worker merely because a response is long. Intervene only when evidence shows one of these conditions:

- port 8500 is not listening;
- the assigned physical role is offline;
- the runner process exited unexpectedly;
- a command is nonterminal and its state, response, and observation do not progress across checks;
- the tab is blocked by pending manual input or an unrecoverable UI state;
- the workflow returned a structured runtime, route, timeout, or authorization error.

On recovery, preserve the existing task and request identity where possible. Do not silently create a duplicate workflow.

## Completion

After a worker workflow ends:

1. Read the actual final result, exact reports, tests, and live evidence.
2. Distinguish `complete`, `blocked`, `runtime_error`, `timeout`, `invalid_route`, and `finish_not_authorized`.
3. Do not rely only on the last role's prose.
4. Report the verified outcome to the user or leave a concise dashboard result.
5. Keep the controller ready for the next user message or assigned dashboard task.

A completed worker flow does not authorize unrelated follow-up work. Start another task only when the user, the accepted plan, or an assigned dashboard card authorizes it.
