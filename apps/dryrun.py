from __future__ import annotations

import json


def synthetic_response(
    role: str,
    instruction: str,
    repair: bool = False,
    prompt_roles: list[str] | None = None,
) -> str:
    if "continue working until the goal is fully achieved" in instruction.lower():
        route = {"FINISH": "TASK COMPLETE. Evidence: dry-run goal-only continuation reached finish."}
    elif prompt_roles and "MANAGER" not in prompt_roles and role in prompt_roles:
        role_index = prompt_roles.index(role)
        if role_index + 1 < len(prompt_roles):
            next_role = prompt_roles[role_index + 1]
            route = {next_role: f"Dry-run handoff from {role} to {next_role}."}
        else:
            route = {"FINISH": f"TASK COMPLETE. Evidence: dry-run role {role} completed the configured role chain."}
    elif role == "MANAGER" and "handoff dry run" in instruction.lower():
        route = {"DEV": "Dry-run handoff DEV task.", "command": "handoff"}
    elif role == "MANAGER" and "parallel dry run" in instruction.lower():
        route = {"DEV": "Dry-run parallel DEV task.", "REVIEW": "Dry-run parallel REVIEW task."}
    elif role == "MANAGER" and ("Parallel roles returned" in instruction or "Review passed" in instruction):
        route = {"FINISH": "Dry-run manager approves finish after returned child results."}
    elif role == "MANAGER":
        route = {"PLAN": "Create the first execution plan."}
    elif role == "PLAN":
        route = {"DEV": "Implement the planned work and run self-tests."}
    elif role == "DEV":
        route = {"REVIEW": "Review DEV work and evidence."}
    elif role == "REVIEW":
        route = {"MANAGER": "Review passed in dry-run; manager should decide finish."}
    else:
        route = {"MANAGER": "Dry-run role completed."}
    return "RESULT:\nDry-run result.\n\nHANDOFF:\nDry-run handoff for continuation.\n\n```json\n" + json.dumps(route, ensure_ascii=False, indent=2) + "\n```"
