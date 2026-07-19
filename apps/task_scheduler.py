from __future__ import annotations

import copy
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from apps.task_store import (
    TaskConflictError,
    TaskStore,
    iso_utc,
    parse_timestamp,
    task_reserves_controller,
    utc_now,
)


CONTROL_REPOSITORY = r"E:\python_project\tampermonkey_auto"
SET_SUCCESS = "PASTE_CONFIRMED"
SEND_SUCCESS = "SEND_ACCEPTED"


def build_wake_prompt(task: dict[str, Any]) -> str:
    logical_roles = ", ".join(task["logical_roles"])
    role_map = ", ".join(f"{logical}={physical}" for logical, physical in task["physical_role_map"].items())
    finish_roles = ", ".join(task["finish_roles"])
    return (
        f"DASHBOARD_TASK_WAKE\n"
        f"Task ID: {task['task_id']}\n"
        f"Wake snapshot/audit revision: {task['revision']}\n"
        f"Assigned controller role: {task['controller_role']}\n"
        f"Control repository: {CONTROL_REPOSITORY}\n"
        f"Read exact skill: {task['skill_path']}\n"
        f"Target root: {task['target_root']}\n"
        f"Target branch: {task['branch']}\n"
        f"Logical roles: {logical_roles}\n"
        f"Physical role map: {role_map}\n"
        f"Finish roles: {finish_roles}\n\n"
        f"User-authorized task objective:\n{task['prompt']}\n\n"
        "Controller contract:\n"
        "1. This is a wake snapshot/audit revision; scheduler wake stages may advance the task revision after this prompt is built.\n"
        "2. Perform a fresh GET immediately before every CAS claim or update: GET the exact task from the dashboard task API, use only the revision returned by that fresh GET, and verify the exact controller_role assignment.\n"
        "3. Inspect existing process, durable flow, browser command, exact report, and result evidence before starting anything.\n"
        "4. Claim the task with an optimistic READY -> RUNNING move as this exact assigned controller before launching work.\n"
        "5. Do not create a duplicate active workflow; reuse or recover the existing flow when evidence shows one already exists.\n"
        "6. Read skills/ORCHESTRATOR.md and choose main.py or role.py from current evidence; this wake contains no executable command.\n"
        "7. PATCH active_request_id, blocker, review/result status, and final summary as evidence changes.\n"
        "8. Never claim, run, or modify another controller's task. Resolve an UNCERTAIN wake only after checking whether work already started.\n"
    )


class TaskScheduler:
    def __init__(
        self,
        *,
        store: TaskStore,
        server_instance_id: str,
        readiness: Callable[[str], dict[str, Any]],
        create_command: Callable[[str, str, dict[str, Any], str], dict[str, Any]],
        command_result: Callable[[str], dict[str, Any] | None],
        clock: Callable[[], datetime] = utc_now,
        poll_interval_s: float = 1.0,
        retry_delay_s: float = 15.0,
    ):
        self.store = store
        self.server_instance_id = str(server_instance_id or "").strip()
        if not self.server_instance_id:
            raise ValueError("server_instance_id must not be empty")
        self.readiness = readiness
        self.create_command = create_command
        self.command_result = command_result
        self.clock = clock
        self.poll_interval_s = max(0.01, float(poll_interval_s))
        self.retry_delay_s = max(1.0, float(retry_delay_s))
        self._lock = threading.RLock()
        self._signal = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._started_at: str | None = None
        self._last_tick_at: str | None = None
        self._last_error = ""
        self._prompts: dict[str, str] = {}

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduler clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def start(self) -> None:
        with self._lock:
            if self._running and self._thread and self._thread.is_alive():
                return
            try:
                self.store.recover_for_server(self.server_instance_id)
            except Exception as exc:
                self._last_error = f"startup recovery failed: {type(exc).__name__}: {exc}"[:500]
            self._running = True
            self._started_at = iso_utc(self._now())
            self._signal.clear()
            self._thread = threading.Thread(target=self._run, name=f"task-scheduler-{self.server_instance_id[:8]}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if not self._running and not thread:
                return
            self._running = False
            self._signal.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.poll_interval_s * 4))
        with self._lock:
            self._thread = None

    def wake(self) -> None:
        self._signal.set()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
            try:
                self.tick()
            except Exception as exc:  # scheduler must remain observable, not silently die
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"[:500]
            self._signal.wait(self.poll_interval_s)
            self._signal.clear()

    def health(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._running and self._thread and self._thread.is_alive())
            return {
                "running": running,
                "server_instance_id": self.server_instance_id,
                "started_at": self._started_at,
                "last_tick_at": self._last_tick_at,
                "last_error": self._last_error,
                "next_due_at": self._next_due_at(),
            }

    def _next_due_at(self) -> str | None:
        values: list[str] = []
        for task in self.store.list_tasks():
            if task.get("enabled") and task.get("next_run_at"):
                values.append(task["next_run_at"])
            retry = (task.get("wake") or {}).get("retry_at")
            if retry:
                values.append(retry)
        if not values:
            return None
        return min(values, key=lambda value: parse_timestamp(value, field="scheduler timestamp"))

    def request_manual(self, task_id: str, expected_revision: int) -> dict[str, Any]:
        task = self.store.claim_wake(
            task_id,
            expected_revision,
            source="manual",
            scheduled_for=None,
            server_instance_id=self.server_instance_id,
        )
        self.wake()
        return task

    def tick(self) -> None:
        now = self._now()
        with self._lock:
            self._last_tick_at = iso_utc(now)

        reserved_this_tick: set[str] = set()
        for task in self.store.due_tasks(now):
            controller = task["controller_role"]
            if controller in reserved_this_tick:
                continue
            try:
                self.store.claim_wake(
                    task["task_id"],
                    task["revision"],
                    source="scheduled",
                    scheduled_for=task["next_run_at"],
                    server_instance_id=self.server_instance_id,
                )
                reserved_this_tick.add(controller)
            except TaskConflictError:
                continue

        for task in self.store.list_tasks():
            wake = task["wake"]
            if wake["state"] == "DEFERRED":
                retry_at = wake.get("retry_at")
                if retry_at and parse_timestamp(retry_at, field="retry_at") > now:
                    continue
                reason = self._readiness_error(self._snapshot(task["controller_role"]))
                if reason:
                    self._defer(task, reason, now)
                    continue
                try:
                    task = self.store.claim_wake(
                        task["task_id"],
                        task["revision"],
                        source=wake.get("source") or "manual",
                        scheduled_for=wake.get("scheduled_for"),
                        server_instance_id=self.server_instance_id,
                    )
                except TaskConflictError as exc:
                    if exc.code == "controller_busy":
                        self._defer(task, "controller_busy", now)
                    continue
                except KeyError:
                    continue
            if task["wake"]["state"] in {
                "CLAIMED", "ISSUING_SET_PROMPT", "SET_PROMPT_PENDING",
                "ISSUING_CLICK_SEND", "CLICK_SEND_PENDING",
            }:
                self._advance(task, now)
        with self._lock:
            self._last_error = ""

    def _snapshot(self, role: str) -> dict[str, Any]:
        raw = self.readiness(role) or {}
        return {
            "online": bool(raw.get("online")),
            "active_command": copy.deepcopy(raw.get("active_command")),
            "composer": bool(raw.get("composer")),
            "stop_visible": bool(raw.get("stop_visible")),
            "composer_text": str(raw.get("composer_text") or ""),
            "attachment_count": int(raw.get("attachment_count") or 0),
            "manual_input": bool(raw.get("manual_input")),
        }

    @staticmethod
    def _readiness_error(snapshot: dict[str, Any], *, expected_text: str | None = None) -> str:
        if not snapshot["online"]:
            return "controller_offline"
        if snapshot["active_command"]:
            return "controller_busy"
        if not snapshot["composer"]:
            return "composer_missing"
        if snapshot["stop_visible"]:
            return "assistant_active"
        if snapshot["manual_input"]:
            return "manual_input"
        if snapshot["attachment_count"]:
            return "attachment_pending"
        if expected_text is None:
            if snapshot["composer_text"]:
                return "composer_dirty"
        elif snapshot["composer_text"] != expected_text:
            return "composer_ownership_lost"
        return ""

    def _other_task_reserves_controller(self, task: dict[str, Any]) -> bool:
        for other in self.store.list_tasks(include_archived=True):
            if other["task_id"] == task["task_id"] or other.get("archived_at"):
                continue
            if other["controller_role"] == task["controller_role"] and task_reserves_controller(other):
                return True
        return False

    def _defer(self, task: dict[str, Any], reason: str, now: datetime) -> None:
        retry_at = iso_utc(now + timedelta(seconds=self.retry_delay_s))
        try:
            self.store.update_wake(
                task["task_id"], task["revision"], state="DEFERRED",
                command_id=None, command_action=None,
                error=reason, retry_at=retry_at, blocker=None,
            )
        except (TaskConflictError, KeyError):
            pass

    def _uncertain(self, task: dict[str, Any], reason: str) -> None:
        try:
            self.store.update_wake(
                task["task_id"], task["revision"], state="UNCERTAIN",
                error=reason, blocker=reason,
            )
        except (TaskConflictError, KeyError):
            pass

    def _advance(self, task: dict[str, Any], now: datetime) -> None:
        stage = task["wake"]["state"]
        if task["wake"].get("server_instance_id") != self.server_instance_id:
            self._uncertain(task, "wakeup outcome uncertain after server restart")
            return
        role = task["controller_role"]
        attempt_id = task["wake"].get("attempt_id") or ""

        if stage == "CLAIMED":
            if self._other_task_reserves_controller(task):
                self._defer(task, "controller_busy", now)
                return
            snapshot = self._snapshot(role)
            reason = self._readiness_error(snapshot)
            if reason:
                self._defer(task, reason, now)
                return
            command_id = str(uuid.uuid4())
            command_accepted = False
            try:
                issuing = self.store.update_wake(
                    task["task_id"], task["revision"], state="ISSUING_SET_PROMPT",
                    command_id=command_id, command_action="SET_PROMPT",
                )
                prompt = build_wake_prompt(issuing)
                self._prompts[attempt_id] = prompt
                command = self.create_command(
                    role,
                    "SET_PROMPT",
                    {"text": prompt, "method": "auto", "expected_text": prompt},
                    command_id,
                )
                command_accepted = True
                if str(command.get("command_id") or "") != command_id:
                    raise RuntimeError("browser command ID did not match durable task linkage")
                self.store.update_wake(
                    issuing["task_id"], issuing["revision"], state="SET_PROMPT_PENDING",
                    command_id=command_id, command_action="SET_PROMPT",
                )
            except Exception as exc:
                current = self.store.get(task["task_id"])
                if current:
                    snapshot = self._snapshot(role)
                    active_command = snapshot.get("active_command")
                    active_command_id = str(active_command.get("command_id") or "") if isinstance(active_command, dict) else ""
                    exact_command_active = bool(command_id and active_command_id == command_id)
                    composer_clean = bool(
                        snapshot["online"]
                        and snapshot["composer"]
                        and not snapshot["stop_visible"]
                        and not snapshot["manual_input"]
                        and not snapshot["attachment_count"]
                        and not snapshot["composer_text"]
                    )
                    if command_accepted or exact_command_active:
                        self._uncertain(current, f"SET_PROMPT accepted but pending persistence is uncertain: {exc}")
                    elif not active_command and composer_clean and current["wake"]["state"] in {"CLAIMED", "ISSUING_SET_PROMPT"}:
                        self._defer(current, f"set_prompt_create_failed:{type(exc).__name__}", now)
                    else:
                        self._uncertain(current, f"SET_PROMPT issuance uncertain: {exc}")
            return

        if stage == "ISSUING_SET_PROMPT":
            self._uncertain(task, "SET_PROMPT issuance outcome uncertain")
            return

        if stage == "SET_PROMPT_PENDING":
            prompt = self._prompts.get(attempt_id)
            if not prompt:
                self._uncertain(task, "wake prompt ownership lost in current server instance")
                return
            command_id = task["wake"].get("command_id") or ""
            result = self.command_result(command_id)
            if result is None:
                return
            result_state = str(result.get("state") or "").upper()
            if result_state != SET_SUCCESS:
                snapshot = self._snapshot(role)
                if self._readiness_error(snapshot) == "":
                    self._defer(task, f"set_prompt_terminal:{result_state or 'unknown'}", now)
                else:
                    self._uncertain(task, f"SET_PROMPT failed with possible composer mutation: {result_state or 'unknown'}")
                return
            if self._other_task_reserves_controller(task):
                self._uncertain(task, "another task reserved the controller after prompt paste")
                return
            snapshot = self._snapshot(role)
            reason = self._readiness_error(snapshot, expected_text=prompt)
            if reason:
                self._uncertain(task, f"cannot prove wake prompt ownership before send: {reason}")
                return
            try:
                command_id = str(uuid.uuid4())
                issuing = self.store.update_wake(
                    task["task_id"], task["revision"], state="ISSUING_CLICK_SEND",
                    command_id=command_id, command_action="CLICK_SEND",
                )
                command = self.create_command(role, "CLICK_SEND", {"expected_text": prompt}, command_id)
                if str(command.get("command_id") or "") != command_id:
                    raise RuntimeError("browser command ID did not match durable task linkage")
                self.store.update_wake(
                    issuing["task_id"], issuing["revision"], state="CLICK_SEND_PENDING",
                    command_id=command_id, command_action="CLICK_SEND",
                )
            except Exception as exc:
                current = self.store.get(task["task_id"])
                if current:
                    self._uncertain(current, f"CLICK_SEND issuance uncertain: {exc}")
            return

        if stage == "ISSUING_CLICK_SEND":
            self._uncertain(task, "CLICK_SEND issuance outcome uncertain")
            return

        if stage == "CLICK_SEND_PENDING":
            command_id = task["wake"].get("command_id") or ""
            result = self.command_result(command_id)
            if result is None:
                return
            result_state = str(result.get("state") or "").upper()
            if result_state == SEND_SUCCESS:
                try:
                    self.store.mark_wake_sent(task["task_id"], task["revision"], now=now)
                    self._prompts.pop(attempt_id, None)
                except (TaskConflictError, KeyError):
                    pass
            else:
                self._uncertain(task, f"CLICK_SEND terminal result is ambiguous: {result_state or 'unknown'}")
