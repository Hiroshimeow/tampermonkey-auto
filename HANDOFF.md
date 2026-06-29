# HANDOFF.md

Use this guide when a role needs context reset or a new chat before the next phase.

## How A Role Requests Handoff

A role requests handoff by doing both things in the same response:

1. include a `HANDOFF:` section with enough state to continue safely,
2. include `"command":"handoff"` in the final route JSON.

Minimal JSON example:

```json
{
  "DEV":"Continue from the handoff and implement the next phase.",
  "command":"handoff"
}
```

Full response example:

````text
RESULT:
Phase 1 is complete. The next role needs a clean session because the current context is large.

HANDOFF:
Goal: Build the requested workflow.
Current phase: DEV implemented the route parser and command policy.
Files touched: main.py, AGENTS.md, HANDOFF.md.
Evidence: main.py self-test passed.
Remaining work: REVIEW should check edge cases around command=handoff and non-manager flows.
Risks: legacy tests may still expect target/reason/message.
Next instruction: Continue from this handoff and review the implementation.

```json
{
  "REVIEW":"Continue from HANDOFF and review the handoff command implementation.",
  "command":"handoff"
}
```
````

`command` is metadata, not a route role.

## Runtime Policy

The runtime treats `command: handoff` as a request.

Default policy is `auto`: reset happens only when the response contains `HANDOFF:` and at least one reset condition is met:

- turn count is at or above `--min-turns-before-reset`,
- response length is at or above `--handoff-response-chars`,
- compact state length is at or above `--handoff-state-chars`,
- `--handoff-every-turns N` divides the current turn.

Other policies:

- `--handoff-command-policy always`: reset on request if `HANDOFF:` exists.
- `--handoff-command-policy off`: ignore handoff commands.

## Good Handoff Content

A useful handoff includes:

- original goal,
- current phase,
- files touched or inspected,
- decisions made,
- evidence gathered,
- remaining work,
- known risks or blockers,
- exact next instruction for the receiving role.

## Rules

- Do not request handoff without a `HANDOFF:` section.
- Do not use handoff with `FINISH`.
- Handoff can be requested by any active role; it is not manager-only.
- When `MANAGER` is active, only `MANAGER` may dispatch multiple roles in one JSON object.
- Without `MANAGER`, small flows such as `PLAN,DEV,REVIEW` may still request handoff normally.