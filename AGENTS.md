# AGENTS.md

This repository uses the public AGENTS.md convention: a predictable Markdown instruction file at the repository root for coding agents.

## Scope

These instructions apply to the whole repository.

The active orchestrator is `main.py`. Legacy `agents.py` and `solo.py` are not the preferred runtime unless a user explicitly asks for them.

## Dynamic Injection Model

Do not inject every prompt or every skill into every role.

For each logical role, inject only:

1. this file: `AGENTS.md`,
2. `prompts/HANDOFF.md`,
3. the role prompt listed below,
4. the role skill listed below,
5. the current goal/state/handoff from the caller.

If any required loader file is missing or empty, the runtime stops before browser dispatch with a structured `loader_error`. It must not guess, downgrade to goal-only mode, or recursively route to `MANAGER`.

## Role Loader Map

| Role | Prompt | Skill |
| --- | --- | --- |
| MANAGER | `prompts/MANAGER.txt` | `skills/MANAGER.md` |
| PLAN | `prompts/PLAN.txt` | `skills/PLAN.md` |
| DEV | `prompts/DEV.txt` | `skills/DEV.md` |
| REVIEW | `prompts/REVIEW.txt` | `skills/REVIEW.md` |
| AUDIT | `prompts/AUDIT.txt` | `skills/AUDIT.md` |
| A | `prompts/A.txt` | `skills/A.md` |
| B | `prompts/B.txt` | `skills/B.md` |

## Route JSON Contract

Every role response must end with exactly one fenced JSON block.

The JSON block must be a simple route map. Use quoted JSON keys so it is parseable.

```json
{"DEV":"hay implement plan sau: ..."}
```

Rules:

- route keys are role names,
- route values are string messages with no length limit,
- `command` is a reserved metadata key, not a role,
- allowed command values are `none` and `handoff`,
- missing `command` means `none`,
- do not use `target`, `reason`, or `message` wrapper keys,
- one role key means normal handoff,
- multiple role keys mean parallel dispatch,
- when `MANAGER` is active, only `MANAGER` may use multiple role keys,
- `FINISH` must not be combined with role keys or `command`,
- `FINISH` is allowed only for configured finish-authority roles.

Valid route keys: `MANAGER`, `PLAN`, `DEV`, `REVIEW`, `AUDIT`, `A`, `B`, `FINISH`. Reserved metadata key: `command`.

## Caller Rule

When a role is called by another role, it reports back to the caller unless its injected prompt says otherwise.

In manager-owned workflows, all roles report back to `MANAGER`.

## Parallel Dispatch Rule

Only `MANAGER` may return multiple route keys.

Parallel tasks must be independent. Every parallel message must explicitly say that the role should report back to `MANAGER`.

The orchestrator must wait for all called roles before continuing. Composer submission is fail-closed: exact prompt ownership, zero real attachments, and an explicitly enabled send button are checked immediately before each click. The userscript performs one click per command; Python may issue at most one additional CLICK_SEND command when the exact owned prompt remains, for a total retry budget of two submit attempts.

Example:

```json
{
  "DEV":"Continue implementation of X. Report back to MANAGER with changed files and checks.",
  "REVIEW":"Review the current diff independently. Report back to MANAGER with blockers or pass criteria."
}
```

## Physical Role Sessions

The browser/controller enforces one physical tab per role.

If a duplicate role appears:

- the newer tab becomes owner,
- the old tab is displaced to `UNROLE`,
- the old tab should stop acting as that role.

Do not intentionally create duplicate role tabs. Ask `MANAGER` for reset, handoff, or external open when needed.

## Handoff and Reset

All roles can read `prompts/HANDOFF.md`. Use it when context is large, a phase is complete, the next role needs a clean session, or the current chat is drifting.

A role requests reset/new-chat by including both:

1. a `HANDOFF:` section in the response,
2. `"command":"handoff"` in the final route JSON.

The runtime treats this as a request. Default policy is conditional, not absolute: reset happens only when `HANDOFF:` exists and the configured thresholds allow it. A reset is complete only after NEW_CHAT navigation is acknowledged, the tab reloads with a new page-instance generation, reclaims the physical role, and exposes a clean composer at `/`; the physical-role lock remains held through that terminal readiness check.

Example:

```json
{
  "DEV":"Continue from the handoff and implement the next phase.",
  "command":"handoff"
}
```

`command: handoff` is not manager-only. Small flows such as `PLAN,DEV,REVIEW` may use it without `MANAGER`.

## Completion Rule

Only configured finish-authority roles may emit:

```json
{"FINISH":"TASK COMPLETE. Evidence: ..."}
```

Default finish authority is `MANAGER`. If `MANAGER` is not active, runtime selects a fallback finish role from active roles, preferring `REVIEW` when present.

Completion requires enough evidence that the original user goal is satisfied and no blocking issue remains.

## Current Checks

Use checks relevant to the changed area. Known checks:

```powershell
node --check .\tampermonkey.js
node .\tests\test_tampermonkey_contract.mjs
uv run python -m pytest
```
