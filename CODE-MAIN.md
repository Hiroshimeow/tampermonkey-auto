CODE-MAIN.md

This file defines the reviewed `PLAN -> REVIEW -> local implementation` workflow.

## 1. Purpose

Use this mode when the user wants repository work to go through:

1. web `PLAN`,
2. web `REVIEW`,
3. local implementation,
4. optional final web `REVIEW`.

Use it for:

- substantial design,
- risky patches,
- multi-step implementation,
- tasks where the chat agent should coordinate but not design, review, or implement directly.

Do not use it for:

- quick answers,
- pure explanation,
- one-off writing tasks,
- direct local edits before review,
- implementation by web `DEV`.

## 2. Core Invariant

The coordinator may route, upload, collect, and report evidence.

The coordinator must not:

- design,
- review,
- implement,
- materially alter the reviewed plan,
- patch missing details from assumptions.

Implementation starts only after web `REVIEW` explicitly passes web `PLAN` output.

If the reviewed plan is incomplete, ambiguous, unsafe, or wrong, return to `PLAN -> REVIEW`.

## 3. Ownership

| Actor | Owns | Must Not Do |
| --- | --- | --- |
| Coordinator | user communication, `role.py` calls, uploads, loop control, worker launch, evidence collection, final reporting | design, review, implement, or rewrite the reviewed plan |
| Web `PLAN` | analysis, design, implementation plan, risks, acceptance criteria, evidence requirements, blocker revisions | implement or route to web `DEV` |
| Web `REVIEW` | strict review of `PLAN` output, optional final implementation review | pass ambiguous or incomplete plans |
| Local implementation worker | repo inspection, edits, checks, diff/evidence reporting | expand scope, guess missing requirements, ignore blockers |
| Web `DEV` | nothing in this mode | implementation unless the user explicitly exits this workflow |

## 4. Role Constraints

Call only roles confirmed online by the user or runtime context.

Normal `CODE-MAIN.md` flow:

- use web `PLAN` and web `REVIEW`,
- call them in mode 2,
- upload exact context,
- ask for direct answers,
- do not request route JSON,
- do not use web `DEV`.

If a repo-managed mode 1 role workflow is explicitly used, `AGENTS.md` still applies.

Do not mix mode 2 direct-answer calls with repo-managed route workflows silently.

If a mode 2 role returns only a local path, call the same role again and require a direct answer.

## 5. New-Chat Policy

Use `--new-chat` only at a real task boundary.

General rule:

- same logical task still in progress: do not use `--new-chat`,
- retry, clarification, blocker revision, or next step of the same task: do not use `--new-chat`,
- new task after the prior task is complete, abandoned, or irrelevant: prefer `--new-chat`,
- old context still matters: upload the needed plan, review, report, diff, or handoff first, then use `--new-chat`,
- if unsure whether it is the same task, keep the existing chat.

Treat `--new-chat` as resetting that web role to `history = 0`.

### PLAN Rule

Do not use `--new-chat` for `PLAN` while the same planning cycle is active.

No `--new-chat` for `PLAN` when:

- refining the same task,
- revising after a `REVIEW` blocker,
- retrying the same planning request,
- answering follow-up questions for the same task.

Use `--new-chat` for `PLAN` only when:

- the old planning task is done,
- the old planning task is abandoned or irrelevant,
- a new unrelated task starts,
- or all needed old context has already been moved into uploaded files and a clean reset is intentional.

### REVIEW Rule

Keep the same chat for the active `PLAN -> REVIEW` cycle. Do not start a new chat between `PLAN` and `REVIEW` turns just because a new prompt is being sent.

Safe uses:

- first request to a fresh role,
- new task after previous task completion,
- explicit role handoff/reset request,
- all needed old context captured in uploads.

Unsafe uses:

- active plan-review cycle,
- retry of the same unfinished request,
- old context exists only in chat history,
- role may still be generating.

## 6. Role-Call Flow

Use `role.py` as the only transport command for web roles.

Use `--prompt` for concise instructions and `--upload` for exact context. Do not paste large file contents into prompts.

Typical calls:

```powershell
uv run role.py --role PLAN --prompt "Work with agent. Produce the requested implementation plan from uploaded context. Answer directly. Do not write files. Do not route." --upload "E:\repo\task.md" --upload "E:\repo\AGENTS.md" --upload "E:\repo\ROLE.md"
uv run role.py --role REVIEW --prompt "Work with agent. Strictly review the uploaded PLAN output. Return PASS or BLOCKED. Answer directly." --upload "E:\repo\.role_state\responses\req_PLAN_xxx.md"
```

After every successful call:

- confirm `ok: true`,
- confirm `status: completed`,
- read `response_path`,
- continue from that content.

Use a 30-minute timeout for `role.py`.

## 7. Upload Rules

Upload exact files whenever their content matters:

- task files,
- repo instructions,
- `ROLE.md`,
- `AGENTS.md`,
- prior `PLAN` or `REVIEW` outputs,
- implementation reports,
- diffs,
- patches,
- check logs,
- screenshots,
- handoffs.

Do not paste large files into prompts. Do not send only a repo path to a mode 2 role.

## 8. PLAN -> REVIEW Loop

Loop until `REVIEW` explicitly returns `PASS`.

Do not use `--new-chat` inside the same active plan-review cycle unless the role requested handoff/reset or all required context has already been captured in uploads and a clean reset is intentional.

Flow:

1. Ask `PLAN` for the detailed design or implementation task.
2. Ask `REVIEW` to review that exact `PLAN` output.
3. If `REVIEW` blocks, upload both the plan and blockers back to `PLAN`.
4. Ask `PLAN` for a complete revised plan.
5. Send the revised plan back to `REVIEW`.
6. Repeat until explicit `PASS`.

Not pass:

- "Looks good overall."
- "Probably fine."
- "No major concerns."
- "Proceed, but clarify later."
- "Pass with blockers."
- "Conditional pass."

Treat ambiguous approval as `BLOCKED`.

## 9. REVIEW Pass Standard

`REVIEW` may pass a plan only if the plan is:

- complete,
- internally consistent,
- safe enough for the task,
- compliant with repo instructions,
- executable by a local implementation worker without invention,
- specific about files to inspect and likely files to change,
- specific about constraints and forbidden changes,
- specific about acceptance criteria,
- specific about checks,
- specific about required evidence.

If any of these are missing, `REVIEW` should return `BLOCKED`.

## 10. Local Implementation

After `REVIEW` passes, delegate implementation to one of:

1. a local subagent with repository access,
2. `codex exec "<reviewed detailed implementation task>" --model gpt-5.4`,
3. `codex exec "<reviewed detailed implementation task>" --model gpt-5.5` for hard or high-risk tasks.

The local worker may inspect files, edit files, run checks, collect diffs, write durable evidence artifacts, and produce a final implementation report.

The local worker must:

- follow the reviewed task exactly,
- avoid scope expansion,
- stop and report blockers when the reviewed task is insufficient,
- report changed files, commands, checks, skipped checks, diffs, blockers, and deviations,
- save or print enough evidence for final `REVIEW`.

The local worker must not:

- implement unrelated improvements,
- silently change architecture beyond the reviewed task,
- skip required checks without explanation,
- resolve missing requirements by speculation,
- treat `REVIEW` blockers as optional,
- claim success without check evidence.

## 11. Evidence and Model Rules

Capture durable evidence when practical:

```powershell
codex exec "<reviewed detailed implementation task>" --model gpt-5.4 | Tee-Object -FilePath ".plan\local-implementation-report.md"
git diff -- . ':!.role_state' | Tee-Object -FilePath ".plan\implementation.diff"
uv run python -m pytest 2>&1 | Tee-Object -FilePath ".plan\check-output.log"
```

The evidence package should include:

- reviewed plan or task,
- local worker report,
- changed file list,
- diff or patch,
- check output,
- skipped-check explanations,
- blocker report if blocked,
- deviation report if the worker deviated from the reviewed plan.

If artifacts cannot be written, the worker must print equivalent evidence in its final response.

Use `gpt-5.4` by default.

Use `gpt-5.5` when the task is high-risk, architecturally complex, spans multiple subsystems, requires migration/compatibility reasoning, requires nontrivial test design, or a prior implementation attempt failed because of reasoning rather than syntax.

## 12. Post-Implementation Review

After local implementation, collect the reviewed plan, worker report, changed file list, diff, checks, skipped checks, blockers, and deviations.

Send the evidence to web `REVIEW` when:

- the change is nontrivial,
- the task is high-risk,
- checks failed,
- the worker skipped required checks,
- the worker deviated from the plan,
- the worker reported uncertainty,
- final review was requested,
- final review is part of the workflow.

If final `REVIEW` blocks:

- do not patch from coordinator assumptions,
- send blockers and the reviewed plan to the local implementation worker,
- ask the worker to fix only the blockers,
- collect new evidence,
- return to final `REVIEW`.

## 13. Failure and Retry

- `completed`: read `response_path`.
- `failed_retryable`: retry the same request, preferably with `--request-id`; do not change prompt/uploads unless input was bad.
- `failed_final`: fix obvious input errors or report `request_id`, `error_id`, `log_path`, role, and phase.
- timeout before JSON: retry the same command; reuse `request_id` if known.

If `PLAN` lacks context, upload the missing file/evidence if available and ask `PLAN` to revise. Do not fill missing technical details yourself.

If `REVIEW` blocks, upload the plan and blockers back to `PLAN`, then send the revised plan back to `REVIEW`. Continue until explicit `PASS`.

If the worker is blocked by an insufficient reviewed plan, return to `PLAN -> REVIEW`.

If checks fail:

- in-scope failure: ask the worker to fix it,
- missing/wrong plan assumption: return to `PLAN -> REVIEW`,
- unrelated/pre-existing failure: require evidence and report clearly or send to `REVIEW`.

## 14. Closeout Rules

`PLAN` must produce a concrete implementation task.

`REVIEW` must return explicit `PASS` or `BLOCKED`.

The local worker must produce a report or final response with changed files, commands, checks, diff summary, deviations, and blockers.

The final user response must include final status, whether `REVIEW` passed, changed files, checks, unresolved blockers if any, deviations if any, tooling failure IDs if blocked, and a clear statement if implementation was not performed.

Before moving forward, check:

- is the exact `PLAN` output uploaded to `REVIEW`,
- did `REVIEW` explicitly pass before local implementation,
- is the worker receiving the reviewed task unchanged,
- are final claims backed by evidence.

## 15. Non-Negotiable Rule

When in doubt, route back to `PLAN -> REVIEW`.

The coordinator’s job is to preserve reviewed intent, not to repair the plan from unstated assumptions.
