# HANDOFF

Use this when a role needs a clean chat before the next phase.

A handoff request needs both parts in the same response:

1. A `HANDOFF:` section with enough state for the next agent.
2. `"command":"handoff"` in the final route JSON.

Minimal route:

```json
{
  "DEV":"Continue from the handoff and implement the next phase.",
  "command":"handoff"
}
```

`command` is metadata. It is not a role.

## Runtime policy

The runtime treats `command: handoff` as a request.

With the default `auto` policy, reset happens only when the response contains `HANDOFF:` and at least one configured threshold is met:

- turn count reaches `--min-turns-before-reset`
- response length reaches `--handoff-response-chars`
- compact state length reaches `--handoff-state-chars`
- `--handoff-every-turns N` matches the current turn

Other policies:

- `--handoff-command-policy always`: reset when `HANDOFF:` exists.
- `--handoff-command-policy off`: ignore handoff commands.

## What to include

Write the handoff as operating notes for the next agent. Keep it specific.

Include:

- original user goal
- current phase
- work already completed
- files, commands, artifacts, links, or logs that matter
- decisions already made and why
- user constraints and style preferences
- open risks, blockers, and uncertain points
- exact next action

Useful shape:

```text
HANDOFF:
Goal: ...
Current state: ...
Done: ...
Important files/artifacts: ...
Decisions: ...
Constraints: ...
Risks/blockers: ...
Next action: ...
```

## Rules

- Do not request handoff without a `HANDOFF:` section.
- Do not use handoff with `FINISH`.
- Any active role may request handoff.
- When MANAGER is active, only MANAGER may dispatch multiple roles in one JSON object.
- Without MANAGER, small flows such as PLAN, DEV, REVIEW may still request handoff normally.
