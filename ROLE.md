# ROLE.md

This is the operating manual for agents that use online roles through `role.py`. When the user says "read ROLE.md", the agent must read this file, accept the online roles listed by the user, and apply the user's current instruction to choose how roles should be used.

`ROLE.md` does not force one fixed agent persona. The user may ask for a transport-only agent, or for a primary worker that uses roles as subagents. Follow the user's prompt first, then use this file for role-call mechanics, mode selection, retries, uploads, and handoff rules.

## Agent Operating Style

First decide what the current agent is supposed to be in this task.

### Style A: Agent Is Transport/Orchestrator

Use this style when the user wants the agent to behave like an external/manual `main.py`: call roles, pass context, read outputs, and decide the next call.

In this style, the agent may only:

- Run `role.py`.
- Pass explicit files with `--upload`.
- Read the `response_path` file returned by `role.py`.
- Retry the same `role.py` request when retry rules say to retry.
- Report exact `request_id`, `error_id`, and `log_path` when blocked.

In this style, the agent must not:

- Inspect source files to solve the task itself.
- Edit code.
- Review diffs itself.
- Create implementation plans itself.
- Synthesize multi-stream conclusions itself.
- Call MCP/project tools to act on the repo, except to run `role.py` or read the returned `response_path`.
- Paste large file contents into terminal prompts.

### Style B: Agent Is Primary Worker

Use this style when the user wants the current agent to be the main DEV/worker, and roles are only subagents for support such as planning, review, audit, or synthesis.

In this style:

- The current agent may use its normal local tools according to the user's task.
- Roles are helper subagents, not the whole workflow engine.
- Calls to roles usually use mode 2, role works with agent, unless the agent intentionally asks a role to work on local.
- If a helper role returns a path when the agent asked for direct synthesis, the agent should call the role again and ask for the answer directly.
- If a helper role is intentionally asked to work on local and returns a `.md` path, the agent may use that path according to the user's task and normal tool permissions.

`main.py` is closest to Style A. Style B is different: the current agent owns the main work and uses roles as subagents.

## Work Modes

There are only two role-call modes to care about:

1. Role works on local.
2. Role works with agent.

Do not infer the mode from the role name. The same named role, including `DEV`, `REVIEW`, `PLAN`, `AUDIT`, `ASK`, or `MANAGER`, can be used in either mode if the user/task says so.

Both modes may have their own prompt file under `prompts/`. Both modes may technically be able to call MCP. The difference is which MCP/local access is appropriate for the task.

### Mode 1: Role Works On Local

Use this mode when the role should work on the user's machine: repo files, local filesystem, local MCP tools, tests, git state, implementation planning, coding, reviewing, auditing, or local handoff writing.

Rules:

- The role may use local/MCP/repo tools according to its role prompt and the task.
- In Style A, the transport agent still remains transport-only and must not inspect or edit the repo itself. In Style B, the current agent follows its own task permissions; this rule only limits what is delegated to the role.
- The role may write durable `.md` reports under `.plan/` for handoff between roles.
- The role should return exact report paths when the next role needs that report.
- In Style A, the transport agent may pass those report files to the next role with `--upload`. In Style B, the primary agent may read/use the report according to its task and may also upload it to another role.
- In Style A, if the role only returns a path, the next role must receive that file path or uploaded file as context; the transport agent must not reinterpret the local work itself. In Style B, the primary agent may use that path according to the task.
- For long answers, multi-role coordination, implementation reports, audit reports, or review reports, returning a `.md` path is correct in mode 1.
- In Style A, when a mode 1 role returns only a path, pass the path or upload the file to the next role. In Style B, the primary agent may read/use the file if that is part of its task.

Good local workflow:

```powershell
uv run role.py --role PLAN --prompt "Work on local repo. Create an implementation plan. Write it to .plan/plan.md and return the exact path."
uv run role.py --role DEV --prompt "Work on local repo. Implement the attached plan. Write a dev report to .plan/dev-report.md and return the exact path." --upload "E:\repo\.plan\plan.md"
uv run role.py --role REVIEW --prompt "Work on local repo. Review the attached dev report and implementation. Return blockers first; if none, PASS." --upload "E:\repo\.plan\dev-report.md"
```

### Mode 2: Role Works With Agent

Use this mode when the role should answer or synthesize from context supplied by the current agent. Typical use: the agent has several role responses, reports, logs, screenshots, notes, or user-provided files and needs one concise answer.

Rules:

- The role should rely on the prompt and uploaded files supplied by the current agent.
- If any context matters, the agent must provide it with `--upload`.
- Do not send only a repo path and expect the role to read it.
- Do not ask this role to create `.md` reports or handoff files.
- The role must answer directly in its assistant response.
- After `role.py` completes, the agent reads `response_path` and returns/summarizes that content to the user.
- Avoid asking this role to use local MCP/repo tools. A role working with agent can call the wrong local MCP and contaminate the answer with unintended local state.
- If the role returns only a local path such as `.plan/*.md`, that is not a valid mode 2 answer. The agent must call the same role again and require a direct answer in the assistant response.

Good agent-facing call:

```powershell
uv run role.py --role REVIEW --prompt "Synthesize the uploaded DEV and AUDIT responses. Return the final decision directly." --upload "E:\repo\.plan\dev-response.md" --upload "E:\repo\.plan\audit-response.md"
```

Bad agent-facing call:

```powershell
uv run role.py --role REVIEW --prompt "Read E:\repo\.plan\dev-response.md and summarize it."
```

Bad agent-facing output request:

```text
Write your answer to .plan/summary.md and return only the path.
```

Reason: when a role works with agent, the current agent should receive the answer through `response_path`, not through an extra report path.

This restriction applies only to mode 2. In mode 1, returning a `.md` path is often the correct way for local roles to coordinate long work.

Repair if the role answered with only a path:

```powershell
uv run role.py --role REVIEW --prompt "Work with agent. Your previous response returned only this path: E:\repo\.plan\summary.md. That is not usable as the final answer in this mode. Answer directly in this chat response. Do not return a path."
```

## Runtime Contract

Use `role.py` as the only transport command.

Basic call:

```powershell
uv run role.py --role DEV --prompt "Implement the attached plan." --upload "E:\path\to\plan.md"
```

Multiple uploads:

```powershell
uv run role.py --role REVIEW --prompt "Review the attached report and patch. Return blockers first." --upload "E:\repo\.plan\dev-report.md" --upload "E:\repo\patch.diff"
```

No `--goal` is used. Use `--prompt` only.

The agent should set a command timeout of 30 minutes when running `role.py`:

```text
1800 seconds
```

Reason: browser roles can wait on uploads, reload recovery, and long generation. Shorter process timeouts create duplicate requests and dirty role chats.

## Online Roles

The user should tell the agent which roles are online, for example:

```text
Online roles: PLAN, DEV, REVIEW, ASK
```

The agent must route only to online roles unless the user explicitly authorizes opening/using another role.

Role names are hints, not mode guarantees:

| Role | Common use |
| --- | --- |
| ASK | Often works with agent for direct synthesis/Q&A |
| PLAN | Often works on local for plans, but may work with agent for plan critique/synthesis |
| DEV | Often works on local for implementation, but may work with agent for technical Q&A |
| REVIEW | Often works on local for code review, but may work with agent to synthesize multiple reports |
| AUDIT | Often works on local for risk audit, but may work with agent to compare uploaded evidence |
| MANAGER | Often coordinates local-role handoffs, but may work with agent to summarize workflow state |

Before choosing a role, decide the mode first:

- Needs repo/filesystem/tests/git/local MCP: mode 1, role works on local.
- Needs answer from uploaded role responses/reports/logs/files: mode 2, role works with agent.
- Same role name can be used in either mode if the prompt makes the mode explicit.

## Upload Policy

The agent must proactively upload files when file content matters.

Use `--upload <full_path_to_file>` for:

- code files
- diffs/patches
- plans
- review reports
- logs
- screenshots/images
- generated strategy or handoff files
- previous role responses
- long prompts that would be unsafe to paste directly

Do not wait for the user to say "upload" if the task obviously depends on a file.

`--upload` means upload the file to the browser role. The agent does not need to know the browser implementation.

Good:

```powershell
uv run role.py --role DEV --prompt "Work on local repo. Implement this attached plan. Report changed files and checks." --upload "E:\repo\.plan\fix-plan.md"
```

Bad:

```powershell
uv run role.py --role DEV --prompt "<paste 800 lines of plan/code here>"
```

`role.py` itself may spill long rendered prompts into an uploaded `prompt.md`. The agent does not need to manage that.

## Output Contract

`role.py` returns one JSON object.

Success example:

```json
{
  "ok": true,
  "status": "completed",
  "exit_code": 0,
  "request_id": "req_DEV_...",
  "run_id": "run_...",
  "role": "DEV",
  "response_path": ".role_state/responses/req_DEV_....md",
  "uploaded": 1,
  "recovered": false,
  "error": null
}
```

On success, the agent must read `response_path`. Do not expect the full response in JSON.

Failure example:

```json
{
  "ok": false,
  "status": "failed_retryable",
  "exit_code": 3,
  "request_id": "req_DEV_...",
  "run_id": "run_...",
  "error_id": "err_req_DEV_...",
  "role": "DEV",
  "message": "runtime failed for DEV",
  "log_path": ".role_state/logs/err_req_DEV_....log"
}
```

On failure, keep `request_id`, `error_id`, and `log_path` in the report.

## Retry Rules

If `status` is `completed`:

- Read `response_path`.
- Continue based on that response.

If `status` is `failed_retryable`:

- Run the same command again.
- Prefer adding `--request-id <request_id>` from the failed JSON.
- Do not change the prompt or uploads unless the failure is caused by bad input.

Example retry:

```powershell
uv run role.py --role DEV --request-id req_DEV_20260703010101_abcd1234 --prompt "Work on local repo. Implement the attached plan." --upload "E:\repo\.plan\fix-plan.md"
```

If `status` is `failed_final`:

- Do not retry blindly.
- Fix the input if obvious, for example missing upload path.
- Otherwise report the failure with `error_id` and `log_path`.

If the process itself times out before JSON is returned:

- Run the same command again with the same prompt/uploads.
- If you know the previous `request_id`, include `--request-id`.
- `role.py` owns response recovery and should avoid duplicate sends.

## Browser Lag And Stuck UI

The agent must not manage browser progress itself.

`role.py` owns:

- waiting for composer/textarea/buttons after reload
- retrying transient snapshot failures
- recovering old responses by `ROLE_REQUEST_ID`
- reloading once when a response appears active for too long with a stop button
- saving responses to `.role_state/responses/`

If browser lag/F5 happens, do not open a new chat immediately. Retry the same `role.py` request first.

## New Chat Rules

Use `--new-chat` only when it is intentionally safe to lose the current role chat context.

Safe cases:

- First request to a fresh role.
- The role response explicitly asks for a handoff/new chat.
- The agent has a complete handoff/summary file and uploads it with the new request.
- The previous request is completed or intentionally abandoned with `--new-request`.

Unsafe cases:

- A role may still be generating.
- A previous `request_id` is `sent`, `waiting`, or `failed_retryable`.
- The only context exists in the old chat and has not been summarized into an uploaded file.

Correct new-chat handoff flow for mode 1:

1. Ask a role working on local to write a handoff file, or use an existing handoff/report file.
2. Save the handoff under `.plan/` or another explicit path.
3. Call the next role with `--new-chat` and upload the handoff file.

For mode 2, prefer uploading the relevant response/report files directly and asking for a direct answer. Do not ask for a new `.md` handoff unless the next step is a mode 1 local workflow.

Example:

```powershell
uv run role.py --role DEV --new-chat --prompt "Work on local repo. Continue from the attached handoff. Implement the next step." --upload "E:\repo\.plan\handoff-to-dev.md"
```

If using `--new-chat` for the same logical request after a retryable failure, include `--request-id` only when recovering the same request and you are sure the prompt was not already sent in the old chat. Otherwise use `--new-request` and upload a handoff.

## Suggested Workflows

Mode 2 synthesis from multiple role responses:

```powershell
uv run role.py --role REVIEW --prompt "Work with agent. Synthesize the uploaded role responses and return the final decision directly." --upload "E:\repo\.plan\dev-response.md" --upload "E:\repo\.plan\audit-response.md"
```

Mode 1 plan then dev then review:

```powershell
uv run role.py --role PLAN --prompt "Work on local repo. Create a concrete implementation plan. Write it to .plan/plan.md and return the exact path."
```

Read PLAN `response_path`, then pass the plan to DEV:

```powershell
uv run role.py --role DEV --prompt "Work on local repo. Implement the attached plan. Write a dev report to .plan/dev-report.md and return the exact path." --upload "E:\repo\.plan\plan.md"
```

Read DEV `response_path`, then pass the dev report to REVIEW:

```powershell
uv run role.py --role REVIEW --prompt "Work on local repo. Review the attached dev report and current implementation. Return blockers first; if none, PASS." --upload "E:\repo\.plan\dev-report.md"
```

Debug failure:

```powershell
uv run role.py --role DEV --request-id req_DEV_xxx --prompt "Same prompt as before" --upload "E:\same\file.md"
```

## Agent Decision Checklist

Before calling a role:

- Is the current agent Style A transport/orchestrator or Style B primary worker?
- For this role call, is it mode 1, role works on local, or mode 2, role works with agent?
- Does the role need file content? If yes, use `--upload`.
- For mode 2, is the prompt self-contained with uploads and no repo-path dependency?
- For mode 2, have you avoided asking the role to call local MCP/repo tools?
- For mode 1, should the role write a `.plan/*.md` report for the next role?
- Is this a retry of an existing `request_id`?
- Is `--new-chat` safe, and is there a handoff file if context matters?

After `role.py` returns:

- If success: read `response_path`.
- If mode 2: return or summarize the answer content from `response_path`.
- If mode 2 and `response_path` contains only a local path/report path: do not treat it as answered. Call the same role again and require a direct answer in the assistant response.
- If mode 1 and the response contains a report/handoff path: in Style A, pass that path or upload that file to the next local role; in Style B, use it according to the current task.
- If retryable: retry same request, preferably with `--request-id`.
- If final failure: report `request_id`, `error_id`, and `log_path`.

## Minimal Agent System Prompts

Use one of these depending on the user's instruction. These are examples for configuring the current agent; `ROLE.md` itself remains flexible.

### Transport/Orchestrator Agent

Use this when the user says the current agent is transport-only, route-only, or equivalent to a manual external `main.py`:

```text
You are a transport-only role orchestrator. Your job is to call role.py, upload explicit files when useful, read returned response_path files, and route work between online roles. You must classify each call by behavior, not by role name. There are two role-call modes: mode 1, role works on local; mode 2, role works with agent. In mode 1, let the role use its own local/MCP/repo tools and write .plan/*.md reports or handoffs when useful, especially for long answers or multi-role coordination. If a mode 1 role returns a path, pass the path or upload that file to the next local role; do not read/code/review it yourself. In mode 2, provide all needed context with --upload, ask for a direct answer in the role response, do not ask for an extra .md report path, and avoid asking the role to use local MCP/repo tools. If a mode 2 role returns only a local path, call it again and require a direct answer; do not report the path as the answer. You must not inspect source files, edit code, review code, create plans, or synthesize multi-stream conclusions yourself unless the user explicitly changes your role. Use a 30-minute process timeout for role.py. On retryable failure, retry the same request with the same prompt/uploads and request_id. Use --new-chat only after a handoff/summary is available or when starting a truly fresh request. Keep outputs concise and report exact request_id/error_id/log_path when blocked.
```

### Primary Worker With Role Subagents

Use this when the user says the current agent is DEV/main worker and should use PLAN, REVIEW, AUDIT, or other roles as supporting subagents:

```text
You are the primary worker for the user's task. You may use your normal tools according to the user's task and repository rules. Use role.py only to ask online roles for support such as planning, review, audit, synthesis, or a second opinion. Classify each role call by behavior, not by role name. There are two role-call modes: mode 1, role works on local; mode 2, role works with agent. Prefer mode 2 for helper questions where the role should evaluate uploaded context and answer directly. Use mode 1 only when you intentionally want the role to work on local files/MCP/repo state or produce durable .plan/*.md handoffs. If a mode 2 helper returns only a path, call it again and require a direct answer. If a mode 1 helper returns a .md path, use that report according to your task and permissions, or upload it to another role. Do not make helper roles use local MCP when the request is only synthesis from supplied context. Keep role calls scoped, include uploads when context matters, and keep the final answer based on verified work plus role feedback.
```
