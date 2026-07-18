import os
import signal

import subprocess
import threading
import time
import uuid
import re

from collections import defaultdict, deque
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from apps.flow_store import DEFAULT_FLOW_PATH, FlowStore, FlowStoreMutationError


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
    "ROLE_SET",
    "ROLE_TAKEOVER_RELOADING",
    "ROLE_TAKEOVER_FAILED",
    "UNKNOWN_COMMAND",
    "ERROR_COMMAND",
}


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


class DiagnosticState:
    def __init__(self, flow_path=DEFAULT_FLOW_PATH):
        self.lock = threading.RLock()
        self.commands = {}
        self.command_results = {}
        self.command_status = {}
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
            return {role: dict(status) for role, status in self.flow_statuses.items()}

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
            "owner_page_instance_id": "",
            "owner_session_id": "",
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
    return {"released": released}


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


@app.get("/api/admin/flow")
def api_admin_flow(request_id: str = ""):
    return state.flow_store.read(request_id or None)


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
        item("admin", "POST", "/api/admin/flow-status", "Atomically patch durable request-keyed semantic role flow state."),
        item("admin", "GET", "/api/admin/flow", "Read durable active or request-specific semantic flow state.", "/api/admin/flow?request_id=demo-request-id"),
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
