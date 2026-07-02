from __future__ import annotations

import json


def synthetic_response(
    role: str,
    instruction: str,
    repair: bool = False,
    prompt_roles: list[str] | None = None,
) -> str:
    roles = prompt_roles or []
    manager_active = "MANAGER" in roles
    if "continue working until the goal is fully achieved" in instruction.lower():
        route = {"FINISH": "TASK COMPLETE. Evidence: dry-run goal-only continuation reached finish."}
    elif manager_active and role == "MANAGER" and "handoff dry run" in instruction.lower():
        route = {"DEV": "Dry-run handoff DEV task. Report back to MANAGER.", "command": "handoff"}
    elif manager_active and role == "MANAGER" and "parallel dry run" in instruction.lower():
        route = {
            "DEV": "Dry-run parallel DEV task. Report back to MANAGER.",
            "REVIEW": "Dry-run parallel REVIEW task. Report back to MANAGER.",
        }
    elif manager_active and role != "MANAGER":
        route = {"MANAGER": f"Dry-run {role} result returned to MANAGER."}
    elif manager_active and role == "MANAGER" and (
        "returned to manager" in instruction.lower()
        or "Parallel roles returned" in instruction
        or "Review passed" in instruction
    ):
        route = {"FINISH": "TASK COMPLETE. Evidence: dry-run MANAGER approved returned role result."}
    elif manager_active and role == "MANAGER":
        next_role = next((item for item in roles if item != "MANAGER"), "")
        if next_role:
            route = {next_role: f"Dry-run MANAGER instruction for {next_role}. Report back to MANAGER."}
        else:
            route = {"FINISH": "TASK COMPLETE. Evidence: dry-run MANAGER completed without worker roles."}
    elif roles and "PLAN" in roles and role == "PLAN":
        if "review" in instruction.lower() and ("pass" in instruction.lower() or "finish" in instruction.lower()):
            route = {"FINISH": "TASK COMPLETE. Evidence: dry-run PLAN approved REVIEW result."}
        elif "DEV" in roles:
            route = {"DEV": "Dry-run PLAN instruction for DEV."}
        else:
            route = {"FINISH": "TASK COMPLETE. Evidence: dry-run PLAN completed without DEV role."}
    elif roles and "PLAN" in roles and role == "DEV" and "REVIEW" in roles:
        route = {"REVIEW": "Dry-run DEV result for REVIEW."}
    elif roles and "PLAN" in roles and role == "REVIEW":
        route = {"PLAN": "Dry-run REVIEW passed; PLAN should decide finish."}
    elif roles and role in roles:
        role_index = roles.index(role)
        if role_index + 1 < len(roles):
            next_role = roles[role_index + 1]
            route = {next_role: f"Dry-run handoff from {role} to {next_role}."}
        else:
            route = {"FINISH": f"TASK COMPLETE. Evidence: dry-run role {role} completed the configured role chain."}
    else:
        route = {"FINISH": "TASK COMPLETE. Evidence: dry-run fallback completed."}
    return "RESULT:\nDry-run result.\n\nHANDOFF:\nDry-run handoff for continuation.\n\n```json\n" + json.dumps(route, ensure_ascii=False, indent=2) + "\n```"
