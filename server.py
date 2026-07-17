import os
import signal

import subprocess
import threading
import time
import uuid

from collections import defaultdict, deque
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8500


TERMINAL_STATES = {
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
    "UNKNOWN_COMMAND",
    "ERROR_COMMAND",
}


class StatusRequest(BaseModel):
    role: str
    session_id: str = ""
    dom_info: Dict[str, Any] = Field(default_factory=dict)


class ReportRequest(BaseModel):
    role: str
    session_id: str = ""
    command_id: str = ""
    state: str
    text: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)
    dom_info: Dict[str, Any] = Field(default_factory=dict)


class SyncRequest(BaseModel):
    role: str
    session_id: str = ""
    reason: str = ""
    transcript: Dict[str, Any] = Field(default_factory=dict)
    snapshot: Dict[str, Any] = Field(default_factory=dict)


class AdminCommandRequest(BaseModel):
    role: str
    action: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class AdminConfigRequest(BaseModel):
    config: Dict[str, Any] = Field(default_factory=dict)


class RoleClaimRequest(BaseModel):
    session_id: str = ""


class FlowStatusRequest(BaseModel):
    run_id: str
    updates: Dict[str, Optional[Dict[str, Any]]] = Field(default_factory=dict)


class DiagnosticState:
    def __init__(self):
        self.lock = threading.RLock()
        self.commands = {}
        self.command_results = {}
        self.command_status = {}
        self.status = defaultdict(lambda: "OFFLINE")
        self.sessions = defaultdict(set)
        self.dom_info = defaultdict(dict)
        self.transcripts = defaultdict(list)
        self.last_user_message = defaultdict(str)
        self.last_response = defaultdict(str)
        self.events = deque(maxlen=10000)
        self.role_seen_at = defaultdict(float)
        self.flow_statuses = {}
        self.flow_run_order = {}
        self.flow_role_order = {}
        self.next_flow_run_order = 0
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
        }

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.config.update(updates)
            return dict(self.config)

    def update_flow_statuses(
        self,
        run_id: str,
        updates: Dict[str, Optional[Dict[str, Any]]],
    ) -> Dict[str, Dict[str, Any]]:
        owner = str(run_id or "").strip()
        if not owner:
            return {}
        changed = {}
        with self.lock:
            owner_order = self.flow_run_order.get(owner)
            if owner_order is None:
                self.next_flow_run_order += 1
                owner_order = self.next_flow_run_order
                self.flow_run_order[owner] = owner_order
            for raw_role, raw_status in updates.items():
                role = str(raw_role or "").strip().upper()
                if not role:
                    continue
                if owner_order < self.flow_role_order.get(role, 0):
                    continue
                if raw_status is None:
                    current = self.flow_statuses.get(role)
                    if current and current.get("run_id") == owner:
                        self.flow_statuses.pop(role, None)
                        self.flow_role_order[role] = owner_order
                    changed[role] = dict(self.flow_statuses.get(role) or {})
                    continue
                state_name = str(raw_status.get("state") or "").strip().upper()
                if state_name not in {"RUNNING", "WAITING", "DONE"}:
                    continue
                record = {"run_id": owner, "state": state_name}
                detail_label = str(raw_status.get("detail_label") or "").strip()
                detail_role = str(raw_status.get("detail_role") or "").strip()[:80]
                if detail_label in {"From", "Routed"} and detail_role:
                    record["detail_label"] = detail_label
                    record["detail_role"] = detail_role
                self.flow_statuses[role] = record
                self.flow_role_order[role] = owner_order
                changed[role] = dict(record)
        return changed

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

    def create_command(self, role: str, action: str, payload: Optional[dict] = None):
        command_id = str(uuid.uuid4())
        cmd = {
            "command_id": command_id,
            "role": role,
            "action": action,
            "payload": payload or {},
            "status": "PENDING",
            "created_at": time.time(),
            "delivered_at": None,
        }
        with self.lock:
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

    def get_command_for_role(self, role: str):
        with self.lock:
            cmd = self.commands.get(role)
            if not cmd:
                return {"action": "WAIT"}

            command_id = cmd["command_id"]
            if command_id in self.command_results:
                return {"action": "WAIT"}

            if cmd["status"] == "PENDING":
                cmd["status"] = "DELIVERED"
                cmd["delivered_at"] = time.time()
                self.command_status[command_id] = "DELIVERED"
                self.log(role, "COMMAND_DELIVERED", command_id=command_id, action=cmd["action"])

            return cmd

    def save_report(self, report: ReportRequest):
        role = report.role
        command_id = report.command_id or ""
        report_state = report.state
        ignored_session = self.is_ignored_session(report.session_id)
        empty_dom = self.dom_has_no_messages(report.dom_info)

        with self.lock:
            if not ignored_session:
                self.status[role] = report_state
                self.role_seen_at[role] = time.time()

            if report.session_id:
                self.sessions[role].add(report.session_id)

            if report.dom_info and not ignored_session:
                self.dom_info[role] = report.dom_info
                self.apply_dom_transcript_cache(role, report.dom_info)

            if command_id:
                self.command_status[command_id] = report_state

            if (
                not ignored_session
                and not empty_dom
                and report_state in {"ASSISTANT_DONE", "TRANSCRIPT_SAVE_ACK", "TRANSCRIPT_SAVED"}
                and report.text
            ):
                self.last_response[role] = report.text

            self.log(
                role,
                report_state,
                session_id=report.session_id,
                command_id=command_id,
                text_preview=(report.text or "")[:500],
                result=report.result,
                dom_summary=self.dom_summary(report.dom_info),
            )

            if command_id and (report_state in TERMINAL_STATES or report_state.startswith("ERROR_")):
                self.command_results[command_id] = {
                    "role": role,
                    "state": report_state,
                    "text": report.text,
                    "result": report.result,
                    "dom_info": report.dom_info,
                    "ts": time.time(),
                }

        return {"status": "OK", "config": self.config}

    def save_sync(self, req: SyncRequest):
        role = req.role
        transcript = req.transcript or {}
        snapshot = req.snapshot or {}
        messages = transcript.get("messages", [])
        last_user = transcript.get("last_user")
        last_assistant = transcript.get("last_assistant")
        ignored_session = self.is_ignored_session(req.session_id)
        empty_snapshot = self.dom_has_no_messages(snapshot)
        snapshot_applied = False

        with self.lock:
            if not ignored_session:
                self.role_seen_at[role] = time.time()

            if req.session_id:
                self.sessions[role].add(req.session_id)

            if snapshot and not ignored_session:
                self.dom_info[role] = snapshot
                snapshot_applied = self.apply_dom_transcript_cache(role, snapshot)

            if isinstance(messages, list) and not ignored_session and not empty_snapshot and not snapshot_applied:
                self.transcripts[role] = messages

            if not ignored_session and not empty_snapshot and not snapshot_applied:
                self.last_user_message[role] = last_user.get("text", "") if isinstance(last_user, dict) else ""

            if not ignored_session and not empty_snapshot and not snapshot_applied:
                self.last_response[role] = last_assistant.get("text", "") if isinstance(last_assistant, dict) else ""

            self.log(
                role,
                "SYNC",
                session_id=req.session_id,
                reason=req.reason,
                counts=transcript.get("counts", {}),
                last_user_preview=self.last_user_message[role][:300],
                last_assistant_preview=self.last_response[role][:300],
                dom_summary=self.dom_summary(snapshot),
            )

        return {"status": "OK", "config": self.config}


state = DiagnosticState()

app = FastAPI(title="MAuto Browser Bridge Server")

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
        if req.session_id:
            state.sessions[role].add(req.session_id)
        if req.dom_info and not state.is_ignored_session(req.session_id):
            state.dom_info[role] = req.dom_info
            state.apply_dom_transcript_cache(role, req.dom_info)
        if not state.is_ignored_session(req.session_id):
            state.status[role] = "ONLINE"
            state.role_seen_at[role] = time.time()

    if state.is_ignored_session(req.session_id):
        return {"command": {"action": "WAIT"}, "config": state.config}

    cmd = state.get_command_for_role(role)
    with state.lock:
        flow_status = dict(state.flow_statuses.get(str(role or "").strip().upper()) or {}) or None
    return {"command": cmd, "config": state.config, "flow_status": flow_status}


@app.post("/api/claim-role")
def api_claim_role(req: RoleClaimRequest):
    role = state.claim_auto_open_role(req.session_id)
    return {"role": role, "config": state.config}


@app.post("/api/report")
def api_report(req: ReportRequest):
    return state.save_report(req)


@app.post("/api/sync")
def api_sync(req: SyncRequest):
    return state.save_sync(req)


@app.post("/api/admin/command")
def api_admin_command(req: AdminCommandRequest):
    cmd = state.create_command(req.role, req.action, req.payload)
    return {"command": cmd}


@app.post("/api/admin/flow-status")
def api_admin_flow_status(req: FlowStatusRequest):
    return {"status": "OK", "flow_statuses": state.update_flow_statuses(req.run_id, req.updates)}


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
        item("admin", "POST", "/api/admin/flow-status", "Update run-scoped browser role flow status."),
        item("admin", "GET", "/api/admin/command/{command_id}", "Read command status/result.", "/api/admin/command/demo-command-id"),
        item("admin", "GET", "/api/admin/role/{role}", "Read role snapshot/cache.", "/api/admin/role/A"),
        item("admin", "GET", "/api/admin/events", "Read recent event log.", "/api/admin/events?role=A&limit=20"),
        item("admin", "GET", "/api/admin/config", "Read runtime config."),
        item("admin", "POST", "/api/admin/config", "Update runtime config."),
        item("admin", "GET", "/api/admin/routes", "List available server endpoints."),
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
def api_admin_role(role: str):
    with state.lock:
        seen_at = float(state.role_seen_at.get(role, 0.0) or 0.0)
        seen_age_s = max(0.0, time.time() - seen_at) if seen_at else None
        poll_s = max(0.1, float(state.config.get("poll_ms", 800) or 800) / 1000.0)
        online = seen_age_s is not None and seen_age_s <= max(10.0, poll_s * 5.0)
        return {
            "role": role,
            "status": state.status.get(role, "OFFLINE"),
            "online": online,
            "last_seen_at": seen_at or None,
            "last_seen_age_s": seen_age_s,
            "sessions": sorted(state.sessions.get(role, set())),
            "dom_info": state.dom_info.get(role, {}),
            "last_user": state.last_user_message.get(role, ""),
            "last_response": state.last_response.get(role, ""),
        }


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







