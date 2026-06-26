"""
agent_core.py - Shared helpers for run.ipynb cells and solo.py.

Load via:
  import agent_core as core                # in scripts
  exec(open('agent_core.py').read())       # in legacy notebooks
"""

import json
import os
import re as _re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
BASE_URL    = "http://127.0.0.1:8500"
PROMPTS_DIR = Path("prompts")

PROBE_TIMEOUT_S      = 600
SET_PROMPT_TIMEOUT_S = 1200
CLICK_TIMEOUT_S      = 600
ASSISTANT_TIMEOUT_S  = 30000
SYNC_TIMEOUT_S       = 600

SEND_MAX_RETRIES = 5
# Attach each role prompt once per process run. Later asks carry runtime context only.
ROLE_PROMPT_EVERY_N_ASKS = 0
ROLE_ASK_COUNTS: dict[str, int] = {}

# Set per-run by each cell/script before calling routing helpers.
ACTIVE_ROLES: list[str] = []

# ============================================================
# HTTP layer
# ============================================================
_HTTP_PROXY  = os.environ.get("HTTP_PROXY", "")
_HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_EXT_OPENER = (
    urllib.request.build_opener(urllib.request.ProxyHandler({
        "http":  _HTTP_PROXY or _HTTPS_PROXY,
        "https": _HTTPS_PROXY or _HTTP_PROXY,
    })) if (_HTTP_PROXY or _HTTPS_PROXY) else _NO_PROXY
)


def _opener(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return _NO_PROXY if host in {"127.0.0.1", "localhost", "::1"} else _EXT_OPENER


def http_json(method, path, payload=None, timeout=300, retries=5, retry_wait_s=1.0):
    url  = f"{BASE_URL}{path}"
    data, headers = None, {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    last_exc = None
    for attempt in range(retries + 1):
        try:
            with _opener(url).open(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                body = ""
            retryable = e.code in (502, 503, 504)
            last_exc = RuntimeError(f"HTTP {e.code} {e.reason} {method} {path} body={body}")
            if retryable and attempt < retries:
                time.sleep(retry_wait_s * (attempt + 1))
                continue
            raise last_exc from e
        except urllib.error.URLError as e:
            last_exc = RuntimeError(f"Cannot connect {url}: {e.reason}")
            if attempt < retries:
                time.sleep(retry_wait_s * (attempt + 1))
                continue
            raise last_exc from e
    raise last_exc or RuntimeError(f"Unknown error {method} {path}")


def send_command(role, action, payload=None):
    return http_json("POST", "/api/admin/command", {
        "role": role, "action": action, "payload": payload or {},
    })["command"]


def wait_command(command_id, timeout=300, print_every=2.0):
    started, last_print = time.time(), 0.0
    last_state = None
    while True:
        data = http_json("GET", f"/api/admin/command/{command_id}")
        now  = time.time()
        state = (data.get("status"), data.get("done"))
        # Only print when status changes to avoid noisy repeated logs.
        if (now - last_print >= print_every) and (state != last_state or data.get("done")):
            print(f"[{time.strftime('%H:%M:%S')}] cmd={command_id[:8]} status={data['status']} done={data['done']}")
            last_print = now
            last_state = state
        if data["done"]:
            return data["result"]
        if timeout and (now - started) > timeout:
            raise TimeoutError(f"Timeout waiting command_id={command_id}")
        time.sleep(1.0)


def run_command(role, action, payload=None, timeout=300, print_every=2.0):
    cmd = send_command(role, action, payload)
    print(f"{action} -> role={role}, command_id={cmd['command_id']}")
    return wait_command(cmd["command_id"], timeout=timeout, print_every=print_every)


def try_reset_page(role):
    for action in ["RESET_PAGE", "RELOAD_PAGE", "HARD_RELOAD", "RELOAD"]:
        try:
            res = run_command(role, action, timeout=90, print_every=1.0)
            print(f"Reset OK: {action}")
            return {"ok": True, "action": action, "result": res}
        except Exception as e:
            print(f"Reset skip: {action} -> {e}")
    return {"ok": False, "action": None, "result": None}


def prompt_role_candidates(role):
    role = str(role or "").upper().strip()
    candidates = []
    if role:
        candidates.append(role)
    base = _re.sub(r"\d+$", "", role).strip()
    if base and base not in candidates:
        candidates.append(base)
    return candidates


def load_prompt(role):
    for prompt_role in prompt_role_candidates(role):
        for suffix in [".txt", ".json"]:
            p = PROMPTS_DIR / f"{prompt_role}{suffix}"
            if p.exists():
                if suffix == ".txt":
                    return p.read_text(encoding="utf-8").strip()
                data = json.loads(p.read_text(encoding="utf-8"))
                return str(data.get("prompt", "")).strip()
    candidates = ", ".join(f"prompts/{item}.txt" for item in prompt_role_candidates(role))
    raise FileNotFoundError(f"Missing: {candidates}")

# ============================================================
# Role / session helpers
# ============================================================

def log_roles_status(roles: list) -> None:
    print("\n[roles status]")
    for role in roles:
        try:
            snap = http_json("GET", f"/api/admin/role/{urllib.parse.quote(role)}")
            status = snap.get("status", "?")
            sess = snap.get("sessions", 0)
            last_r = (snap.get("last_response") or "")[:120].replace("\n", " ")
            marker = "OK" if status not in ("OFFLINE", "ERROR") else "!!"
            tail = f" | last: {last_r}..." if last_r else ""
            print(f"  [{marker}] {role:10s} status={status} sessions={sess}{tail}")
        except Exception as e:
            print(f"  [??] {role:10s} error: {e}")
    print()


def open_new_chat(role, wait_s=3.0):
    for action in ["NEW_CHAT", "NAVIGATE_NEW"]:
        try:
            res = run_command(role, action, timeout=30, print_every=1.0)
            if str(res.get("state") or res.get("status") or "").upper() == "UNKNOWN_COMMAND":
                raise RuntimeError("command is not supported by browser controller")
            print(f"[new_chat] {action} OK")
            if wait_s > 0:
                time.sleep(wait_s)
            return {"ok": True, "action": action, "result": res}
        except Exception as e:
            print(f"[new_chat] {action} skip: {e}")
    for action in ["RELOAD_PAGE", "HARD_RELOAD", "RELOAD"]:
        try:
            res = run_command(role, action, timeout=30, print_every=1.0)
            if str(res.get("state") or res.get("status") or "").upper() == "UNKNOWN_COMMAND":
                raise RuntimeError("command is not supported by browser controller")
            print(f"[new_chat] fallback {action} OK")
            if wait_s > 0:
                time.sleep(wait_s)
            return {"ok": True, "action": action, "result": res}
        except Exception as e:
            print(f"[new_chat] fallback {action} skip: {e}")
    return {"ok": False, "action": None, "result": None}

# ============================================================
# Prompt cadence helpers
# ============================================================

def should_attach_role_prompt(role: str) -> bool:
    asks = ROLE_ASK_COUNTS.get(role, 0)
    return asks == 0


def build_role_prompt(
    role: str,
    prompt_text: str,
    turn_index: int,
    *,
    persona_file: bool = False,
    from_coordinator: bool = False,
    include_system_rules: bool = False,
) -> str:
    parts = []
    if persona_file:
        parts.append(load_prompt(role))
        if include_system_rules:
            parts.append(
                "SYSTEM RULES:\n"
                "- End with exactly one fenced JSON object unless the task is complete.\n"
                "- The JSON object must contain exactly: target, reason, message.\n"
                "- target MUST be one value from ALLOWED_TARGETS.\n"
                "- Keep reasoning concise."
            )
    if from_coordinator:
        parts.append("[FROM COORDINATOR]")

    parts.append(f"ALLOWED_TARGETS: {', '.join(ACTIVE_ROLES)}")
    parts.append(f"CURRENT TURN: {turn_index}")
    parts.append(prompt_text)
    return "\n\n".join(parts)


def run_agent_prompt(role, prompt_text, do_reset=False, new_chat=False,
                     new_chat_wait_s=6.0, timeout_s=ASSISTANT_TIMEOUT_S, print_every=5.0):
    if role not in ACTIVE_ROLES:
        raise ValueError(f"Invalid role={role!r}, must be one of {ACTIVE_ROLES}")

    if new_chat:
        open_new_chat(role, wait_s=new_chat_wait_s)
    elif do_reset:
        try_reset_page(role)

    result = {}
    result["probe"] = run_command(role, "PROBE", timeout=PROBE_TIMEOUT_S, print_every=print_every)
    result["set_prompt"] = run_command(
        role, "SET_PROMPT",
        {"text": prompt_text, "method": "auto", "samples": 6, "sample_ms": 300},
        timeout=SET_PROMPT_TIMEOUT_S, print_every=print_every,
    )

    last_click_error = None
    for attempt in range(SEND_MAX_RETRIES + 1):
        try:
            result["find_send"] = run_command(role, "FIND_SEND", timeout=PROBE_TIMEOUT_S, print_every=print_every)
            result["click_send"] = run_command(role, "CLICK_SEND", timeout=CLICK_TIMEOUT_S, print_every=print_every)
            if result["click_send"].get("state") == "SEND_ACCEPTED":
                last_click_error = None
                break
            last_click_error = RuntimeError(f"CLICK_SEND returned state={result['click_send'].get('state')}")
        except Exception as e:
            last_click_error = e
            print(f"[send_retry] attempt {attempt + 1}/{SEND_MAX_RETRIES} failed: {e}")

        if attempt < SEND_MAX_RETRIES:
            print(f"[send_retry] reset + repaste before retry {attempt + 2}/{SEND_MAX_RETRIES + 1}")
            try_reset_page(role)
            time.sleep(6.0)
            result["set_prompt"] = run_command(
                role, "SET_PROMPT",
                {"text": prompt_text, "method": "auto", "samples": 6, "sample_ms": 300},
                timeout=SET_PROMPT_TIMEOUT_S, print_every=print_every,
            )
        else:
            raise RuntimeError(f"CLICK_SEND failed after {SEND_MAX_RETRIES} retries: {last_click_error}") from last_click_error

    result["assistant"] = run_command(role, "WAIT_ASSISTANT_DONE", timeout=timeout_s, print_every=print_every)
    if result["assistant"].get("state") != "ASSISTANT_DONE":
        raise RuntimeError(f"WAIT_ASSISTANT_DONE failed: state={result['assistant'].get('state')}")

    result["sync"] = run_command(role, "SYNC_TRANSCRIPT", {"reason": "ask"}, timeout=SYNC_TIMEOUT_S, print_every=print_every)
    result["response_text"] = (result["assistant"].get("text") or "").strip()
    return result


def fetch_live_response(role: str) -> str:
    try:
        sync = run_command(role, "SYNC_TRANSCRIPT", {"reason": "resume_from_web"}, timeout=SYNC_TIMEOUT_S, print_every=1.0)
        for k in ("last_assistant_text", "assistant_text", "response_text", "text"):
            txt = str(sync.get(k) or "").strip()
            if txt:
                return txt
    except Exception as e:
        print(f"[resume] SYNC_TRANSCRIPT skip ({role}): {e}")

    try:
        snap = http_json("GET", f"/api/admin/role/{urllib.parse.quote(role)}")
        for k in ("last_response", "last_assistant_text", "response_text"):
            txt = str(snap.get(k) or "").strip()
            if txt:
                return txt
    except Exception as e:
        print(f"[resume] role snapshot skip ({role}): {e}")

    return ""


def run_turn(role, prompt, turn_index: int, *, persona_file=True, from_coordinator=False,
             resume_from_web=True, do_reset=False, new_chat=False, timeout_s=ASSISTANT_TIMEOUT_S):
    if resume_from_web:
        live_response = fetch_live_response(role)
        if live_response:
            print(f"[resume] using live response for {role}")
            print(live_response[:600] + ("..." if len(live_response) > 600 else ""))
            return {"mode": "resume", "role": role, "response": live_response}

    attach_persona = bool(persona_file and should_attach_role_prompt(role))
    if attach_persona:
        next_ask = ROLE_ASK_COUNTS.get(role, 0) + 1
        print(f"[prompt] attach {role} system prompt at ask #{next_ask}")

    full_prompt = build_role_prompt(
        role,
        prompt,
        turn_index,
        persona_file=attach_persona,
        from_coordinator=from_coordinator,
        include_system_rules=attach_persona,
    )
    response = run_agent_prompt(
        role=role,
        prompt_text=full_prompt,
        do_reset=do_reset,
        new_chat=new_chat,
        timeout_s=timeout_s,
    )["response_text"]

    if persona_file:
        ROLE_ASK_COUNTS[role] = ROLE_ASK_COUNTS.get(role, 0) + 1

    return {"mode": "ask", "role": role, "response": response}

# ============================================================
# Routing helpers
# ============================================================

def parse_routing(text: str) -> dict | None:
    for candidate in iter_json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict) and "target" in parsed:
            return parsed
    return None


def iter_json_candidates(text: str) -> list[str]:
    text = text or ""
    candidates = []
    fenced = list(_re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, _re.DOTALL | _re.IGNORECASE))
    for match in reversed(fenced):
        candidates.extend(extract_balanced_json_objects(match.group(1)))
    candidates.extend(extract_balanced_json_objects(text))
    seen = set()
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            unique.append(candidate)
            seen.add(candidate)
    return unique


def extract_balanced_json_objects(text: str) -> list[str]:
    objects = []
    start = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text or ""):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start:index + 1])
                start = None
    return list(reversed(objects))


def normalize_routing_target(target: str) -> str:
    raw = (target or "").strip().upper()
    if not raw:
        return ""
    compact = _re.sub(r"[\s_\-]+", "", raw)
    if compact == "FINISH":
        return "FINISH"
    return raw

def first_status_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip().upper()
        if s:
            return s
    return ""


def is_task_complete(text: str) -> bool:
    routing = parse_routing(text)
    if not routing:
        return False
    return normalize_routing_target(routing.get("target", "")) == "FINISH"

def is_changes_requested(text: str) -> bool:
    return first_status_line(text).startswith("CHANGES REQUESTED")


def is_need_info(text: str) -> bool:
    return first_status_line(text).startswith("NEED INFO")


def is_goal_complete(text: str) -> bool:
    return first_status_line(text).startswith("GOAL COMPLETE")
