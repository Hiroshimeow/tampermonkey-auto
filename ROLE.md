# ROLE.md

This is the operating manual for using online roles through `role.py`.

When the user says "read ROLE.md", the agent must:

- use only the online roles listed by the user,
- decide whether it is transport-only or a primary worker,
- follow this file for role-call mechanics.

## Agent Style

Choose one style for the current task.

### Style A: Transport/Orchestrator

Use this when the agent should behave like an external/manual `main.py`.

Allowed:

- run `role.py`,
- upload explicit files with `--upload`,
- read returned `response_path` files,
- retry the same request when retry rules say to retry,
- report exact `request_id`, `error_id`, and `log_path` when blocked.

Forbidden:

- inspect source files to solve the task,
- edit code,
- review code or diffs,
- create implementation plans,
- synthesize conclusions from multiple workstreams,
- call MCP/project tools except to run `role.py` or read returned files.

### Style B: Primary Worker

Use this when the current agent owns the work and roles are helpers.

Rules:

- The current agent may use normal local tools according to the task.
- Roles are helper subagents, not the whole workflow engine.
- Prefer mode 2 when the role should answer from uploaded context.
- Use mode 1 only when intentionally delegating local repo work.
- If a mode 2 helper returns only a path, call it again and require a direct answer.
- If a mode 1 helper returns a `.md` path, use or upload that report according to the task.

## Work Modes

Classify each call by behavior, not by role name.

### Mode 1: Role Works On Local

Use this when the role should inspect or modify local state: repo files, filesystem, MCP tools, tests, git state, implementation, review, audit, or handoff writing.

Rules:

- The role may use local tools according to its prompt and task.
- The role may write durable `.md` reports under `.plan/`.
- Returning an exact report path is valid.
- In Style A, pass that path or upload that file to the next role; do not reinterpret the local work yourself.
- In Style B, use or upload the report according to the task.

Example:

```powershell
uv run role.py --role PLAN --prompt "Work on local repo. Create an implementation plan. Write it to .plan/plan.md and return the exact path."
uv run role.py --role DEV --prompt "Work on local repo. Implement the attached plan. Write a dev report to .plan/dev-report.md and return the exact path." --upload "E:\repo\.plan\plan.md"
uv run role.py --role REVIEW --prompt "Work on local repo. Review the attached dev report and implementation. Return blockers first; if none, PASS." --upload "E:\repo\.plan\dev-report.md"
```

### Mode 2: Role Works With Agent

Use this when the role should answer from context supplied by the current agent.

Rules:

- Upload every file whose content matters.
- Do not send only a repo path and expect the role to read it.
- Do not ask for `.md` reports or handoff files.
- Ask for a direct answer.
- Avoid local MCP/repo tools.
- After completion, read `response_path` and use that answer.
- If the role returns only a local path, call it again and require a direct answer.

Example:

```powershell
uv run role.py --role REVIEW --prompt "Synthesize the uploaded DEV and AUDIT responses. Return the final decision directly." --upload "E:\repo\.plan\dev-response.md" --upload "E:\repo\.plan\audit-response.md"
```

## Runtime Contract

Use `role.py` as the only transport command.

Basic calls:

```powershell
uv run role.py --role DEV --prompt "Implement the attached plan." --upload "E:\path\to\plan.md"
uv run role.py --role REVIEW --prompt "Review the attached report and patch. Return blockers first." --upload "E:\repo\.plan\dev-report.md" --upload "E:\repo\patch.diff"
```

Options:

- `--prompt`: short, self-contained instruction.
- `--upload <path>`: exact file context.
- `--resp-from ROLE`: prefix with up to 3 latest assistant responses from another role.

Use `--prompt`, not `--goal`.

Set a 30-minute timeout for `role.py`.

## Online Roles

The user should tell the agent which roles are online, for example:

```text
Online roles: PLAN, DEV, REVIEW, ASK
```

The agent must call only online roles unless the user explicitly authorizes another role.

Role names are hints, not mode guarantees:

- Needs repo/filesystem/tests/git/local MCP: mode 1.
- Needs answer from uploaded responses/reports/logs/files: mode 2.

## Upload Policy

Use `--upload` whenever exact file content matters.

Upload:

- code,
- diffs,
- plans,
- review reports,
- logs,
- screenshots,
- handoffs,
- previous role responses,
- long prompts that should not be pasted directly.

Do not paste large file contents into prompts.

## Output Contract

`role.py` returns one JSON object.

On success:

- read `response_path`,
- do not expect the full answer in the JSON.

On failure:

- keep `request_id`, `error_id`, and `log_path`.

## Retry Rules

If `status` is `completed`, read `response_path` and continue from that content.

If `status` is `failed_retryable`:

- retry the same command,
- prefer `--request-id <request_id>`,
- do not change prompt/uploads unless bad input caused the failure.

Example:

```powershell
uv run role.py --role DEV --request-id req_DEV_20260703010101_abcd1234 --prompt "Work on local repo. Implement the attached plan." --upload "E:\repo\.plan\fix-plan.md"
```

If `status` is `failed_final`, do not retry blindly. Fix obvious input errors if present; otherwise report `request_id`, `error_id`, and `log_path`.

If the process times out before JSON returns, retry the same command and reuse `--request-id` if known.

`role.py` owns browser lag handling, snapshot retries, response recovery by `ROLE_REQUEST_ID`, and `.role_state/responses/` persistence. If browser lag/F5 happens, retry the same request first.

## New Chat Policy

Use `--new-chat` only when the next request does not need the role's current chat history.

Core rule:

- Same logical task still in progress: do not use `--new-chat`.
- Retry, clarification, continuation, or next step of the same task: do not use `--new-chat`.
- New task after the prior task is complete, abandoned, or irrelevant: prefer `--new-chat`.
- Old context still matters: summarize it into uploaded artifacts first, then use `--new-chat`.
- If unsure whether it is the same task, keep the existing chat.

Treat `--new-chat` as resetting that web role to `history = 0`.

### PLAN-Specific Rule

For `PLAN`, do not use `--new-chat` while the same planning cycle is active.

That means no `--new-chat` when:

- refining the same task,
- revising the plan after a `REVIEW` blocker,
- retrying the same planning request,
- answering follow-up questions for the same task.

Use `--new-chat` for `PLAN` only when:

- the old planning task is done,
- the old planning task is abandoned or irrelevant,
- a new unrelated task starts,
- or the needed old context has already been moved into uploaded files and a clean reset is intentional.

Safe uses:

- first request to a fresh role,
- new task after the previous task is done,
- previous role context is irrelevant,
- the role explicitly asks for handoff/reset,
- a complete handoff or summary file is uploaded,
- the previous request was completed or intentionally abandoned with `--new-request`.

Unsafe uses:

- the role is still working on the same logical task,
- a role may still be generating,
- a previous `request_id` is `sent`, `waiting`, or `failed_retryable`,
- needed context exists only in old chat history.

For mode 1, create or reuse a handoff/report file, upload it, then use `--new-chat` only after that handoff contains all needed context.

```powershell
uv run role.py --role DEV --new-chat --prompt "Work on local repo. Continue from the attached handoff. Implement the next step." --upload "E:\repo\.plan\handoff-to-dev.md"
```

For mode 2, prefer uploading the relevant response/report files and asking for a direct answer.

If using `--new-chat` for the same logical request after a retryable failure, include `--request-id` only when recovering the same request and you are sure the prompt was not already sent in the old chat. Otherwise use `--new-request` and upload a handoff.

## Agent Checklist

Before calling a role:

- Is the agent Style A or Style B?
- Is this call mode 1 or mode 2?
- Is the role confirmed online?
- Does the role need exact file content? If yes, use `--upload`.
- For mode 2, is the prompt self-contained and direct-answer?
- For mode 1, should the role write a `.plan/*.md` report?
- Is this a retry of an existing `request_id`?
- Is this still the same task? If yes, do not use `--new-chat`.

After `role.py` returns:

- If completed: read `response_path`.
- If mode 2 returned only a path: call again and require a direct answer.
- If mode 1 returned a report path: pass/upload/use it according to Style A or B.
- If retryable: retry the same request, preferably with `--request-id`.
- If final failure: report `request_id`, `error_id`, and `log_path`.
