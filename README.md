# Tampermonkey Auto Role Runner

This repo automates ChatGPT browser roles through the MAuto bridge and a Tampermonkey userscript.

It has two common entrypoints:

- `main.py`: multi-role coordination with route JSON.
- `role.py`: send exactly one prompt to exactly one browser role and return one machine-readable JSON object.

## Prerequisites

1. Start the MAuto bridge/server.
2. Install or update `tampermonkey.js` in the browser.
3. Open one ChatGPT tab per browser role you want to use.
4. Make sure each tab is registered with the expected role name, for example `DEV`, `REVIEW`, `PLAN`, or `MANAGER`.

Default bridge URL:

```text
http://127.0.0.1:8500
```

Override it with `--base-url` or `MAUTO_BASE_URL`.

## Main Multi-Role Flow

Standard 3-role command:

```powershell
uv run python main.py --role DEV,REVIEW,PLAN --goal "your task here"
```

Rules:

- `--role` is required.
- The first role in `--role` is the start role.
- `--role` is also used as the default prompt role list and browser role list.
- Default finish authority is the highest-precedence role in the list:

```text
custom role < DEV < REVIEW < PLAN < MANAGER
```

Examples:

```powershell
uv run python main.py --role DEV --goal "single role task"
uv run python main.py --role DEV,REVIEW,PLAN --goal "implement and review this change"
uv run python main.py --role DEV,MANAGER,REVIEW --goal "manager-controlled workflow"
```

If `MANAGER` is present, manager mode is active:

- Non-manager roles must route back to `MANAGER`.
- `MANAGER` coordinates the next target role.
- `MANAGER` has finish authority by default.

Defaults:

```text
max-turns    = 0      # unlimited until FINISH or unrecoverable no-route
reload-after = 10.0   # reload previous browser role after routing to another role
resume       = off
```

Disable auto reload:

```powershell
uv run python main.py --role DEV,REVIEW,PLAN --reload-after 0 --goal "your task here"
```

Set a debug turn limit:

```powershell
uv run python main.py --role DEV,REVIEW,PLAN --max-turns 20 --goal "your task here"
```

## Resume

Use `--resume` only when the current browser tab already has a response you want the runner to route from:

```powershell
uv run python main.py --role DEV,REVIEW,PLAN --resume --goal "your task here"
```

`--resume` reads the existing browser response only on the first dispatched turn. Later turns do not reuse old responses.

If manager mode is active and the resumed route JSON does not route to `MANAGER`, the runner sends a repair prompt asking the role to route to `MANAGER`.

## Route JSON Contract

Every role response in `main.py` must end with exactly one fenced JSON object and nothing after it.

Shape:

```json
{
  "ROLE_NAME": "self-contained message for that role"
}
```

Finish shape:

```json
{
  "FINISH": "final result summary"
}
```

Rules:

- Route keys must be valid logical roles from `--role`, or `FINISH`.
- A role may route to one target role or multiple target roles.
- `FINISH` is accepted only from a finish-authorized role.
- If `MANAGER` is active, non-manager roles must route only to `MANAGER`.
- Route messages must be self-contained because browser roles do not share chat history.

## Workflow Memory Rule

Roles share the same machine and repo, but not the same browser history. Use `.plan/` as durable workflow memory.

Rules for role prompts and routed messages:

- If a role creates an important plan, implementation report, review report, or handoff, write it to an exact file under `.plan/`.
- Route messages should name the exact file the next role must read.
- The next role must read only the named file or files.
- Do not scan `.plan/` looking for the latest file.
- Do not infer a file name that was not explicitly routed.

Example route message:

```json
{
  "DEV": "Read .plan/turn_1_plan_for_task_a.md only, then implement the listed steps. Write your report to .plan/turn_2_dev_for_review_task_a.md and route to REVIEW."
}
```

## `role.py`: Single-Role Command For External Agents

`role.py` is the minimal entrypoint for external orchestrators such as opencode, OpenClaw, Hermes, or Codex.

It sends one prompt to one browser role, waits for the assistant response to finish, and prints exactly one JSON object to stdout.

Commands:

```powershell
uv run python role.py --role PLAN --prompt "review code changes in E:\python_project\Screens-Trans-Chatbot"
uv run python role.py --role REVIEW --resp-from DEV --prompt "Review the latest DEV response."
Get-Content .plan\prompt.txt | uv run python role.py --role PLAN
```

`--resp-from ROLE` reads up to the 3 latest assistant responses from that source role and prefixes them to the prompt before sending it to the target role.

If `--resp-from` is omitted, `role.py` sends only `--prompt` or stdin.

Stdout contract:

```json
{"ok":true,"exit_code":0,"summary":"short preview","data":{"role":"PLAN","resp_from":null,"source_response_count":0,"response":"full assistant response"},"error":null}
```

Failure contract:

```json
{"ok":false,"exit_code":2,"summary":"missing prompt","data":null,"error":{"type":"Error","message":"--prompt or stdin prompt text is required"}}
```

Notes:

- stdout is exactly one JSON object.
- `data.response` is the full assistant response.
- `summary` is a short preview for logs.
- JSON uses `ensure_ascii=True` to avoid Windows codepage failures when responses contain Unicode.
- Bridge and recovery logs go to stderr.

Exit codes:

```text
0 = completed with a non-empty response
2 = missing input
3 = bridge/runtime failure or empty response
4 = manual input is pending in the browser composer
```

## External Agent Hard Rules

Use this guide when an external agent is asked to call a browser role through `role.py`.

Hard rules:

1. Run exactly one requested `uv run python role.py ...` command.
2. Do not perform any other action, no matter how small.
3. Do not read the repo.
4. Do not edit files.
5. Do not run tests.
6. Do not run `git status`, `git diff`, `git log`, or any other inspection command.
7. Do not infer or execute a next step.
8. Wait only for the `role.py` process to finish.
9. Parse stdout as one JSON object.
10. If `ok=true` and exit code is `0`, return `data.response` as the result.
11. If `ok=false` or the exit code is not `0`, report `summary`, `error.type`, `error.message`, and the exit code.
12. Do not retry unless explicitly instructed.
13. Treat stderr as runtime logs only. Do not mix stderr into the main result unless reporting an operational failure.

Copyable instruction for external agents:

```text
Run only this command and wait for it to finish:

uv run python role.py --role <ROLE> --prompt "<PROMPT>"

Do not read files, do not inspect the repo, do not edit anything, do not run tests, and do not run git commands.
Parse stdout as one JSON object. If ok=true and exit_code=0, return data.response. If it fails, report summary, error.type, error.message, and exit_code. Do not retry unless explicitly asked.
```

## Prompt Files

Built-in role prompts live in `prompts/`:

```text
prompts/MANAGER.txt
prompts/PLAN.txt
prompts/DEV.txt
prompts/REVIEW.txt
```

Custom roles may use exact prompt files such as `prompts/DEV2.txt`. If no exact prompt exists, the runner may fall back to a known role type such as `DEV`, depending on the role name.

If no prompt is available for a role, the runner still works in goal-only mode, but the role must be given self-contained instructions.

## Advanced Overrides

Normal usage should not need these flags:

```text
--prompt-roles      logical roles allowed in route JSON
--browser-roles     physical browser roles/tabs to call
--role-map          map logical roles to physical browser roles
--finish-roles      override finish-authorized roles
--parallelism       max parallel target dispatches
--preflight         test browser commands before running
```

Example with logical roles mapped to fewer browser tabs:

```powershell
uv run python main.py `
  --role DEV,REVIEW,PLAN `
  --prompt-roles DEV,REVIEW,PLAN `
  --browser-roles DEV,REVIEW `
  --role-map PLAN=REVIEW DEV=DEV REVIEW=REVIEW `
  --finish-roles REVIEW `
  --goal "your task here"
```

## Verification

Run the focused tests:

```powershell
uv run pytest tests/test_role_cli.py tests/test_main_flow.py -q
```

Run the full Python test suite:

```powershell
uv run pytest -q
```

Check the userscript contract:

```powershell
node --check tampermonkey.js
node tests/test_tampermonkey_contract.mjs
```
