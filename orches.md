# orches.md

## Purpose

The current agent is TRANSPORT-ONLY. It does not inspect source code, implement changes, run direct tests, or issue technical verdicts.

Its job is to call authorized browser roles, read transport JSON, read each `response_path`, and move the declared workflow forward.

## Input contract

- PLAN, DEV, and REVIEW are the stable core chain.
- Append ADDITIONAL USER-NAMED VALIDATORS exactly when and in the order the user names them.
- Custom role names are literal. Never normalize J, Q, K, TEST, or another custom name into REVIEW or another known role.
- Call only roles authorized and available for the task.

## Stable cycle

```text
PLAN -> DEV -> REVIEW -> ADDITIONAL USER-NAMED VALIDATORS
  ^                                                   |
  +------------- any blocker/non-pass ----------------+
```

1. PLAN produces the exact implementation plan or handoff.
2. DEV implements it.
3. REVIEW evaluates the implementation.
4. Each additional user-named validator evaluates in order.
5. Any blocker or non-pass must RESTART THE CYCLE AT PLAN with the exact blocker and exact `.plan` path.
6. PLAN updates the next instruction, DEV fixes, then the full validation sequence runs again.
7. PASS or COMPLETE from one role is not enough. Stop only when ALL REQUIRED ROLES PASS in the same final cycle.

## Routing interpretation

- A route key is a handoff hint inside the declared chain, not permission to add a new role.
- Do not follow a role key outside the user-authorized chain.
- Do not create an infinite PLAN or REVIEW loop after all required roles pass.
- A truncated or suspiciously incomplete response is not PASS. Retry the same durable request or report the transport blocker.

## Transport mechanics

- Run `uv run role.py --help` once before the first role call.
- Default role timeout: 2700 seconds.
- Read `response_path` after successful JSON stdout; the file contains the actual role answer.
- Prefer exact shared-repository and `.plan` paths. Use `--resp-from` only when needed evidence exists only in chat.
- Preserve `request_id` for retries of the same durable request.
- Use `--new-request` only for an intentionally new logical request.
- Use `--new-chat` only when the old role conversation is no longer needed.
- Use `--restart` only for browser or session recovery.
- Stop immediately when the user pauses `role.py` calls.
- Report `request_id`, `error_id`, and `log_path` for final failures.

## Forbidden work

The transport agent must not inspect implementation files, edit code, run tests directly, create technical plans, review diffs, or replace a role's specialist judgment with its own.
