import os
import shutil
import signal

import subprocess
import threading
import time
import uuid
import re

from contextlib import asynccontextmanager
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apps.flow_store import DEFAULT_FLOW_PATH, FlowStore, FlowStoreMutationError
from apps.task_scheduler import CONTROL_REPOSITORY, TaskScheduler, build_launch_argv, build_launch_command
from apps.task_store import (
    DEFAULT_TASK_PATH,
    RESERVING_WAKE_STATES,
    TaskConflictError,
    TaskStore,
    TaskStoreMutationError,
    bounded_text,
    normalize_execution_options,
    normalize_role,
    task_reserves_controller,
)


SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8500
DASHBOARD_PATH = Path(__file__).resolve().with_name("dashboard.html")
DASHBOARD_LAUNCH_DIR = Path(CONTROL_REPOSITORY) / ".runtime" / "dashboard-launches"


TERMINAL_STATES = {
    "CANCELLED",
    "EXPIRED",
    "PROBE_DONE",
    "DUMP_BUTTONS_DONE",
    "COMPOSER_STABLE",
    "COMPOSER_UNSTABLE",
    "PASTE_CONFIRMED",
    "PASTE_FAILED",
    "FIND_SEND_DONE",
    "SEND_BUTTON_ENABLED_DONE",
    "SEND_ACCEPTED",
    "SEND_FAILED",
    "ASSISTANT_DONE",
    "ASSISTANT_TIMEOUT",
    "TRANSCRIPT_SAVED",
    "TRANSCRIPT_SAVE_ACK",
    "PAGE_RELOADING",
    "NEW_CHAT_NAVIGATING",
    "WINDOW_CLOSE_REQUESTED",
    "WINDOW_CLOSE_BLOCKED",
    "UPLOAD_FILES_DONE",
    "UPLOAD_FILES_FAILED",
    "SEND_BLOCKED_OWNERSHIP_LOST",
    "PASTE_BLOCKED_MANUAL_INPUT",
    "MANUAL_INPUT_PENDING",
    "CHOICE_PROMPT_CLICKED",
    "CHOICE_PROMPT_CLICK_FAILED",
    "CHOICE_PROMPT_NOT_FOUND",
    "ROLE_SET",
    "ROLE_TAKEOVER_RELOADING",
    "ROLE_TAKEOVER_FAILED",
    "COMPOSER_TEXT_CLEARED",
    "COMPOSER_TEXT_CLEAR_FAILED",
    "UNKNOWN_COMMAND",
    "ERROR_COMMAND",
}


PAGE_HANDOFF_SAFE_ACTIONS = {"PROBE", "SYNC_TRANSCRIPT", "WAIT_ASSISTANT_DONE"}
BRIDGE_REQUIRED_ADMIN_ACTIONS = {
    "PROBE", "DUMP_BUTTONS", "WAIT_COMPOSER_STABLE", "SET_PROMPT", "CLEAR_COMPOSER_TEXT",
    "UPLOAD_FILE", "UPLOAD_FILES", "PASTE_IMAGE", "PASTE_FILES", "FIND_SEND", "CLICK_SEND",
    "WAIT_ASSISTANT_DONE", "SYNC_TRANSCRIPT", "CLICK_CHOICE_PROMPT", "SET_ROLE", "TAKEOVER_ROLE",
    "PHYSICAL_TAKEOVER_ROLE", "OPEN_ROLE_WINDOW", "WAKE_ROLE", "PHYSICAL_OPEN_ROLE", "NEW_CHAT",
    "NAVIGATE_NEW", "RESET_PAGE", "RELOAD_PAGE", "RELOAD", "HARD_RELOAD", "CLOSE_WINDOW", "CLOSE_TAB",
}
COMMAND_CANCEL_STATES = {"CANCELLED", "EXPIRED"}
NON_TERMINAL_FLOW_STATES = {"RUNNING", "WAITING"}
SEMANTIC_ROLE_EVENTS = {
    "ROLE_CLAIMED",
    "ROLE_RELEASED",
    "COMMAND_CREATED",
    "COMMAND_DELIVERED",
    "COMMAND_CANCELLED",
    "COMMAND_EXPIRED",
}


def _iso_from_epoch(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


class StatusRequest(BaseModel):
    role: str
    session_id: str = ""
    page_instance_id: str = ""
    role_owner_id: str = ""
    role_claim_id: str = ""
    claim_role: bool = False
    observation_seq: int = 0
    dom_info: Optional[Dict[str, Any]] = None


class ReportRequest(BaseModel):
    role: str
    session_id: str = ""
    page_instance_id: str = ""
    role_owner_id: str = ""
    role_claim_id: str = ""
    command_id: str = ""
    state: str
    text: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)
    observation_seq: int = 0
    dom_info: Optional[Dict[str, Any]] = None


class SyncRequest(BaseModel):
    role: str
    session_id: str = ""
    page_instance_id: str = ""
    role_owner_id: str = ""
    role_claim_id: str = ""
    reason: str = ""
    observation_seq: int = 0
    transcript: Optional[Dict[str, Any]] = None
    snapshot: Optional[Dict[str, Any]] = None


class AdminCommandRequest(BaseModel):
    role: str
    action: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class AdminCommandCancelRequest(BaseModel):
    state: str = "CANCELLED"
    reason: str = "cancelled_by_admin"


class AdminConfigRequest(BaseModel):
    config: Dict[str, Any] = Field(default_factory=dict)


class RoleClaimRequest(BaseModel):
    session_id: str = ""


class RoleClaimReservationRequest(BaseModel):
    role: str


class RoleReleaseRequest(BaseModel):
    role: str
    session_id: str = ""
    page_instance_id: str = ""
    role_owner_id: str = ""
    role_claim_id: str = ""


class FlowStatusRequest(BaseModel):
    run_id: str = ""
    request_id: str = ""
    parent_request_id: Optional[str] = None
    goal_hash: Optional[str] = None
    terminal_status: Optional[str] = None
    activate: bool = False
    updates: Dict[str, Optional[Dict[str, Any]]] = Field(default_factory=dict)


class FlowHeartbeatRequest(BaseModel):
    run_id: str = ""
    request_id: str = ""
    pid: Optional[int] = None


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    target_root: str
    branch: str
    prompt: str
    skill_path: str = "skills/ORCHESTRATOR.md"
    controller_role: str
    logical_roles: list[str]
    physical_role_map: Dict[str, str]
    finish_roles: list[str]
    status: str = "BACKLOG"
    enabled: bool = True
    schedule: Dict[str, Any] = Field(default_factory=lambda: {"kind": "manual"})
    execution_options: Dict[str, Any] = Field(default_factory=dict)


class TaskLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    controller_role: str
    prompt: str
    logical_roles: list[str]
    physical_role_map: Dict[str, str]
    finish_roles: list[str]
    execution_options: Dict[str, Any] = Field(default_factory=dict)


class TaskPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int
    changes: Dict[str, Any] = Field(default_factory=dict)
    actor_role: str = ""
    wake_resolution: Optional[str] = None


class TaskMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int
    status: str
    actor_role: str = ""


class TaskRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int


class DiagnosticState:
    def __init__(self, flow_path=DEFAULT_FLOW_PATH, task_path=DEFAULT_TASK_PATH, scheduler_poll_s: float = 1.0):
        self.lock = threading.RLock()
        self.commands = {}
        self.command_results = {}
        self.command_status = {}
        self.dashboard_processes = {}
        self.status = defaultdict(lambda: "OFFLINE")
        self.sessions = defaultdict(set)
        self.current_sessions = defaultdict(str)
        self.dom_info = defaultdict(dict)
        self.transcripts = defaultdict(list)
        self.last_user_message = defaultdict(str)
        self.last_response = defaultdict(str)
        self.observation_pages = {}
        self.retired_observation_pages = defaultdict(set)
        self.events = deque(maxlen=10000)
        self.role_seen_at = defaultdict(float)
        self.role_owners = {}
        self.next_role_claim_generation = 0
        self.flow_store = FlowStore(flow_path)
        self.flow_statuses = self.flow_store.active_projection()
        self.task_store = TaskStore(task_path)
        self.server_instance_id = f"server-{uuid.uuid4().hex}"
        self.task_scheduler = TaskScheduler(
            store=self.task_store,
            server_instance_id=self.server_instance_id,
            readiness=self.controller_readiness,
            create_command=self.create_command,
            command_result=self.scheduler_command_result,
            poll_interval_s=scheduler_poll_s,
        )
        self.auto_open_roles = {}
        self.auto_role_inflight = defaultdict(int)
        self.config = {

            "poll_ms": 800,
            "sync_debounce_ms": 1200,
            "wait_loop_interval_ms": 500,
            "action_delay_min_ms": 3000,
            "action_delay_max_ms": 5000,
            "send_delay_min_ms": 2000,
            "send_delay_max_ms": 5000,
            "role_switch_delay_min_s": 3,
            "role_switch_delay_max_s": 5,
            "composer_stable_samples": 6,
            "composer_stable_sample_ms": 300,
            "composer_watchdog_ms": 60000,
            "assistant_quiet_ms": 2500,
            "send_accept_timeout_ms": 60000,
            "send_accept_poll_ms": 400,
            "assistant_force_sync_quiet_ms": 5000,
            "assistant_post_stop_timeout_ms": 15000,
            "report_wait_every_ms": 1500,
            "max_button_dump": 80,
            "auto_reload_on_assistant_timeout": True,
            "reload_after_timeout_ms": 1500,
            "auto_open_missing_model": True,
            "auto_open_url": "https://chatgpt.com/",
            "auto_open_wait_s": 45,
            "auto_close_after_s": 600,
            "runner_heartbeat_interval_s": 5,
            "runner_stale_after_s": 20,
            "command_stale_after_s": 120,
            "dashboard_role_retention_s": 900,
        }
        self.runner_heartbeats: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def role_owner_matches(owner: Dict[str, Any] | None, owner_id: str, claim_id: str, page_instance_id: str) -> bool:
        if not owner:
            return True
        if owner.get("role_owner_id") or owner.get("role_claim_id"):
            return bool(owner_id and claim_id and owner.get("role_owner_id") == owner_id and owner.get("role_claim_id") == claim_id)
        return bool(page_instance_id and owner.get("page_instance_id") == page_instance_id)

    @staticmethod
    def claim_is_newer(candidate: str, current: str) -> bool:
        """Compare client claim generations without letting delayed work reclaim a role."""
        candidate_text = str(candidate or "")
        current_text = str(current or "")
        if not current_text:
            return bool(candidate_text)
        if not candidate_text:
            return False
        candidate_match = re.match(r"^g-(\d+)-", candidate_text)
        current_match = re.match(r"^g-(\d+)-", current_text)
        if candidate_match and current_match:
            return int(candidate_match.group(1)) > int(current_match.group(1))
        # Compatibility with claims made before the reservation endpoint. Equal
        # generations are never ordered by their random suffix.
        try:
            candidate_generation = int(candidate_text.split("-", 1)[0])
            current_generation = int(current_text.split("-", 1)[0])
            return candidate_generation > current_generation
        except ValueError:
            return False

    def reserve_role_claim(self, role: str) -> str:
        with self.lock:
            self.next_role_claim_generation += 1
            return f"g-{self.next_role_claim_generation}-{uuid.uuid4().hex}"

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.config.update(updates)
            return dict(self.config)

    def role_is_online(self, role: str, *, now: float | None = None) -> tuple[bool, float | None]:
        normalized = normalize_role(role)
        seen_at = float(self.role_seen_at.get(normalized, 0.0) or 0.0)
        age = max(0.0, (time.time() if now is None else now) - seen_at) if seen_at else None
        poll_s = max(0.1, float(self.config.get("poll_ms", 800) or 800) / 1000.0)
        return bool(age is not None and age <= max(10.0, poll_s * 5.0)), age

    def controller_readiness(self, role: str) -> Dict[str, Any]:
        normalized = normalize_role(role)
        with self.lock:
            online, _ = self.role_is_online(normalized)
            dom = dict(self.dom_info.get(normalized) or {})
            composer_text = str(dom.get("composer_text") or "")
            if not composer_text and int(dom.get("composer_text_len") or 0) > 0:
                composer_text = "<unknown-nonempty-composer>"
            attachments = dom.get("composer_attachments")
            attachment_count = len(attachments) if isinstance(attachments, list) else int(dom.get("attachment_count") or 0)
            active = self.active_command(normalized)
            return {
                "online": online,
                "active_command": dict(active) if active else None,
                "composer": bool(dom.get("composer")),
                "stop_visible": bool(dom.get("stop_visible")),
                "composer_text": composer_text,
                "attachment_count": attachment_count,
                "manual_input": bool(dom.get("manual_input_pending")),
            }

    def scheduler_command_result(self, command_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            result = self.command_results.get(command_id)
            return dict(result) if result is not None else None

    def configured_role_for(self, physical_role: str) -> str:
        normalized = normalize_role(physical_role)
        last_user = str(self.last_user_message.get(normalized) or "")
        for pattern in (
            r"(?m)^PROMPT_ROLE:\s*([A-Za-z0-9_-]+)\s*$",
            r'"prompt_role"\s*:\s*"([A-Za-z0-9_-]+)"',
        ):
            match = re.search(pattern, last_user)
            if match:
                return normalize_role(match.group(1))
        flow = self.flow_statuses.get(normalized) or {}
        return normalize_role(flow.get("logical_role") or normalized)

    def response_projection(self, physical_role: str) -> list[Dict[str, Any]]:
        normalized = normalize_role(physical_role)
        projected: list[Dict[str, Any]] = []
        turn = 0
        for message in self.transcripts.get(normalized, []):
            if not isinstance(message, dict) or str(message.get("role") or "").lower() != "assistant":
                continue
            turn += 1
            images = message.get("images")
            image_count = len(images) if isinstance(images, list) else int(message.get("image_count") or 0)
            text = str(message.get("text") or "")
            projected.append({
                "turn": turn,
                "text": text,
                "text_len": len(text),
                "image_count": image_count,
            })
        return projected

    @staticmethod
    def is_semantic_role_event(event: Dict[str, Any]) -> bool:
        name = str(event.get("event") or "").strip().upper()
        if not name or name in {"SYNC", "SYNC_IGNORED", "REPORT_IGNORED"}:
            return False
        return bool(
            name in SEMANTIC_ROLE_EVENTS
            or name in TERMINAL_STATES
            or name.startswith(("FLOW_", "ERROR_"))
            or any(marker in name for marker in ("FAILED", "TIMEOUT", "BLOCKED"))
        )

    def role_timeline(self, physical_role: str, *, limit: int = 100) -> Dict[str, Any]:
        normalized = normalize_role(physical_role)
        bounded_limit = max(1, min(int(limit), 500))
        with self.lock:
            raw_events = [
                dict(event)
                for event in self.events
                if str(event.get("role") or "").strip().upper() == normalized
            ]
        meaningful = [event for event in raw_events if self.is_semantic_role_event(event)]
        return {
            "role": normalized,
            "events": meaningful[-bounded_limit:],
            "raw_event_count": len(raw_events),
            "omitted_event_count": len(raw_events) - len(meaningful),
        }

    def role_inventory(self) -> list[Dict[str, Any]]:
        """Return live roles, offline operational owners, and bounded recent evidence.

        Recent last-known browser evidence remains visible for the configured dashboard
        retention window. Long-expired historical caches are omitted unless the role
        still owns a nonterminal flow, active command, or controller-reserving task.
        """
        with self.lock:
            tasks = self.task_store.list_tasks(include_archived=True)
            role_reasons: Dict[str, set[str]] = defaultdict(set)
            retention_s = max(10.0, float(self.config.get("dashboard_role_retention_s", 900) or 900))

            def retain(role: str, reason: str) -> None:
                role_reasons[normalize_role(role)].add(reason)

            for role in self.role_seen_at:
                normalized = normalize_role(role)
                online, age = self.role_is_online(normalized)
                if online:
                    retain(normalized, "online")
                    continue
                has_cached_evidence = bool(
                    self.current_sessions.get(normalized)
                    or (self.dom_info.get(normalized) or {}).get("page_path")
                    or (self.dom_info.get(normalized) or {}).get("pathname")
                    or self.observation_pages.get(normalized)
                )
                if age is not None and age <= retention_s and has_cached_evidence:
                    retain(normalized, "recent_cached_evidence")

            for role in self.commands:
                normalized = normalize_role(role)
                if self.active_command(normalized):
                    retain(normalized, "active_command")

            for role, flow in self.flow_statuses.items():
                if str((flow or {}).get("state") or "").upper() in NON_TERMINAL_FLOW_STATES:
                    retain(role, "active_flow")

            for task in tasks:
                if task.get("archived_at") or not task_reserves_controller(task):
                    continue
                retain(task["controller_role"], "reserved_task")
                for role in task["physical_role_map"].values():
                    retain(role, "reserved_task")

            result = []
            for role in sorted(role_reasons):
                normalized = normalize_role(role)
                online, age = self.role_is_online(normalized)
                dom = dict(self.dom_info.get(normalized) or {})
                flow = dict(self.flow_statuses.get(normalized) or {})
                active = self.active_command(normalized)
                reserved_task = next(
                    (
                        task for task in tasks
                        if task_reserves_controller(task)
                        and not task.get("archived_at")
                        and (
                            task["controller_role"] == normalized
                            or normalized in set(task["physical_role_map"].values())
                        )
                    ),
                    None,
                )
                responses = self.response_projection(normalized)
                retention_reasons = sorted(role_reasons[normalized])
                operational = any(reason in {"active_command", "active_flow", "reserved_task"} for reason in retention_reasons)
                presence_status = "ONLINE" if online else "OFFLINE" if operational else "STALE"
                result.append({
                    "role": normalized,
                    "configured_role": self.configured_role_for(normalized),
                    "turn": len(responses),
                    "online": online,
                    "status": presence_status,
                    "evidence_cached": not online,
                    "retention_reasons": retention_reasons,
                    "last_seen_age_s": age,
                    "current_logical_role": flow.get("logical_role") or None,
                    "current_flow_state": flow.get("state") or None,
                    "current_task_id": reserved_task["task_id"] if reserved_task else None,
                    "reservation_state": (
                        reserved_task["wake"]["state"] if reserved_task and reserved_task["wake"]["state"] in RESERVING_WAKE_STATES
                        else reserved_task["status"] if reserved_task else None
                    ),
                    "page_path": dom.get("page_path") or dom.get("pathname") or "",
                    "page_instance_id": str((self.observation_pages.get(normalized) or {}).get("page_instance_id") or ""),
                    "observation_seq": int((self.observation_pages.get(normalized) or {}).get("observation_seq") or 0),
                    "bridge_version": str(dom.get("bridge_version") or ""),
                    "dom_summary": self.dom_summary(dom),
                    "active_command": {
                        "command_id": active.get("command_id"),
                        "action": active.get("action"),
                        "state": self.command_status.get(active.get("command_id"), active.get("status", "PENDING")),
                    } if active else None,
                    "transport": "userscript",
                    "external_target": None,
                })
            return result

    def update_flow_statuses(
        self,
        run_id: str,
        updates: Dict[str, Optional[Dict[str, Any]]],
        *,
        request_id: str = "",
        parent_request_id: Optional[str] = None,
        goal_hash: Optional[str] = None,
        terminal_status: Optional[str] = None,
        activate: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        explicit_request_id = str(request_id or "").strip()
        resolved_request_id = str(explicit_request_id or run_id or "").strip()
        resolved_run_id = str(run_id or resolved_request_id or "").strip()
        with self.lock:
            existing_requests = self.flow_store.document["requests"]
            prior_projection = {role: dict(status) for role, status in self.flow_statuses.items()}
            effective_activate = activate or (not explicit_request_id and resolved_request_id not in existing_requests)
            self.flow_store.patch(
                request_id=resolved_request_id,
                run_id=resolved_run_id,
                updates=updates,
                activate=effective_activate,
                parent_request_id=parent_request_id,
                goal_hash=goal_hash,
                terminal_status=terminal_status,
            )
            self.flow_statuses = self.flow_store.active_projection()
            for role, status in updates.items():
                normalized_role = normalize_role(role)
                prior = prior_projection.get(normalized_role)
                if status is None:
                    if prior is not None:
                        self.log(
                            normalized_role,
                            "FLOW_CLEARED",
                            run_id=resolved_run_id,
                            request_id=resolved_request_id,
                        )
                    continue
                projected = self.flow_statuses.get(normalized_role) or status
                evidence = {
                    key: projected.get(key)
                    for key in ("state", "logical_role", "from_role", "done_from", "sent_to")
                    if projected.get(key) not in (None, "")
                }
                prior_evidence = {
                    key: prior.get(key)
                    for key in ("state", "logical_role", "from_role", "done_from", "sent_to")
                    if prior and prior.get(key) not in (None, "")
                }
                if evidence != prior_evidence:
                    self.log(
                        normalized_role,
                        f"FLOW_{str(projected.get('state') or 'UPDATED').upper()}",
                        run_id=resolved_run_id,
                        request_id=resolved_request_id,
                        **evidence,
                    )
            return {role: dict(status) for role, status in self.flow_statuses.items()}

    def record_runner_heartbeat(self, run_id: str, request_id: str = "", pid: Optional[int] = None) -> str:
        resolved_request_id = str(request_id or run_id or "").strip()
        if not resolved_request_id:
            raise ValueError("run_id or request_id is required")
        with self.lock:
            self.runner_heartbeats[resolved_request_id] = {
                "ts": time.time(),
                "run_id": str(run_id or resolved_request_id).strip(),
                "pid": int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None,
            }
        return resolved_request_id

    def _runner_liveness(self, request_id: str, *, now: float) -> Dict[str, Any]:
        stale_after = max(1.0, float(self.config.get("runner_stale_after_s", 20) or 20))
        beat = self.runner_heartbeats.get(str(request_id or "").strip())
        if not beat:
            return {"state": "UNKNOWN", "last_heartbeat_at": None, "last_heartbeat_age_s": None, "pid": None}
        age = max(0.0, now - float(beat.get("ts") or 0.0))
        return {
            "state": "RUNNING" if age <= stale_after else "STOPPED",
            "last_heartbeat_at": _iso_from_epoch(beat.get("ts") or 0.0),
            "last_heartbeat_age_s": age,
            "pid": beat.get("pid"),
        }

    def _active_command_snapshot(self, physical_role: str, *, now: float) -> Optional[Dict[str, Any]]:
        active = self.active_command(physical_role)
        if not active:
            return None
        cmd_state = self.command_status.get(active.get("command_id"), active.get("status", "PENDING"))
        delivered_at = active.get("delivered_at")
        age = max(0.0, now - float(delivered_at)) if delivered_at else None
        return {
            "role": physical_role,
            "action": active.get("action"),
            "state": cmd_state,
            "age_s": age,
        }

    @staticmethod
    def _command_overdue(command: Optional[Dict[str, Any]], *, threshold: float) -> bool:
        if not command:
            return False
        age = command.get("age_s")
        return bool(
            str(command.get("state") or "").upper() == "DELIVERED"
            and isinstance(age, (int, float))
            and age > threshold
        )

    def derive_flow_liveness(self, request_id: Optional[str] = None, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Reconcile the stored flow projection against runner liveness and command age.

        A flow that is still marked RUNNING/WAITING but whose runner liveness is
        unavailable (UNKNOWN after restart or STOPPED after a stale heartbeat),
        or whose active command is stuck DELIVERED past the overdue window (E08),
        is reported STALLED with recovery evidence rather than trusted as live.
        """
        now = time.time() if now is None else now
        command_stale_after = max(1.0, float(self.config.get("command_stale_after_s", 120) or 120))
        with self.lock:
            document = self.flow_store.read(request_id)
            flow = document.get("flow")
            active_request_id = str(document.get("active_request_id") or "").strip()
            resolved_request_id = ""
            if flow:
                resolved_request_id = str(flow.get("request_id") or "").strip()
            if not resolved_request_id:
                resolved_request_id = str(request_id or "").strip() or active_request_id
            runner = self._runner_liveness(resolved_request_id, now=now)
            result: Dict[str, Any] = {
                "runner": runner,
                "stalled": False,
                "reason": "",
                "role": None,
                "logical_role": None,
                "last_command": None,
                "next_action": "",
            }
            if not flow or flow.get("terminal_status"):
                return result

            roles = flow.get("roles") or {}
            selected: Optional[tuple[str, Dict[str, Any]]] = None
            for physical, status in roles.items():
                if str(status.get("state") or "").upper() == "RUNNING":
                    selected = (physical, status)
                    break
            if selected is None:
                for physical, status in roles.items():
                    if str(status.get("state") or "").upper() in NON_TERMINAL_FLOW_STATES:
                        selected = (physical, status)
                        break
            if selected is None:
                return result

            physical, status = selected
            last_command = (
                self._active_command_snapshot(physical, now=now)
                if resolved_request_id == active_request_id
                else None
            )
            command_overdue = self._command_overdue(last_command, threshold=command_stale_after)
            runner_state = str(runner.get("state") or "").upper()
            runner_unavailable = runner_state in {"UNKNOWN", "STOPPED"}
            if not (runner_unavailable or command_overdue):
                result["last_command"] = last_command
                return result

            if command_overdue and last_command:
                reason = f"{last_command['action']} command did not terminate"
            elif runner_state == "STOPPED":
                reason = "runner stopped before terminalizing flow"
            elif runner_state == "UNKNOWN":
                reason = "runner heartbeat unavailable for active flow"
            else:
                reason = "flow made no progress within the liveness window"
            result.update({
                "stalled": True,
                "reason": reason,
                "role": physical,
                "logical_role": status.get("logical_role") or physical,
                "last_command": last_command,
                "next_action": "recover existing flow",
            })
            return result

    def flow_status_stalled(self, physical_role: str, flow_status: Optional[Dict[str, Any]], *, now: Optional[float] = None) -> bool:
        """Per-role stall verdict for the single-tab userscript overlay."""
        if not flow_status:
            return False
        if str(flow_status.get("state") or "").upper() not in NON_TERMINAL_FLOW_STATES:
            return False
        now = time.time() if now is None else now
        command_stale_after = max(1.0, float(self.config.get("command_stale_after_s", 120) or 120))
        with self.lock:
            active_flow = self.flow_store.read(None).get("flow")
            if not active_flow or active_flow.get("terminal_status"):
                return False
            request_id = str(active_flow.get("request_id") or "").strip()
            runner = self._runner_liveness(request_id, now=now)
            if str(runner.get("state") or "").upper() in {"UNKNOWN", "STOPPED"}:
                return True
            command = self._active_command_snapshot(normalize_role(physical_role), now=now)
            return self._command_overdue(command, threshold=command_stale_after)

    def queue_auto_open_role(self, role: str, url: str = "") -> None:
        normalized = str(role or "").strip().upper()
        if not normalized:
            return
        with self.lock:
            self.auto_open_roles[normalized] = {
                "opened_at": time.time(),
                "url": url or str(self.config.get("auto_open_url") or "https://chatgpt.com/"),
                "claimed_at": 0.0,
                "claimed_session_id": "",
            }

    def claim_auto_open_role(self, session_id: str = "") -> str:
        with self.lock:
            for role, info in list(self.auto_open_roles.items()):
                if info.get("claimed_at"):
                    continue
                info["claimed_at"] = time.time()
                info["claimed_session_id"] = session_id or ""
                self.log(role, "ROLE_CLAIMED", session_id=session_id or "")
                return role
        return ""

    @staticmethod
    def is_ignored_session(session_id: str) -> bool:
        return session_id.startswith("/backend-api/sentinel/")

    def is_current_session(self, role: str, session_id: str) -> bool:
        return bool(session_id) and session_id == self.current_sessions.get(role, "")

    def log(self, role: str, event: str, **data):
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "role": role,
            "event": event,
            **data,
        }
        with self.lock:
            self.events.append(rec)
        return rec

    def dom_summary(self, dom: Dict[str, Any]):
        if not dom:
            return {}

        messages = dom.get("messages")
        counts = None
        if isinstance(messages, dict):
            counts = messages.get("counts")

        return {
            "composer": bool(dom.get("composer")),
            "composer_text_len": dom.get("composer_text_len"),
            "composer_attachment_count": len(dom.get("composer_attachments") or []) if isinstance(dom.get("composer_attachments"), list) else 0,
            "send_enabled": dom.get("send_enabled"),
            "stop_visible": dom.get("stop_visible"),
            "voice_visible": dom.get("voice_visible"),
            "message_counts": counts,
            "image_count": counts.get("images", 0) if isinstance(counts, dict) else 0,
        }

    @staticmethod
    def dom_has_no_messages(dom: Dict[str, Any]) -> bool:
        messages = (dom or {}).get("messages")
        if not isinstance(messages, dict):
            return False
        counts = messages.get("counts") or {}
        parsed_messages = messages.get("messages")
        user_count = int(counts.get("user") or 0)
        assistant_count = int(counts.get("assistant") or 0)
        return user_count == 0 and assistant_count == 0 and isinstance(parsed_messages, list) and not parsed_messages

    def clear_transcript_cache(self, role: str) -> None:
        self.transcripts[role] = []
        self.last_user_message[role] = ""
        self.last_response[role] = ""

    def apply_dom_transcript_cache(self, role: str, dom: Dict[str, Any]) -> bool:
        messages = (dom or {}).get("messages")
        if not isinstance(messages, dict):
            return False

        parsed_messages = messages.get("messages")
        if self.dom_has_no_messages(dom):
            self.clear_transcript_cache(role)
            return True

        if isinstance(parsed_messages, list):
            self.transcripts[role] = parsed_messages

        last_user = messages.get("last_user")
        last_assistant = messages.get("last_assistant")
        self.last_user_message[role] = last_user.get("text", "") if isinstance(last_user, dict) else ""
        self.last_response[role] = last_assistant.get("text", "") if isinstance(last_assistant, dict) else ""
        return True

    def touch_presence(self, role: str, session_id: str = "") -> None:
        if session_id:
            self.sessions[role].add(session_id)
            self.current_sessions[role] = session_id
        self.status[role] = "ONLINE"
        self.role_seen_at[role] = time.time()

    def register_observation_page(
        self,
        role: str,
        page_instance_id: str,
        role_owner_id: str = "",
        role_claim_id: str = "",
    ) -> tuple[bool, str]:
        normalized_role = str(role or "").strip().upper()
        if not page_instance_id:
            return True, "legacy_missing_page_instance_id"

        current = self.observation_pages.get(normalized_role)
        identity = (str(role_owner_id or ""), str(role_claim_id or ""))
        current_identity = (
            str((current or {}).get("role_owner_id") or ""),
            str((current or {}).get("role_claim_id") or ""),
        )
        if not current or current_identity != identity:
            self.observation_pages[normalized_role] = {
                "page_instance_id": page_instance_id,
                "observation_seq": 0,
                "role_owner_id": identity[0],
                "role_claim_id": identity[1],
            }
            self.retired_observation_pages[normalized_role].clear()
            return True, "page_registered"

        current_page = str(current.get("page_instance_id") or "")
        if current_page == page_instance_id:
            return True, "page_current"
        if page_instance_id in self.retired_observation_pages[normalized_role]:
            return False, "stale_page_instance_id"

        if current_page:
            self.retired_observation_pages[normalized_role].add(current_page)
        current.update({"page_instance_id": page_instance_id, "observation_seq": 0})
        owner = self.role_owners.get(normalized_role)
        if owner and self.role_owner_matches(owner, role_owner_id, role_claim_id, page_instance_id):
            owner["page_instance_id"] = page_instance_id
        return True, "page_replaced"

    def apply_role_observation(
        self,
        role: str,
        page_instance_id: str,
        observation_seq: int,
        *,
        role_owner_id: str = "",
        role_claim_id: str = "",
        dom_info: Optional[Dict[str, Any]] = None,
        transcript: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str, int]:
        if dom_info is None and transcript is None:
            return False, "no_observation", 0

        normalized_role = str(role or "").strip().upper()
        owner = self.role_owners.get(normalized_role)
        if not self.role_owner_matches(owner, role_owner_id, role_claim_id, page_instance_id):
            return False, "role_owner_mismatch", 0

        page_current, page_reason = self.register_observation_page(
            normalized_role,
            page_instance_id,
            role_owner_id,
            role_claim_id,
        )
        if not page_current:
            return False, page_reason, 0

        current = self.observation_pages.get(normalized_role)
        last_seq = int((current or {}).get("observation_seq") or 0)
        supplied_seq = max(0, int(observation_seq or 0))
        if supplied_seq and supplied_seq <= last_seq:
            return False, "stale_observation_seq", last_seq
        accepted_seq = supplied_seq or (last_seq + 1)

        if dom_info is not None:
            self.dom_info[role] = dict(dom_info)
        snapshot_applied = self.apply_dom_transcript_cache(role, dom_info or {}) if dom_info is not None else False

        if transcript is not None and not snapshot_applied:
            messages = transcript.get("messages")
            if isinstance(messages, list):
                self.transcripts[role] = messages
            last_user = transcript.get("last_user")
            last_assistant = transcript.get("last_assistant")
            self.last_user_message[role] = last_user.get("text", "") if isinstance(last_user, dict) else ""
            self.last_response[role] = last_assistant.get("text", "") if isinstance(last_assistant, dict) else ""

        if current is None:
            current = {
                "page_instance_id": page_instance_id,
                "role_owner_id": str(role_owner_id or ""),
                "role_claim_id": str(role_claim_id or ""),
            }
            self.observation_pages[normalized_role] = current
        current["observation_seq"] = accepted_seq
        return True, "accepted", accepted_seq

    def terminalize_command(self, command_id: str, terminal_state: str, reason: str) -> Optional[Dict[str, Any]]:
        terminal_state = str(terminal_state or "").strip().upper()
        if terminal_state not in COMMAND_CANCEL_STATES:
            raise ValueError(f"unsupported command terminal state: {terminal_state or 'empty'}")
        with self.lock:
            existing = self.command_results.get(command_id)
            if existing is not None:
                return dict(existing)
            cmd = next(
                (candidate for candidate in self.commands.values() if candidate.get("command_id") == command_id),
                None,
            )
            if not cmd:
                return None
            result = {
                "role": cmd.get("role", ""),
                "state": terminal_state,
                "text": "",
                "result": {"reason": str(reason or "unspecified")},
                "dom_info": {},
                "observation_accepted": False,
                "observation_reason": "command_terminalized",
                "ts": time.time(),
            }
            self.command_status[command_id] = terminal_state
            self.command_results[command_id] = result
            self.log(
                str(cmd.get("role") or ""),
                f"COMMAND_{terminal_state}",
                command_id=command_id,
                action=cmd.get("action", ""),
                reason=result["result"]["reason"],
                owner_page_instance_id=cmd.get("owner_page_instance_id", ""),
            )
            return dict(result)

    def create_command(
        self,
        role: str,
        action: str,
        payload: Optional[dict] = None,
        command_id: str = "",
        *,
        require_online: bool = False,
    ):
        command_id = str(command_id or uuid.uuid4()).strip()
        if not command_id or len(command_id) > 160:
            raise ValueError("command_id must be a non-empty string of at most 160 characters")
        cmd = {
            "command_id": command_id,
            "role": role,
            "action": action,
            "payload": payload or {},
            "status": "PENDING",
            "created_at": time.time(),
            "delivered_at": None,
            "owner_page_instance_id": "",
            "owner_session_id": "",
        }
        with self.lock:
            if require_online:
                online, age = self.role_is_online(role)
                if not online:
                    age_text = "never seen" if age is None else f"last seen {age:.1f}s ago"
                    raise RuntimeError(
                        f"role {normalize_role(role)} bridge is offline ({age_text}); {str(action).upper()} was not queued"
                    )
            if (
                command_id in self.command_status
                or command_id in self.command_results
                or any(existing.get("command_id") == command_id for existing in self.commands.values())
            ):
                raise RuntimeError(f"command_id already exists: {command_id}")
            active = self.active_command(role)
            if active:
                if str(active.get("action") or "").upper() not in PAGE_HANDOFF_SAFE_ACTIONS:
                    raise RuntimeError(
                        f"role {role} already has active mutating command {active.get('command_id')} "
                        f"({active.get('action')})"
                    )
                self.terminalize_command(
                    str(active.get("command_id") or ""),
                    "CANCELLED",
                    "superseded_by_new_command",
                )
            self.commands[role] = cmd
            self.command_status[command_id] = "PENDING"
            self.log(role, "COMMAND_CREATED", command_id=command_id, action=action, payload=payload or {})
        return cmd

    def wait_for_command_result(self, command_id: str, timeout_s: float, poll_s: float = 0.2) -> Optional[Dict[str, Any]]:
        deadline = time.time() + max(0.1, timeout_s)
        while time.time() < deadline:
            with self.lock:
                result = self.command_results.get(command_id)
                if result is not None:
                    return dict(result)
            time.sleep(max(0.05, poll_s))
        return None

    def active_command(self, role: str) -> Optional[Dict[str, Any]]:
        cmd = self.commands.get(role)
        if not cmd or cmd["command_id"] in self.command_results:
            return None
        return cmd

    @staticmethod
    def command_owner_matches(cmd: Dict[str, Any], page_instance_id: str) -> bool:
        return bool(page_instance_id) and cmd.get("owner_page_instance_id") == page_instance_id

    def get_command_for_role(self, role: str, session_id: str = "", page_instance_id: str = ""):
        with self.lock:
            cmd = self.active_command(role)
            if not cmd or not page_instance_id:
                return {"action": "WAIT"}

            command_id = cmd["command_id"]
            if cmd["status"] == "PENDING" and not cmd.get("owner_page_instance_id"):
                cmd["owner_page_instance_id"] = page_instance_id
                cmd["owner_session_id"] = session_id
                cmd["status"] = "DELIVERED"
                cmd["delivered_at"] = time.time()
                self.command_status[command_id] = "DELIVERED"
                self.log(
                    role,
                    "COMMAND_DELIVERED",
                    command_id=command_id,
                    action=cmd["action"],
                    page_instance_id=page_instance_id,
                    session_id=session_id,
                )

            if cmd["status"] == "DELIVERED" and self.command_owner_matches(cmd, page_instance_id):
                return cmd
            return {"action": "WAIT"}

    def save_report(self, report: ReportRequest):
        role = report.role
        command_id = report.command_id or ""
        report_state = report.state
        ignored_session = self.is_ignored_session(report.session_id)

        with self.lock:
            owner = self.role_owners.get(str(role or "").strip().upper())
            if not self.role_owner_matches(owner, report.role_owner_id, report.role_claim_id, report.page_instance_id):
                return {"status": "IGNORED", "reason": "role_owner_mismatch", "config": self.config}

            active = self.active_command(role)
            ignore_reason = ""
            if ignored_session:
                ignore_reason = "ignored_session"
            elif command_id:
                if not report.page_instance_id:
                    ignore_reason = "missing_page_instance_id"
                elif not active or active.get("command_id") != command_id:
                    ignore_reason = "stale_command"
                elif not self.command_owner_matches(active, report.page_instance_id):
                    ignore_reason = "command_owner_mismatch"
            elif active:
                ignore_reason = "missing_command_id"

            if ignore_reason:
                self.log(
                    role,
                    "REPORT_IGNORED",
                    reason=ignore_reason,
                    session_id=report.session_id,
                    page_instance_id=report.page_instance_id,
                    command_id=command_id,
                    state=report_state,
                )
                return {"status": "IGNORED", "reason": ignore_reason, "config": self.config}

            command_owned = bool(command_id and active)
            current_session = self.is_current_session(role, report.session_id)
            if command_owned or current_session:
                self.touch_presence(role, report.session_id)

            observation_accepted, observation_reason, accepted_seq = self.apply_role_observation(
                role,
                report.page_instance_id,
                report.observation_seq,
                role_owner_id=report.role_owner_id,
                role_claim_id=report.role_claim_id,
                dom_info=report.dom_info,
            )

            if command_id:
                self.command_status[command_id] = report_state

            self.log(
                role,
                report_state,
                session_id=report.session_id,
                page_instance_id=report.page_instance_id,
                command_id=command_id,
                text_preview=(report.text or "")[:500],
                result=report.result,
                observation_accepted=observation_accepted,
                observation_reason=observation_reason,
                observation_seq=accepted_seq,
                dom_summary=self.dom_summary(report.dom_info or {}),
            )

            if command_id and (report_state in TERMINAL_STATES or report_state.startswith("ERROR_")):
                self.command_results[command_id] = {
                    "role": role,
                    "state": report_state,
                    "text": report.text,
                    "result": report.result,
                    "dom_info": report.dom_info if observation_accepted else {},
                    "observation_accepted": observation_accepted,
                    "observation_reason": observation_reason,
                    "ts": time.time(),
                }

        return {
            "status": "OK",
            "config": self.config,
            "observation_accepted": observation_accepted,
            "observation_reason": observation_reason,
            "observation_seq": accepted_seq,
        }

    def save_sync(self, req: SyncRequest):
        role = req.role
        transcript = req.transcript
        snapshot = req.snapshot
        ignored_session = self.is_ignored_session(req.session_id)

        with self.lock:
            owner = self.role_owners.get(str(role or "").strip().upper())
            if not self.role_owner_matches(owner, req.role_owner_id, req.role_claim_id, req.page_instance_id):
                return {"status": "IGNORED", "reason": "role_owner_mismatch", "config": self.config}

            active = self.active_command(role)
            ignore_reason = ""
            if ignored_session:
                ignore_reason = "ignored_session"
            elif active and not req.page_instance_id:
                ignore_reason = "missing_page_instance_id"
            elif active and not self.command_owner_matches(active, req.page_instance_id):
                ignore_reason = "command_owner_mismatch"

            if ignore_reason:
                self.log(
                    role,
                    "SYNC_IGNORED",
                    reason=ignore_reason,
                    session_id=req.session_id,
                    page_instance_id=req.page_instance_id,
                )
                return {"status": "IGNORED", "reason": ignore_reason, "config": self.config}

            observation_accepted, observation_reason, accepted_seq = self.apply_role_observation(
                role,
                req.page_instance_id,
                req.observation_seq,
                role_owner_id=req.role_owner_id,
                role_claim_id=req.role_claim_id,
                dom_info=snapshot,
                transcript=transcript,
            )

            if active or self.is_current_session(role, req.session_id):
                self.touch_presence(role, req.session_id)

            self.log(
                role,
                "SYNC",
                session_id=req.session_id,
                page_instance_id=req.page_instance_id,
                reason=req.reason,
                counts=(transcript or {}).get("counts", {}),
                last_user_preview=self.last_user_message[role][:300],
                last_assistant_preview=self.last_response[role][:300],
                observation_accepted=observation_accepted,
                observation_reason=observation_reason,
                observation_seq=accepted_seq,
                dom_summary=self.dom_summary(snapshot or {}),
            )

        return {
            "status": "OK",
            "config": self.config,
            "observation_accepted": observation_accepted,
            "observation_reason": observation_reason,
            "observation_seq": accepted_seq,
        }


state = DiagnosticState()


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    active_state = state
    active_state.task_scheduler.start()
    try:
        yield
    finally:
        active_state.task_scheduler.stop()


app = FastAPI(title="MAuto Browser Bridge Server", lifespan=app_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)




@app.post("/api/status")
def api_status(req: StatusRequest):
    role = req.role
    with state.lock:
        if state.is_ignored_session(req.session_id):
            return {"command": {"action": "WAIT"}, "config": state.config}

        normalized_role = str(role or "").strip().upper()
        owner = state.role_owners.get(normalized_role)
        same_claim = bool(
            owner and state.role_owner_matches(owner, req.role_owner_id, req.role_claim_id, req.page_instance_id)
        )
        retired_same_claim = bool(
            same_claim and req.page_instance_id in state.retired_observation_pages.get(normalized_role, set())
        )
        if req.claim_role and normalized_role and req.page_instance_id:
            if (
                not owner
                or (same_claim and not retired_same_claim)
                or state.claim_is_newer(req.role_claim_id, owner.get("role_claim_id", ""))
            ):
                state.role_owners[normalized_role] = {
                    "page_instance_id": req.page_instance_id,
                    "session_id": req.session_id,
                    "role_owner_id": req.role_owner_id,
                    "role_claim_id": req.role_claim_id,
                }
                owner = state.role_owners[normalized_role]

        clear_role = not state.role_owner_matches(owner, req.role_owner_id, req.role_claim_id, req.page_instance_id)
        active_before_poll = state.active_command(role)
        command_locked_to_other_page = bool(
            active_before_poll
            and active_before_poll.get("status") == "DELIVERED"
            and not state.command_owner_matches(active_before_poll, req.page_instance_id)
        )
        if (
            command_locked_to_other_page
            and same_claim
            and str(active_before_poll.get("action") or "").upper() in PAGE_HANDOFF_SAFE_ACTIONS
        ):
            state.terminalize_command(
                str(active_before_poll.get("command_id") or ""),
                "CANCELLED",
                "owner_page_replaced",
            )
            active_before_poll = None
            command_locked_to_other_page = False
        page_current, page_reason = (False, "role_owner_mismatch")
        if retired_same_claim:
            page_reason = "stale_page_instance_id"
        elif not clear_role and not command_locked_to_other_page:
            page_current, page_reason = state.register_observation_page(
                role,
                req.page_instance_id,
                req.role_owner_id,
                req.role_claim_id,
            )
        if page_reason == "stale_page_instance_id":
            clear_role = True

        cmd = (
            state.get_command_for_role(role, req.session_id, req.page_instance_id)
            if not clear_role and page_current and not command_locked_to_other_page
            else {"action": "WAIT"}
        )
        active = state.active_command(role)
        owner_only = bool(active and active.get("status") == "DELIVERED")
        may_update = bool(
            not clear_role
            and page_current
            and (not owner_only or state.command_owner_matches(active, req.page_instance_id))
        )

        observation_accepted = False
        observation_reason = page_reason
        accepted_seq = 0
        if may_update:
            state.touch_presence(role, req.session_id)
            observation_accepted, observation_reason, accepted_seq = state.apply_role_observation(
                role,
                req.page_instance_id,
                req.observation_seq,
                role_owner_id=req.role_owner_id,
                role_claim_id=req.role_claim_id,
                dom_info=req.dom_info,
            )

        flow_status = None if clear_role else (dict(state.flow_statuses.get(normalized_role) or {}) or None)
        if flow_status is not None:
            flow_status["stalled"] = state.flow_status_stalled(normalized_role, flow_status)
    return {
        "command": cmd,
        "config": state.config,
        "flow_status": flow_status,
        "clear_role": clear_role,
        "observation_accepted": observation_accepted,
        "observation_reason": observation_reason,
        "observation_seq": accepted_seq,
    }


@app.post("/api/claim-role")
def api_claim_role(req: RoleClaimRequest):
    role = state.claim_auto_open_role(req.session_id)
    return {"role": role, "config": state.config}


@app.post("/api/reserve-role-claim")
def api_reserve_role_claim(req: RoleClaimReservationRequest):
    return {"role_claim_id": state.reserve_role_claim(req.role), "config": state.config}


@app.post("/api/release-role")
def api_release_role(req: RoleReleaseRequest):
    role = str(req.role or "").strip().upper()
    with state.lock:
        owner = state.role_owners.get(role)
        released = bool(owner and state.role_owner_matches(owner, req.role_owner_id, req.role_claim_id, req.page_instance_id))
        if released:
            state.role_owners.pop(role, None)
            # A successful explicit release removes live presence immediately.
            # Historical diagnostic caches remain readable through
            # /api/admin/role/{role}, but no longer keep the role in Live roles.
            state.status.pop(role, None)
            state.role_seen_at.pop(role, None)
            state.current_sessions.pop(role, None)
    return {"released": released}


@app.post("/api/report")
def api_report(req: ReportRequest):
    return state.save_report(req)


@app.post("/api/sync")
def api_sync(req: SyncRequest):
    return state.save_sync(req)


@app.post("/api/admin/command")
def api_admin_command(req: AdminCommandRequest):
    role = normalize_role(req.role)
    action = str(req.action or "").strip()
    normalized_action = action.upper()
    try:
        cmd = state.create_command(
            role,
            action,
            req.payload,
            require_online=normalized_action in BRIDGE_REQUIRED_ADMIN_ACTIONS,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"command": cmd}


@app.post("/api/admin/command/{command_id}/cancel")
def api_admin_command_cancel(command_id: str, req: AdminCommandCancelRequest):
    try:
        result = state.terminalize_command(command_id, req.state, req.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown command: {command_id}")
    return {
        "command_id": command_id,
        "status": result["state"],
        "done": True,
        "result": result,
    }


@app.post("/api/admin/flow-status")
def api_admin_flow_status(req: FlowStatusRequest):
    try:
        flow_statuses = state.update_flow_statuses(
            req.run_id,
            req.updates,
            request_id=req.request_id,
            parent_request_id=req.parent_request_id,
            goal_hash=req.goal_hash,
            terminal_status=req.terminal_status,
            activate=req.activate,
        )
    except FlowStoreMutationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "OK", "flow_statuses": flow_statuses, "flow": state.flow_store.read(req.request_id or req.run_id)}


@app.post("/api/admin/flow-heartbeat")
def api_admin_flow_heartbeat(req: FlowHeartbeatRequest):
    try:
        request_id = state.record_runner_heartbeat(req.run_id, req.request_id, req.pid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "OK", "request_id": request_id}


@app.get("/api/admin/flow")
def api_admin_flow(request_id: str = ""):
    document = state.flow_store.read(request_id or None)
    document["liveness"] = state.derive_flow_liveness(request_id or None)
    return document


def _validated_task_body(model_type, body: Any):
    try:
        return model_type.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_task_request", "errors": exc.errors()}) from exc


def _task_error(exc: Exception):
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": str(exc.args[0])}) from exc
    if isinstance(exc, TaskConflictError):
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    if isinstance(exc, TaskStoreMutationError):
        raise HTTPException(status_code=409, detail={"code": "task_store_unavailable", "message": str(exc)}) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail={"code": "invalid_task_request", "message": str(exc)}) from exc
    raise exc


def _task_mutation_response(task: Dict[str, Any]):
    return {"task": task, "store_revision": state.task_store.document["revision"]}


def _normalized_dashboard_launch(req: TaskLaunchRequest) -> Dict[str, Any]:
    prompt = bounded_text(req.prompt, field="prompt", limit=200000, allow_empty=False)
    controller_role = normalize_role(req.controller_role, field="controller_role")
    logical_roles = [normalize_role(role, field="logical_roles") for role in req.logical_roles]
    if not logical_roles:
        raise ValueError("logical_roles must be a non-empty array")
    if len(set(logical_roles)) != len(logical_roles):
        raise ValueError("logical_roles must be unique")
    physical_role_map = {
        normalize_role(logical, field="physical_role_map key"): normalize_role(physical, field="physical_role_map value")
        for logical, physical in req.physical_role_map.items()
    }
    if set(physical_role_map) != set(logical_roles):
        raise ValueError("physical_role_map keys must exactly match logical_roles")
    if physical_role_map[logical_roles[0]] != controller_role:
        raise ValueError("controller_role must match the first logical role mapping")
    finish_roles = [normalize_role(role, field="finish_roles") for role in req.finish_roles]
    if not finish_roles:
        raise ValueError("finish_roles must be a non-empty array")
    if len(set(finish_roles)) != len(finish_roles):
        raise ValueError("finish_roles must be unique")
    if not set(finish_roles).issubset(logical_roles):
        raise ValueError("finish_roles must be a subset of logical_roles")
    return {
        "controller_role": controller_role,
        "prompt": prompt,
        "logical_roles": logical_roles,
        "physical_role_map": {role: physical_role_map[role] for role in logical_roles},
        "finish_roles": finish_roles,
        "execution_options": normalize_execution_options(req.execution_options),
    }


def _dashboard_launch_env() -> Dict[str, str]:
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_INTERNAL__PYTHONHOME", "UV_RUN_RECURSION_DEPTH"):
        env.pop(name, None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _start_dashboard_process(task: Dict[str, Any]) -> Dict[str, Any]:
    physical_roles = list(dict.fromkeys(task["physical_role_map"][role] for role in task["logical_roles"]))
    unavailable = {}
    for role in physical_roles:
        reason = TaskScheduler._readiness_error(state.controller_readiness(role))
        if reason:
            unavailable[role] = reason
    offline = sorted(role for role, reason in unavailable.items() if reason == "controller_offline")
    if offline:
        raise TaskConflictError("role_offline", f"offline physical roles: {', '.join(offline)}")
    if unavailable:
        detail = ", ".join(f"{role}:{reason}" for role, reason in sorted(unavailable.items()))
        raise TaskConflictError("role_not_ready", detail)

    env = _dashboard_launch_env()
    uv_executable = shutil.which("uv", path=env.get("PATH"))
    if not uv_executable:
        raise OSError("uv executable is unavailable")
    run_id = f"dashboard-{uuid.uuid4().hex}"
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    command = build_launch_command(task)
    argv = build_launch_argv(task, uv_executable)
    DASHBOARD_LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = DASHBOARD_LAUNCH_DIR / f"{run_id}.out.log"
    stderr_path = DASHBOARD_LAUNCH_DIR / f"{run_id}.err.log"

    with state.lock:
        for existing_id, record in list(state.dashboard_processes.items()):
            if record["process"].poll() is not None:
                state.dashboard_processes.pop(existing_id, None)
        requested = set(physical_roles)
        for record in state.dashboard_processes.values():
            if requested.intersection(record["physical_roles"]):
                raise TaskConflictError("controller_busy", "a dashboard-launched main.py process already owns one of these roles")

        stdout_handle = stdout_path.open("ab", buffering=0)
        stderr_handle = stderr_path.open("ab", buffering=0)
        try:
            popen_args = {
                "cwd": CONTROL_REPOSITORY,
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": stdout_handle,
                "stderr": stderr_handle,
                "shell": False,
                "close_fds": True,
            }
            if os.name == "nt":
                popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                popen_args["start_new_session"] = True
            process = subprocess.Popen(argv, **popen_args)
        finally:
            stdout_handle.close()
            stderr_handle.close()

        state.dashboard_processes[run_id] = {
            "process": process,
            "physical_roles": set(physical_roles),
            "started_at": started_at,
        }

    time.sleep(0.15)
    return_code = process.poll()
    if return_code is not None:
        with state.lock:
            state.dashboard_processes.pop(run_id, None)
        tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-800:].strip() if stderr_path.exists() else ""
        raise RuntimeError(f"main.py exited immediately with code {return_code}: {tail or 'no stderr'}")

    return {
        "run_id": run_id,
        "pid": process.pid,
        "started_at": started_at,
        "command": command,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "physical_roles": physical_roles,
    }


@app.get("/api/admin/tasks")
def api_admin_tasks(include_archived: bool = False):
    payload = state.task_store.read(include_archived=include_archived)
    payload["scheduler"] = state.task_scheduler.health()
    return payload


@app.get("/api/admin/tasks/{task_id}")
def api_admin_task(task_id: str):
    task = state.task_store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": task_id})
    return _task_mutation_response(task)


@app.post("/api/admin/tasks")
def api_admin_task_create(body: Any = Body(...)):
    req = _validated_task_body(TaskCreateRequest, body)
    try:
        if req.status.strip().upper() not in {"BACKLOG", "READY", "BLOCKED"}:
            raise ValueError("new tasks must start in BACKLOG, READY, or BLOCKED")
        task = state.task_store.create(req.model_dump())
        return _task_mutation_response(task)
    except (KeyError, TaskConflictError, TaskStoreMutationError, ValueError) as exc:
        _task_error(exc)


@app.post("/api/admin/tasks/launch")
def api_admin_task_launch(body: Any = Body(...)):
    req = _validated_task_body(TaskLaunchRequest, body)
    try:
        task = _normalized_dashboard_launch(req)
        return {"status": "STARTED", "run": _start_dashboard_process(task)}
    except (KeyError, TaskConflictError, TaskStoreMutationError, ValueError) as exc:
        _task_error(exc)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "launch_failed", "message": str(exc)},
        ) from exc


@app.patch("/api/admin/tasks/{task_id}")
def api_admin_task_patch(task_id: str, body: Any = Body(...)):
    req = _validated_task_body(TaskPatchRequest, body)
    try:
        task = state.task_store.patch(
            task_id,
            req.expected_revision,
            req.changes,
            actor_role=req.actor_role,
            wake_resolution=req.wake_resolution,
        )
        state.task_scheduler.wake()
        return _task_mutation_response(task)
    except (KeyError, TaskConflictError, TaskStoreMutationError, ValueError) as exc:
        _task_error(exc)


@app.post("/api/admin/tasks/{task_id}/move")
def api_admin_task_move(task_id: str, body: Any = Body(...)):
    req = _validated_task_body(TaskMoveRequest, body)
    try:
        task = state.task_store.move(task_id, req.expected_revision, req.status, actor_role=req.actor_role)
        state.task_scheduler.wake()
        return _task_mutation_response(task)
    except (KeyError, TaskConflictError, TaskStoreMutationError, ValueError) as exc:
        _task_error(exc)


@app.post("/api/admin/tasks/{task_id}/wake")
def api_admin_task_wake(task_id: str, body: Any = Body(...)):
    req = _validated_task_body(TaskRevisionRequest, body)
    try:
        return _task_mutation_response(state.task_scheduler.request_manual(task_id, req.expected_revision))
    except (KeyError, TaskConflictError, TaskStoreMutationError, ValueError) as exc:
        _task_error(exc)


@app.post("/api/admin/tasks/{task_id}/pause")
def api_admin_task_pause(task_id: str, body: Any = Body(...)):
    req = _validated_task_body(TaskRevisionRequest, body)
    try:
        return _task_mutation_response(state.task_store.pause(task_id, req.expected_revision))
    except (KeyError, TaskConflictError, TaskStoreMutationError, ValueError) as exc:
        _task_error(exc)


@app.post("/api/admin/tasks/{task_id}/resume")
def api_admin_task_resume(task_id: str, body: Any = Body(...)):
    req = _validated_task_body(TaskRevisionRequest, body)
    try:
        task = state.task_store.resume(task_id, req.expected_revision)
        state.task_scheduler.wake()
        return _task_mutation_response(task)
    except (KeyError, TaskConflictError, TaskStoreMutationError, ValueError) as exc:
        _task_error(exc)


@app.get("/api/admin/roles")
def api_admin_roles():
    return {"roles": state.role_inventory()}


@app.get("/dashboard", response_class=FileResponse)
def dashboard():
    return FileResponse(DASHBOARD_PATH, media_type="text/html")


@app.get("/api/admin/config")
def api_admin_config_get():
    with state.lock:
        return {"config": dict(state.config)}


@app.post("/api/admin/config")
def api_admin_config_update(req: AdminConfigRequest):
    return {"config": state.update_config(req.config)}


def api_route_catalog(base_url: str = ""):
    def item(group: str, method: str, path: str, purpose: str, sample_path: str = ""):
        sample_path = sample_path or path
        sample = f"{base_url}{sample_path}" if base_url else sample_path
        return {
            "group": group,
            "method": method,
            "path": path,
            "sample": sample,
            "purpose": purpose,
        }

    return [
        item("client", "POST", "/api/status", "Browser role poll/status and command delivery."),
        item("client", "POST", "/api/claim-role", "Claim a queued browser role without URL role params."),
        item("client", "POST", "/api/report", "Browser command result/report ingestion."),
        item("client", "POST", "/api/sync", "Transcript and DOM snapshot sync."),
        item("admin", "POST", "/api/admin/command", "Create a command for a role."),
        item("admin", "POST", "/api/admin/command/{command_id}/cancel", "Terminalize a stranded command as CANCELLED or EXPIRED."),
        item("admin", "POST", "/api/admin/flow-status", "Atomically patch durable request-keyed semantic role flow state."),
        item("admin", "POST", "/api/admin/flow-heartbeat", "Record a runner liveness heartbeat for a flow request so stalls surface as STALLED."),
        item("admin", "GET", "/api/admin/flow", "Read durable flow state with derived runner liveness and stall evidence.", "/api/admin/flow?request_id=demo-request-id"),
        item("tasks", "GET", "/api/admin/tasks", "Read durable Kanban tasks and scheduler health."),
        item("tasks", "GET", "/api/admin/tasks/{task_id}", "Read one durable task.", "/api/admin/tasks/task-demo"),
        item("tasks", "POST", "/api/admin/tasks", "Create a validated dashboard task."),
        item("tasks", "POST", "/api/admin/tasks/launch", "Launch the generated role.py or main.py command for a clean set of online browser roles."),
        item("tasks", "PATCH", "/api/admin/tasks/{task_id}", "Optimistically update task metadata/result or resolve a wake."),
        item("tasks", "POST", "/api/admin/tasks/{task_id}/move", "Optimistically move or claim a Kanban task."),
        item("tasks", "POST", "/api/admin/tasks/{task_id}/wake", "Reserve one manual wake occurrence."),
        item("tasks", "POST", "/api/admin/tasks/{task_id}/pause", "Pause task scheduling."),
        item("tasks", "POST", "/api/admin/tasks/{task_id}/resume", "Resume and recompute task scheduling."),
        item("tasks", "GET", "/api/admin/roles", "Read generic userscript role inventory and task reservations."),
        item("admin", "GET", "/api/admin/command/{command_id}", "Read command status/result.", "/api/admin/command/demo-command-id"),
        item("admin", "GET", "/api/admin/role/{role}", "Read role snapshot/cache.", "/api/admin/role/A"),
        item("admin", "GET", "/api/admin/role/{role}/timeline", "Read semantic role lifecycle events without raw poll noise.", "/api/admin/role/A/timeline?limit=20"),
        item("admin", "GET", "/api/admin/events", "Read the separate raw diagnostic event log.", "/api/admin/events?role=A&limit=20"),
        item("admin", "GET", "/api/admin/config", "Read runtime config."),
        item("admin", "POST", "/api/admin/config", "Update runtime config."),
        item("admin", "GET", "/api/admin/routes", "List available server endpoints."),
        item("presentation", "GET", "/dashboard", "Polling physical-role board with live flow, current-task evidence, and command preview."),
    ]


@app.get("/api/admin/routes")
def api_admin_routes():
    return {"routes": api_route_catalog()}


@app.get("/api/admin/command/{command_id}")
def api_admin_command_result(command_id: str):
    with state.lock:
        result = state.command_results.get(command_id)
        status = state.command_status.get(command_id, "UNKNOWN")

    return {
        "command_id": command_id,
        "status": status,
        "done": result is not None,
        "result": result,
    }


@app.get("/api/admin/role/{role}")
def api_admin_role(role: str, response_limit: int = 100):
    normalized_role = str(role or "").strip().upper()
    with state.lock:
        seen_at = float(state.role_seen_at.get(normalized_role, 0.0) or 0.0)
        seen_age_s = max(0.0, time.time() - seen_at) if seen_at else None
        poll_s = max(0.1, float(state.config.get("poll_ms", 800) or 800) / 1000.0)
        online = seen_age_s is not None and seen_age_s <= max(10.0, poll_s * 5.0)
        presence_status = "ONLINE" if online else "OFFLINE"
        sessions = sorted(state.sessions.get(normalized_role, set()))
        dom_info = state.dom_info.get(normalized_role, {})
        last_user = state.last_user_message.get(normalized_role, "")
        last_response = state.last_response.get(normalized_role, "")
        all_responses = state.response_projection(normalized_role)
        bounded_response_limit = max(1, min(int(response_limit), 500))
        responses = all_responses[-bounded_response_limit:]
        flow_status = dict(state.flow_statuses.get(normalized_role) or {})
        tasks = state.task_store.list_tasks(include_archived=False)
        current_task = next(
            (
                task for task in tasks
                if task_reserves_controller(task)
                and (
                    task.get("controller_role") == normalized_role
                    or normalized_role in set((task.get("physical_role_map") or {}).values())
                )
            ),
            None,
        )
        observation_state = dict(state.observation_pages.get(normalized_role) or {})
        active = state.active_command(normalized_role)
        active_command = None
        if active:
            command_id = str(active.get("command_id") or "")
            active_command = {
                "command_id": command_id,
                "action": active.get("action"),
                "state": state.command_status.get(command_id, active.get("status", "PENDING")),
                "owner_page_instance_id": active.get("owner_page_instance_id", ""),
            }

        presence = {
            "online": online,
            "status": presence_status,
            "last_seen_at": seen_at or None,
            "last_seen_age_s": seen_age_s,
            "sessions": sessions,
        }
        observation = {
            "page_instance_id": observation_state.get("page_instance_id", ""),
            "observation_seq": int(observation_state.get("observation_seq") or 0),
            "dom_info": dom_info,
            "last_user": last_user,
            "last_response": last_response,
        }
        return {
            "role": normalized_role,
            "configured_role": state.configured_role_for(normalized_role),
            "current_logical_role": flow_status.get("logical_role") or None,
            "turn": len(all_responses),
            "responses": responses,
            "message_counts": state.dom_summary(dom_info).get("message_counts") or {},
            "current_task_id": current_task.get("task_id") if current_task else None,
            "current_request_id": flow_status.get("run_id") or (current_task.get("active_request_id") if current_task else None),
            "flow_status": flow_status or None,
            "presence": presence,
            "observation": observation,
            "active_command": active_command,
            # Compatibility aliases retained for BridgeClient and legacy tooling.
            "status": presence_status,
            "online": online,
            "last_seen_at": seen_at or None,
            "last_seen_age_s": seen_age_s,
            "sessions": sessions,
            "dom_info": dom_info,
            "last_user": last_user,
            "last_response": last_response,
        }


@app.get("/api/admin/role/{role}/timeline")
def api_admin_role_timeline(role: str, limit: int = 100):
    try:
        return state.role_timeline(role, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/events")
def api_admin_events(role: str = "", contains: str = "", command_id: str = "", limit: int = 50):
    with state.lock:
        events = list(state.events)

    if role:
        events = [event for event in events if event.get("role") == role]
    if contains:
        events = [event for event in events if contains in event.get("event", "")]
    if command_id:
        events = [event for event in events if event.get("command_id") == command_id]

    limit = max(1, min(limit, 500))
    return {"events": events[-limit:]}


def find_pid_on_port(host: str, port: int) -> Optional[int]:
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    target_suffixes = {
        f"{host}:{port}",
        f"0.0.0.0:{port}",
        f"[::]:{port}",
        f"[::1]:{port}",
    }

    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local_address = parts[1]
        state_name = parts[3].upper()
        if state_name != "LISTENING":
            continue
        if local_address not in target_suffixes:
            continue
        try:
            return int(parts[4])
        except ValueError:
            return None

    return None


def ensure_port_available(host: str, port: int, wait_s: float = 1.0) -> None:
    pid = find_pid_on_port(host, port)
    if pid is None:
        return

    print(f"[MAuto] port {port} is busy, stopping PID {pid}")
    os.kill(pid, signal.SIGTERM)
    time.sleep(wait_s)

    remaining_pid = find_pid_on_port(host, port)
    if remaining_pid is not None:
        raise RuntimeError(f"Port {port} is still busy after stopping PID {pid}; current owner={remaining_pid}")


def log_startup_routes(base_url: str) -> None:
    print(f"[MAuto] server starting on {base_url}")
    print("[MAuto] backend API:")
    for route in api_route_catalog(base_url):
        print(
            f"  [{route['group']}] {route['method']:4s} {route['path']:<32s} "
            f"sample: {route['sample']}"
        )


def run_server():
    ensure_port_available(SERVER_HOST, SERVER_PORT)
    base = f"http://{SERVER_HOST}:{SERVER_PORT}"
    log_startup_routes(base)
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="error")


if __name__ == "__main__":
    run_server()
