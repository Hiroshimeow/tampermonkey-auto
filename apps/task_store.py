from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from dateutil import tz as dateutil_tz


TASK_VERSION = 1
DEFAULT_TASK_PATH = Path(__file__).resolve().parents[1] / ".role_state" / "tasks.json"
TASK_STATES = ("BACKLOG", "READY", "RUNNING", "REVIEW", "BLOCKED", "DONE")
RESERVING_WAKE_STATES = {
    "CLAIMED",
    "ISSUING_SET_PROMPT",
    "SET_PROMPT_PENDING",
    "ISSUING_CLICK_SEND",
    "CLICK_SEND_PENDING",
    "SENT",
    "UNCERTAIN",
}
WAKE_STATES = RESERVING_WAKE_STATES | {"IDLE", "DEFERRED"}
ALLOWED_TRANSITIONS = {
    "BACKLOG": {"READY", "BLOCKED"},
    "READY": {"BACKLOG", "RUNNING", "BLOCKED"},
    "RUNNING": {"REVIEW", "BLOCKED", "DONE"},
    "REVIEW": {"RUNNING", "BLOCKED", "DONE"},
    "BLOCKED": {"BACKLOG", "READY", "RUNNING", "REVIEW", "DONE"},
    "DONE": {"BACKLOG", "READY"},
}
EXECUTION_FIELDS = {"active_request_id", "last_request_id", "last_result_status", "last_result_summary", "blocker"}
REASSIGNMENT_FIELDS = {"controller_role", "logical_roles", "physical_role_map", "finish_roles", "schedule", "execution_options"}
EXECUTION_OPTION_DEFAULTS = {
    "timeout": 1800.0,
    "request_timeout": 1200.0,
    "parallelism": 4,
    "max_turns": 0,
    "reload_after": 10.0,
    "new_chat_on_handoff": False,
    "handoff_command_policy": "auto",
}
_WAKE_FIELD_UNSET = object()


class TaskStoreMutationError(RuntimeError):
    pass


class TaskConflictError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit UTC offset")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def normalize_role(value: Any, *, field: str = "role") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    role = value.strip().upper()
    if not role:
        raise ValueError(f"{field} must not be empty")
    if len(role) > 80:
        raise ValueError(f"{field} exceeds 80 characters")
    if any(ord(char) < 32 for char in role):
        raise ValueError(f"{field} contains control characters")
    return role


def bounded_text(value: Any, *, field: str, limit: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not allow_empty and not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return text


def default_execution_options() -> dict[str, Any]:
    return copy.deepcopy(EXECUTION_OPTION_DEFAULTS)


def normalize_execution_options(raw: Any) -> dict[str, Any]:
    if raw is None or raw == {}:
        return default_execution_options()
    if not isinstance(raw, dict):
        raise ValueError("execution_options must be an object")
    unknown = set(raw) - set(EXECUTION_OPTION_DEFAULTS)
    if unknown:
        raise ValueError(f"unsupported execution_options fields: {', '.join(sorted(unknown))}")
    options = {**EXECUTION_OPTION_DEFAULTS, **raw}

    def finite_number(name: str, *, minimum: float, maximum: float) -> float:
        value = options[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"execution_options.{name} must be a finite number")
        value = float(value)
        if value < minimum or value > maximum:
            raise ValueError(f"execution_options.{name} must be between {minimum:g} and {maximum:g}")
        return value

    timeout = finite_number("timeout", minimum=1, maximum=86400)
    request_timeout = finite_number("request_timeout", minimum=1, maximum=86400)
    reload_after = finite_number("reload_after", minimum=0, maximum=3600)
    for name, minimum, maximum in (("parallelism", 1, 32), ("max_turns", 0, 100000)):
        value = options[name]
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ValueError(f"execution_options.{name} must be a whole number between {minimum} and {maximum}")
    if not isinstance(options["new_chat_on_handoff"], bool):
        raise ValueError("execution_options.new_chat_on_handoff must be a boolean")
    policy = bounded_text(
        options["handoff_command_policy"],
        field="execution_options.handoff_command_policy",
        limit=16,
        allow_empty=False,
    ).lower()
    if policy not in {"auto", "always"}:
        raise ValueError("execution_options.handoff_command_policy must be auto or always")
    return {
        "timeout": timeout,
        "request_timeout": request_timeout,
        "parallelism": options["parallelism"],
        "max_turns": options["max_turns"],
        "reload_after": reload_after,
        "new_chat_on_handoff": options["new_chat_on_handoff"],
        "handoff_command_policy": policy,
    }


def load_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        fallback = dateutil_tz.gettz(name)
        if fallback is None:
            raise ValueError(f"invalid schedule timezone: {name}")
        return fallback


def normalize_schedule(raw: Any, now: datetime | None = None) -> tuple[dict[str, Any], str | None]:
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")
    now = (now or utc_now()).astimezone(timezone.utc)
    kind = bounded_text(raw.get("kind", ""), field="schedule.kind", limit=16, allow_empty=False).lower()
    if kind == "manual":
        if set(raw) - {"kind"}:
            raise ValueError("manual schedule accepts only kind")
        return {"kind": "manual"}, None
    if kind == "once":
        if set(raw) - {"kind", "run_at"}:
            raise ValueError("once schedule has unsupported fields")
        run_at = iso_utc(parse_timestamp(raw.get("run_at"), field="schedule.run_at"))
        return {"kind": "once", "run_at": run_at}, run_at
    if kind == "interval":
        if set(raw) - {"kind", "minutes", "start_at"}:
            raise ValueError("interval schedule has unsupported fields")
        minutes = raw.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            raise ValueError("schedule.minutes must be a whole number >= 1")
        start = parse_timestamp(raw["start_at"], field="schedule.start_at") if raw.get("start_at") else now
        start_at = iso_utc(start)
        return {"kind": "interval", "minutes": minutes, "start_at": start_at}, start_at
    if kind == "cron":
        if set(raw) - {"kind", "expression", "timezone"}:
            raise ValueError("cron schedule has unsupported fields")
        expression = bounded_text(raw.get("expression", ""), field="schedule.expression", limit=128, allow_empty=False)
        if len(expression.split()) != 5:
            raise ValueError("cron expression must contain exactly five fields")
        timezone_name = bounded_text(raw.get("timezone", ""), field="schedule.timezone", limit=128, allow_empty=False)
        zone = load_timezone(timezone_name)
        if not croniter.is_valid(expression):
            raise ValueError("invalid five-field cron expression")
        local_now = now.astimezone(zone)
        next_local = croniter(expression, local_now).get_next(datetime)
        return {"kind": "cron", "expression": expression, "timezone": timezone_name}, iso_utc(next_local)
    raise ValueError("schedule.kind must be manual, once, interval, or cron")


def next_after_success(schedule: dict[str, Any], now: datetime) -> tuple[bool, str | None]:
    kind = schedule["kind"]
    now = now.astimezone(timezone.utc)
    if kind == "manual":
        return True, None
    if kind == "once":
        return False, None
    if kind == "interval":
        start = parse_timestamp(schedule["start_at"], field="schedule.start_at")
        step = timedelta(minutes=schedule["minutes"])
        if start > now:
            return True, iso_utc(start)
        elapsed = now - start
        jumps = int(elapsed.total_seconds() // step.total_seconds()) + 1
        return True, iso_utc(start + step * jumps)
    if kind == "cron":
        zone = load_timezone(schedule["timezone"])
        next_local = croniter(schedule["expression"], now.astimezone(zone)).get_next(datetime)
        return True, iso_utc(next_local)
    raise ValueError(f"unsupported schedule kind: {kind}")


def empty_wake() -> dict[str, Any]:
    return {
        "state": "IDLE",
        "attempt_id": "",
        "server_instance_id": "",
        "source": "",
        "scheduled_for": None,
        "retry_at": None,
        "command_id": None,
        "command_action": None,
        "requested_at": None,
        "sent_at": None,
        "error": "",
    }


def empty_document() -> dict[str, Any]:
    return {"version": TASK_VERSION, "revision": 0, "updated_at": "", "tasks": {}}


def task_reserves_controller(task: dict[str, Any]) -> bool:
    return bool(
        task.get("status") in {"RUNNING", "REVIEW"}
        or task.get("active_request_id")
        or (task.get("wake") or {}).get("state") in RESERVING_WAKE_STATES
    )


def require_keys(raw: dict[str, Any], allowed: set[str], required: set[str], *, subject: str) -> None:
    extra = set(raw) - allowed
    missing = required - set(raw)
    if extra:
        raise ValueError(f"{subject} has unsupported fields: {', '.join(sorted(extra))}")
    if missing:
        raise ValueError(f"{subject} is missing fields: {', '.join(sorted(missing))}")


def validate_event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("task event must be an object")
    event_keys = {"event_id", "timestamp", "type", "summary", "actor_role", "request_id", "command_id"}
    require_keys(raw, event_keys, event_keys, subject="task event")
    event = {
        "event_id": bounded_text(raw.get("event_id", ""), field="event.event_id", limit=80, allow_empty=False),
        "timestamp": iso_utc(parse_timestamp(raw.get("timestamp"), field="event.timestamp")),
        "type": bounded_text(raw.get("type", ""), field="event.type", limit=80, allow_empty=False),
        "summary": bounded_text(raw.get("summary", ""), field="event.summary", limit=500),
        "actor_role": bounded_text(raw.get("actor_role", ""), field="event.actor_role", limit=80),
        "request_id": bounded_text(raw.get("request_id", ""), field="event.request_id", limit=160),
        "command_id": bounded_text(raw.get("command_id", ""), field="event.command_id", limit=160),
    }
    return event


def validate_wake(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("wake must be an object")
    wake_keys = {
        "state", "attempt_id", "server_instance_id", "source", "scheduled_for", "retry_at",
        "command_id", "command_action", "requested_at", "sent_at", "error",
    }
    require_keys(raw, wake_keys, wake_keys, subject="wake")
    state = bounded_text(raw.get("state", ""), field="wake.state", limit=40, allow_empty=False).upper()
    if state not in WAKE_STATES:
        raise ValueError(f"unsupported wake state: {state}")
    result = empty_wake()
    result["state"] = state
    for key, limit in (("attempt_id", 160), ("server_instance_id", 160), ("source", 32), ("error", 500)):
        result[key] = bounded_text(raw.get(key, ""), field=f"wake.{key}", limit=limit)
    for key in ("scheduled_for", "retry_at", "requested_at", "sent_at"):
        value = raw.get(key)
        result[key] = iso_utc(parse_timestamp(value, field=f"wake.{key}")) if value else None
    command_id = raw.get("command_id")
    result["command_id"] = bounded_text(command_id, field="wake.command_id", limit=160) if command_id else None
    command_action = raw.get("command_action")
    result["command_action"] = bounded_text(command_action, field="wake.command_action", limit=40).upper() if command_action else None
    return result


def validate_task(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("task must be an object")
    task_keys = {
        "task_id", "revision", "title", "target_root", "branch", "prompt", "skill_path",
        "controller_role", "logical_roles", "physical_role_map", "finish_roles", "status", "enabled",
        "schedule", "execution_options", "next_run_at", "active_request_id", "last_request_id", "last_result_status",
        "last_result_summary", "blocker", "wake", "events", "created_at", "updated_at", "archived_at",
        "transport", "external_target",
    }
    require_keys(raw, task_keys, task_keys, subject="task")
    task_id = bounded_text(raw.get("task_id", ""), field="task_id", limit=80, allow_empty=False)
    if re.fullmatch(r"task-[0-9a-f]{32}", task_id) is None:
        raise ValueError("task_id must be a server-generated opaque task UUID")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("task revision must be a positive integer")
    logical_roles = raw.get("logical_roles")
    if not isinstance(logical_roles, list) or not logical_roles:
        raise ValueError("logical_roles must be a non-empty array")
    normalized_logical = [normalize_role(role, field="logical_roles") for role in logical_roles]
    if len(set(normalized_logical)) != len(normalized_logical):
        raise ValueError("logical_roles must be unique")
    role_map = raw.get("physical_role_map")
    if not isinstance(role_map, dict):
        raise ValueError("physical_role_map must be an object")
    normalized_map = {normalize_role(key, field="physical_role_map key"): normalize_role(value, field="physical_role_map value") for key, value in role_map.items()}
    if set(normalized_map) != set(normalized_logical):
        raise ValueError("physical_role_map keys must exactly match logical_roles")
    finish_roles = raw.get("finish_roles")
    if not isinstance(finish_roles, list) or not finish_roles:
        raise ValueError("finish_roles must be a non-empty array")
    normalized_finish = [normalize_role(role, field="finish_roles") for role in finish_roles]
    if len(set(normalized_finish)) != len(normalized_finish):
        raise ValueError("finish_roles must be unique")
    if not set(normalized_finish).issubset(normalized_logical):
        raise ValueError("finish_roles must be a subset of logical_roles")
    status = bounded_text(raw.get("status", ""), field="status", limit=16, allow_empty=False).upper()
    if status not in TASK_STATES:
        raise ValueError(f"status must be one of {', '.join(TASK_STATES)}")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    schedule, _ = normalize_schedule(raw.get("schedule"), utc_now())
    next_run_at = raw.get("next_run_at")
    normalized_next = iso_utc(parse_timestamp(next_run_at, field="next_run_at")) if next_run_at else None
    active_request_id = raw.get("active_request_id")
    last_request_id = raw.get("last_request_id")
    archived_at = raw.get("archived_at")
    events = raw.get("events")
    if not isinstance(events, list) or len(events) > 50:
        raise ValueError("events must be an array with at most 50 entries")
    result = {
        "task_id": task_id,
        "revision": revision,
        "title": bounded_text(raw.get("title", ""), field="title", limit=160, allow_empty=False),
        "target_root": bounded_text(raw.get("target_root", ""), field="target_root", limit=500, allow_empty=False),
        "branch": bounded_text(raw.get("branch", ""), field="branch", limit=200, allow_empty=False),
        "prompt": bounded_text(raw.get("prompt", ""), field="prompt", limit=12000, allow_empty=False),
        "skill_path": bounded_text(raw.get("skill_path", ""), field="skill_path", limit=200, allow_empty=False),
        "controller_role": normalize_role(raw.get("controller_role"), field="controller_role"),
        "logical_roles": normalized_logical,
        "physical_role_map": {role: normalized_map[role] for role in normalized_logical},
        "finish_roles": normalized_finish,
        "status": status,
        "enabled": enabled,
        "schedule": schedule,
        "execution_options": normalize_execution_options(raw.get("execution_options")),
        "next_run_at": normalized_next,
        "active_request_id": bounded_text(active_request_id, field="active_request_id", limit=160) if active_request_id else None,
        "last_request_id": bounded_text(last_request_id, field="last_request_id", limit=160) if last_request_id else None,
        "last_result_status": bounded_text(raw.get("last_result_status") or "", field="last_result_status", limit=80) or None,
        "last_result_summary": bounded_text(raw.get("last_result_summary", ""), field="last_result_summary", limit=2000),
        "blocker": bounded_text(raw.get("blocker", ""), field="blocker", limit=2000),
        "wake": validate_wake(raw.get("wake")),
        "events": [validate_event(event) for event in events][-50:],
        "created_at": iso_utc(parse_timestamp(raw.get("created_at"), field="created_at")),
        "updated_at": iso_utc(parse_timestamp(raw.get("updated_at"), field="updated_at")),
        "archived_at": iso_utc(parse_timestamp(archived_at, field="archived_at")) if archived_at else None,
        "transport": bounded_text(raw.get("transport", "userscript"), field="transport", limit=40, allow_empty=False),
        "external_target": raw.get("external_target"),
    }
    if result["skill_path"] != "skills/ORCHESTRATOR.md":
        raise ValueError("skill_path must be exactly skills/ORCHESTRATOR.md")
    if result["transport"] != "userscript":
        raise ValueError("transport must be userscript in Phase 05")
    if result["external_target"] is not None:
        raise ValueError("external_target must be null in Phase 05")
    if result["archived_at"] and result["status"] != "DONE":
        raise ValueError("only DONE tasks may be archived")
    return result


def validate_document(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("top-level task document must be an object")
    document_keys = {"version", "revision", "updated_at", "tasks"}
    require_keys(raw, document_keys, document_keys, subject="task document")
    if raw.get("version") != TASK_VERSION:
        raise ValueError(f"unsupported task version: {raw.get('version')!r}")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("document revision must be a non-negative integer")
    updated_at = raw.get("updated_at")
    normalized_updated = iso_utc(parse_timestamp(updated_at, field="updated_at")) if updated_at else ""
    tasks = raw.get("tasks")
    if not isinstance(tasks, dict):
        raise ValueError("tasks must be an object")
    normalized_tasks: dict[str, Any] = {}
    reserved_controllers: dict[str, str] = {}
    for key, value in tasks.items():
        migrated = copy.deepcopy(value) if isinstance(value, dict) else value
        if isinstance(migrated, dict):
            migrated.setdefault("execution_options", default_execution_options())
        task = validate_task(migrated)
        if key != task["task_id"]:
            raise ValueError("task map key must match task_id")
        if not task.get("archived_at") and task_reserves_controller(task):
            prior = reserved_controllers.get(task["controller_role"])
            if prior is not None:
                raise ValueError(f"controller {task['controller_role']} is reserved by both {prior} and {task['task_id']}")
            reserved_controllers[task["controller_role"]] = task["task_id"]
        normalized_tasks[key] = task
    return {"version": TASK_VERSION, "revision": revision, "updated_at": normalized_updated, "tasks": normalized_tasks}


class TaskStore:
    def __init__(self, path: str | Path = DEFAULT_TASK_PATH, *, clock: Callable[[], datetime] = utc_now):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.RLock()
        self._document = empty_document()
        self.load_error: dict[str, str] | None = None
        self._load()

    @property
    def document(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self._document)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _load(self) -> None:
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeError) as exc:
            self._set_load_error(exc)
            return
        try:
            self._document = validate_document(json.loads(raw_text))
        except (json.JSONDecodeError, ValueError, UnicodeError) as exc:
            self._set_load_error(exc)

    def _set_load_error(self, exc: Exception) -> None:
        self.load_error = {"code": "invalid_task_file", "message": str(exc)[:500]}
        self._document = empty_document()

    def _ensure_mutable(self) -> None:
        if self.load_error is not None:
            raise TaskStoreMutationError(f"task mutation blocked by load_error: {self.load_error['message']}")

    def read(self, *, include_archived: bool = False) -> dict[str, Any]:
        with self.lock:
            tasks = self._sorted_tasks(self._document["tasks"].values(), include_archived=include_archived)
            return {
                "version": self._document["version"],
                "revision": self._document["revision"],
                "updated_at": self._document["updated_at"],
                "load_error": copy.deepcopy(self.load_error),
                "tasks": copy.deepcopy(tasks),
            }

    def list_tasks(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        return self.read(include_archived=include_archived)["tasks"]

    def _sorted_tasks(self, values, *, include_archived: bool) -> list[dict[str, Any]]:
        order = {state: index for index, state in enumerate(TASK_STATES)}
        tasks = [task for task in values if include_archived or not task.get("archived_at")]
        return sorted(
            tasks,
            key=lambda task: (
                order[task["status"]],
                parse_timestamp(task["updated_at"], field="updated_at"),
                task["title"].lower(),
                task["task_id"],
            ),
        )

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self._document["tasks"].get(str(task_id or "").strip())
            return copy.deepcopy(task) if task else None

    def _event(self, event_type: str, summary: str, *, actor_role: str = "", request_id: str = "", command_id: str = "") -> dict[str, Any]:
        return {
            "event_id": f"event-{uuid.uuid4().hex}",
            "timestamp": iso_utc(self._now()),
            "type": event_type,
            "summary": bounded_text(summary, field="event.summary", limit=500),
            "actor_role": normalize_role(actor_role) if actor_role else "",
            "request_id": bounded_text(request_id, field="event.request_id", limit=160),
            "command_id": bounded_text(command_id, field="event.command_id", limit=160),
        }

    def _append_event(self, task: dict[str, Any], event: dict[str, Any]) -> None:
        task["events"] = (task.get("events", []) + [event])[-50:]

    def _controller_conflict(self, candidate: dict[str, Any], *, exclude_task_id: str = "") -> dict[str, Any] | None:
        controller = candidate["controller_role"]
        for task in self._document["tasks"].values():
            if task["task_id"] == exclude_task_id or task.get("archived_at"):
                continue
            if task["controller_role"] == controller and task_reserves_controller(task):
                return task
        return None

    def create(self, raw: dict[str, Any], *, manual_wake_server_instance_id: str = "") -> dict[str, Any]:
        with self.lock:
            self._ensure_mutable()
            if not isinstance(raw, dict):
                raise ValueError("task create body must be an object")
            forbidden = {"task_id", "revision", "created_at", "updated_at", "archived_at", "wake", "events", "active_request_id", "last_request_id"}
            if forbidden.intersection(raw):
                raise ValueError("create body contains server-owned task fields")
            now = self._now()
            schedule, next_run_at = normalize_schedule(raw.get("schedule", {"kind": "manual"}), now)
            wake = empty_wake()
            if manual_wake_server_instance_id:
                if str(raw.get("status", "BACKLOG")).strip().upper() != "READY" or schedule["kind"] != "manual":
                    raise ValueError("manual wake creation requires a READY manual task")
                wake.update({
                    "state": "CLAIMED",
                    "attempt_id": f"attempt-{uuid.uuid4().hex}",
                    "server_instance_id": manual_wake_server_instance_id,
                    "source": "manual",
                    "requested_at": iso_utc(now),
                })
            task_id = f"task-{uuid.uuid4().hex}"
            task = validate_task({
                "task_id": task_id,
                "revision": 1,
                "title": raw.get("title", ""),
                "target_root": raw.get("target_root", ""),
                "branch": raw.get("branch", ""),
                "prompt": raw.get("prompt", ""),
                "skill_path": raw.get("skill_path", "skills/ORCHESTRATOR.md"),
                "controller_role": raw.get("controller_role", ""),
                "logical_roles": raw.get("logical_roles", []),
                "physical_role_map": raw.get("physical_role_map", {}),
                "finish_roles": raw.get("finish_roles", []),
                "status": raw.get("status", "BACKLOG"),
                "enabled": raw.get("enabled", True),
                "schedule": schedule,
                "execution_options": normalize_execution_options(raw.get("execution_options")),
                "next_run_at": next_run_at,
                "active_request_id": None,
                "last_request_id": None,
                "last_result_status": None,
                "last_result_summary": "",
                "blocker": "",
                "wake": wake,
                "events": [],
                "created_at": iso_utc(now),
                "updated_at": iso_utc(now),
                "archived_at": None,
                "transport": "userscript",
                "external_target": None,
            })
            if task_reserves_controller(task) and self._controller_conflict(task):
                raise TaskConflictError("controller_busy")
            self._append_event(task, self._event("created", "Task created", actor_role=task["controller_role"]))
            if manual_wake_server_instance_id:
                self._append_event(task, self._event("wake_claimed", "manual wake occurrence claimed", actor_role=task["controller_role"]))
            return self._publish_new_task(task)

    def _publish_new_task(self, task: dict[str, Any]) -> dict[str, Any]:
        candidate = copy.deepcopy(self._document)
        candidate["tasks"][task["task_id"]] = task
        candidate["revision"] += 1
        candidate["updated_at"] = task["updated_at"]
        validated = validate_document(candidate)
        self._persist(validated)
        self._document = validated
        return copy.deepcopy(validated["tasks"][task["task_id"]])

    def _current_for_update(self, task_id: str, expected_revision: int) -> dict[str, Any]:
        self._ensure_mutable()
        task = self._document["tasks"].get(str(task_id or "").strip())
        if task is None:
            raise KeyError(task_id)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("expected_revision must be an integer")
        if task["revision"] != expected_revision:
            raise TaskConflictError("stale_revision")
        return copy.deepcopy(task)

    def patch(self, task_id: str, expected_revision: int, changes: dict[str, Any], *, actor_role: str = "", wake_resolution: str | None = None) -> dict[str, Any]:
        with self.lock:
            task = self._current_for_update(task_id, expected_revision)
            original_task = copy.deepcopy(task)
            if task.get("archived_at"):
                raise TaskConflictError("archived")
            if not isinstance(changes, dict):
                raise ValueError("changes must be an object")
            allowed = {
                "title", "target_root", "branch", "prompt", "controller_role", "logical_roles", "physical_role_map",
                "finish_roles", "enabled", "schedule", "execution_options", "active_request_id", "last_request_id", "last_result_status",
                "last_result_summary", "blocker", "archived",
            }
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError(f"unsupported task changes: {', '.join(sorted(unknown))}")
            normalized_actor = normalize_role(actor_role) if actor_role else ""
            if EXECUTION_FIELDS.intersection(changes) and normalized_actor != task["controller_role"]:
                raise TaskConflictError("controller_mismatch")
            if wake_resolution is not None:
                if normalized_actor and normalized_actor != task["controller_role"]:
                    raise TaskConflictError("controller_mismatch")
                if task["wake"]["state"] != "UNCERTAIN":
                    raise TaskConflictError("wake_not_uncertain")
                if wake_resolution not in {"sent", "not_sent"}:
                    raise ValueError("wake_resolution must be sent or not_sent")
                if wake_resolution == "sent":
                    task["wake"]["state"] = "SENT"
                    task["wake"]["sent_at"] = task["wake"]["sent_at"] or iso_utc(self._now())
                else:
                    task["wake"] = empty_wake()
                task["blocker"] = ""
                self._append_event(task, self._event("wake_resolved", f"Uncertain wake resolved as {wake_resolution}", actor_role=normalized_actor or task["controller_role"]))
            for key, value in changes.items():
                if key == "archived":
                    if not isinstance(value, bool):
                        raise ValueError("archived must be a boolean")
                    if value:
                        if task["status"] != "DONE":
                            raise TaskConflictError("archive_requires_done")
                        task["archived_at"] = iso_utc(self._now())
                    else:
                        task["archived_at"] = None
                elif key == "schedule":
                    task["schedule"], task["next_run_at"] = normalize_schedule(value, self._now())
                elif key == "execution_options":
                    if not isinstance(value, dict):
                        raise ValueError("execution_options must be an object")
                    task[key] = normalize_execution_options({**task["execution_options"], **value})
                elif key == "controller_role":
                    task[key] = normalize_role(value, field=key)
                elif key == "logical_roles":
                    task[key] = [normalize_role(role, field=key) for role in value]
                elif key == "physical_role_map":
                    task[key] = {normalize_role(role, field=key): normalize_role(physical, field=key) for role, physical in value.items()}
                elif key == "finish_roles":
                    task[key] = [normalize_role(role, field=key) for role in value]
                elif key == "enabled":
                    if not isinstance(value, bool):
                        raise ValueError("enabled must be a boolean")
                    task[key] = value
                elif key in {"active_request_id", "last_request_id", "last_result_status"}:
                    task[key] = bounded_text(value, field=key, limit=160) if value else None
                elif key == "last_result_summary":
                    task[key] = bounded_text(value, field=key, limit=2000)
                elif key == "blocker":
                    task[key] = bounded_text(value, field=key, limit=2000)
                else:
                    limits = {"title": 160, "target_root": 500, "branch": 200, "prompt": 12000}
                    task[key] = bounded_text(value, field=key, limit=limits[key], allow_empty=False)
            if task_reserves_controller(original_task):
                reassigned = any(key in changes and task.get(key) != original_task.get(key) for key in REASSIGNMENT_FIELDS)
                if reassigned:
                    raise TaskConflictError("active_task_reassignment")
            if task_reserves_controller(task) and self._controller_conflict(task, exclude_task_id=task["task_id"]):
                raise TaskConflictError("controller_busy")
            task["revision"] += 1
            task["updated_at"] = iso_utc(self._now())
            self._append_event(task, self._event("updated", "Task metadata updated", actor_role=normalized_actor or task["controller_role"], request_id=task.get("active_request_id") or ""))
            return self._publish_existing(task)

    def move(self, task_id: str, expected_revision: int, destination: str, *, actor_role: str = "") -> dict[str, Any]:
        with self.lock:
            task = self._current_for_update(task_id, expected_revision)
            if task.get("archived_at"):
                raise TaskConflictError("archived")
            destination = bounded_text(destination, field="status", limit=16, allow_empty=False).upper()
            if destination not in TASK_STATES:
                raise ValueError("invalid destination status")
            source = task["status"]
            normalized_actor = normalize_role(actor_role) if actor_role else ""
            if destination != source and destination not in ALLOWED_TRANSITIONS[source]:
                raise TaskConflictError("invalid_state_transition")
            if destination in {"RUNNING", "REVIEW", "DONE"} and normalized_actor != task["controller_role"]:
                raise TaskConflictError("controller_mismatch")
            if destination in {"RUNNING", "REVIEW"}:
                conflict = self._controller_conflict(task, exclude_task_id=task["task_id"])
                if conflict:
                    raise TaskConflictError("controller_busy")
            task["status"] = destination
            if destination == "RUNNING" and task["wake"]["state"] == "SENT":
                task["wake"] = empty_wake()
                self._append_event(task, self._event("wake_acknowledged", "Controller claimed the sent wake", actor_role=normalized_actor))
            if destination == "DONE":
                if task.get("active_request_id"):
                    task["last_request_id"] = task["active_request_id"]
                task["active_request_id"] = None
                task["wake"] = empty_wake()
            task["revision"] += 1
            task["updated_at"] = iso_utc(self._now())
            if destination != source:
                self._append_event(task, self._event("moved", f"{source} -> {destination}", actor_role=normalized_actor or task["controller_role"], request_id=task.get("active_request_id") or task.get("last_request_id") or ""))
            return self._publish_existing(task)

    def claim_wake(self, task_id: str, expected_revision: int, *, source: str, scheduled_for: str | None, server_instance_id: str) -> dict[str, Any]:
        with self.lock:
            task = self._current_for_update(task_id, expected_revision)
            if task.get("archived_at") or not task["enabled"]:
                raise TaskConflictError("task_not_eligible")
            if task["status"] != "READY":
                raise TaskConflictError("task_not_ready")
            if task["wake"]["state"] in {"SENT", "UNCERTAIN"}:
                raise TaskConflictError("duplicate_or_uncertain_wake")
            if task["wake"]["state"] not in {"IDLE", "DEFERRED"}:
                raise TaskConflictError("duplicate_or_uncertain_wake")
            conflict = self._controller_conflict(task, exclude_task_id=task["task_id"])
            if conflict:
                raise TaskConflictError("controller_busy")
            source = bounded_text(source, field="wake.source", limit=32, allow_empty=False).lower()
            if source not in {"manual", "scheduled"}:
                raise ValueError("wake source must be manual or scheduled")
            now = self._now()
            previous_wake = task["wake"]
            preserve_occurrence = previous_wake["state"] == "DEFERRED"
            task["wake"] = {
                **empty_wake(),
                "state": "CLAIMED",
                "attempt_id": previous_wake["attempt_id"] if preserve_occurrence else f"attempt-{uuid.uuid4().hex}",
                "server_instance_id": bounded_text(server_instance_id, field="server_instance_id", limit=160, allow_empty=False),
                "source": previous_wake["source"] if preserve_occurrence else source,
                "scheduled_for": previous_wake["scheduled_for"] if preserve_occurrence else (iso_utc(parse_timestamp(scheduled_for, field="scheduled_for")) if scheduled_for else None),
                "requested_at": previous_wake["requested_at"] if preserve_occurrence else iso_utc(now),
            }
            task["revision"] += 1
            task["updated_at"] = iso_utc(now)
            if not preserve_occurrence:
                self._append_event(task, self._event("wake_claimed", f"{source} wake occurrence claimed", actor_role=task["controller_role"]))
            return self._publish_existing(task)

    def update_wake(
        self,
        task_id: str,
        expected_revision: int,
        *,
        state: str,
        command_id: Any = _WAKE_FIELD_UNSET,
        command_action: Any = _WAKE_FIELD_UNSET,
        error: str = "",
        retry_at: str | None = None,
        sent_at: str | None = None,
        blocker: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            task = self._current_for_update(task_id, expected_revision)
            state = bounded_text(state, field="wake.state", limit=40, allow_empty=False).upper()
            if state not in WAKE_STATES:
                raise ValueError("invalid wake state")
            wake = copy.deepcopy(task["wake"])
            wake["state"] = state
            if command_id is not _WAKE_FIELD_UNSET:
                wake["command_id"] = bounded_text(command_id, field="command_id", limit=160) if command_id else None
            if command_action is not _WAKE_FIELD_UNSET:
                wake["command_action"] = bounded_text(command_action, field="command_action", limit=40).upper() if command_action else None
            wake["error"] = bounded_text(error, field="wake.error", limit=500)
            wake["retry_at"] = iso_utc(parse_timestamp(retry_at, field="wake.retry_at")) if retry_at else None
            wake["sent_at"] = iso_utc(parse_timestamp(sent_at, field="wake.sent_at")) if sent_at else wake.get("sent_at")
            task["wake"] = wake
            if blocker is not None:
                task["blocker"] = bounded_text(blocker, field="blocker", limit=2000)
            task["revision"] += 1
            task["updated_at"] = iso_utc(self._now())
            last = task["events"][-1] if task["events"] else None
            summary = f"Wake state {state}" + (f": {error}" if error else "")
            if not (state == "DEFERRED" and last and last.get("type") == "wake_deferred" and last.get("summary") == summary):
                self._append_event(
                    task,
                    self._event(
                        "wake_deferred" if state == "DEFERRED" else "wake_state",
                        summary,
                        actor_role=task["controller_role"],
                        command_id=wake.get("command_id") or "",
                    ),
                )
            return self._publish_existing(task)

    def mark_wake_sent(self, task_id: str, expected_revision: int, *, now: datetime) -> dict[str, Any]:
        with self.lock:
            task = self._current_for_update(task_id, expected_revision)
            if task["wake"]["state"] != "CLICK_SEND_PENDING":
                raise TaskConflictError("wake_stage_mismatch")
            task["wake"]["state"] = "SENT"
            task["wake"]["sent_at"] = iso_utc(now)
            if task["wake"]["source"] == "scheduled":
                task["enabled"], task["next_run_at"] = next_after_success(task["schedule"], now)
            task["blocker"] = ""
            task["revision"] += 1
            task["updated_at"] = iso_utc(now)
            self._append_event(task, self._event("wake_sent", "Wake prompt accepted by controller composer", actor_role=task["controller_role"], command_id=task["wake"].get("command_id") or ""))
            return self._publish_existing(task)

    def pause(self, task_id: str, expected_revision: int) -> dict[str, Any]:
        return self.patch(task_id, expected_revision, {"enabled": False})

    def resume(self, task_id: str, expected_revision: int) -> dict[str, Any]:
        with self.lock:
            task = self._current_for_update(task_id, expected_revision)
            now = self._now()
            task["enabled"] = True
            kind = task["schedule"]["kind"]
            if kind == "manual":
                task["next_run_at"] = None
            elif kind == "once":
                run_at = parse_timestamp(task["schedule"]["run_at"], field="schedule.run_at")
                task["next_run_at"] = iso_utc(run_at) if run_at > now else None
                if run_at <= now:
                    task["enabled"] = False
            else:
                _, task["next_run_at"] = next_after_success(task["schedule"], now)
            task["revision"] += 1
            task["updated_at"] = iso_utc(self._now())
            self._append_event(task, self._event("resumed", "Task schedule resumed", actor_role=task["controller_role"]))
            return self._publish_existing(task)

    def due_tasks(self, now: datetime) -> list[dict[str, Any]]:
        now = now.astimezone(timezone.utc)
        with self.lock:
            tasks = []
            for task in self._document["tasks"].values():
                if task.get("archived_at") or not task["enabled"] or not task.get("next_run_at"):
                    continue
                wake = task["wake"]
                due = parse_timestamp(task["next_run_at"], field="next_run_at") <= now
                retry_ready = wake["state"] != "DEFERRED" or not wake.get("retry_at") or parse_timestamp(wake["retry_at"], field="retry_at") <= now
                if due and retry_ready and wake["state"] in {"IDLE", "DEFERRED"}:
                    tasks.append(copy.deepcopy(task))
            tasks.sort(
                key=lambda task: (
                    parse_timestamp(task["next_run_at"], field="next_run_at"),
                    parse_timestamp(task["created_at"], field="created_at"),
                    task["task_id"],
                )
            )
            return tasks

    def recover_for_server(self, server_instance_id: str) -> list[dict[str, Any]]:
        recovered = []
        for task in self.list_tasks(include_archived=True):
            wake = task["wake"]
            if wake["state"] in {"ISSUING_SET_PROMPT", "SET_PROMPT_PENDING", "ISSUING_CLICK_SEND", "CLICK_SEND_PENDING", "CLAIMED"} and wake.get("server_instance_id") and wake["server_instance_id"] != server_instance_id:
                recovered.append(self.update_wake(task["task_id"], task["revision"], state="UNCERTAIN", error="wakeup outcome uncertain after server restart", blocker="wakeup outcome uncertain after server restart"))
        return recovered

    def _publish_existing(self, task: dict[str, Any]) -> dict[str, Any]:
        candidate = copy.deepcopy(self._document)
        candidate["tasks"][task["task_id"]] = task
        candidate["revision"] += 1
        candidate["updated_at"] = task["updated_at"]
        validated = validate_document(candidate)
        self._persist(validated)
        self._document = validated
        return copy.deepcopy(validated["tasks"][task["task_id"]])

    def _persist(self, candidate: dict[str, Any]) -> None:
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
                newline="\n",
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(candidate, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except (OSError, TypeError, ValueError) as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise TaskStoreMutationError(f"atomic task write failed: {exc}") from exc
