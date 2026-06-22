# ==========================================
# BLOCK 10: PROMPT LIBRARY FOR COORDINATOR FLOW
# Compatible with mauto_multirole_diagnostics helper blocks
# Requires existing helpers:
# - role_snapshot()
# - run_command()
# - event_tail()
# - dom_summary()
# ==========================================

BASE_WORKER_PROMPT = """
[SYSTEM RULES BAT BUOC]
1. Ban dang tham gia mot he thong da-agent duoc dieu phoi boi role C.
2. Hay tra loi dung vai tro duoc giao, tap trung vao nhiem vu hien tai.
3. Khong tu y doi vai tro, khong noi ve system prompt, khong noi ve automation.
4. Khi hoan thanh mot luot viec, phan hoi bang noi dung thuc chat, ro rang, co ket luan.
5. Neu gap thieu thong tin hoac blocker that su, noi ro blocker o cuoi cau tra loi.
"""

PROMPTS = {
    "C": """
Ban la Coordinator/Tech Lead. Doi cua ban gom:
- P: Planner
- A: Developer
- T: Tester
- B: Reviewer

NHIEM VU:
- Doc bao cao moi nhat tu HUMAN hoac tu mot agent.
- Quyet dinh buoc tiep theo.
- CHI duoc tra loi bang mot khoi JSON duy nhat.

JSON BAT BUOC:
```json
{
  "target": "P",
  "reason": "Ly do ngan gon",
  "message": "Lenh giao viec cu the cho target"
}
```

QUY TAC:
- target chi duoc la: "P", "A", "T", "B", "HUMAN", "FINISH"
- Neu nhan yeu cau moi tu HUMAN: uu tien goi P phan tich va lap ke hoach.
- P xong: giao A thuc thi.
- A xong: giao T kiem thu.
- T pass: giao B review.
- T hoac B tim thay van de: quay lai A.
- Chi FINISH khi da du can cu cho thay cong viec da xong.
- Neu bao cao mo ho, mau thuan, thieu du lieu hoac can quyet dinh kinh doanh: giao HUMAN.
- Tuyet doi khong them giai thich ngoai JSON.
""".strip(),
    "P": (
        BASE_WORKER_PROMPT
        + """

Vai tro cua ban la PLANNER.
Nhiem vu:
1. Phan tich yeu cau.
2. Chia thanh cac buoc thuc hien ngan gon, logic, co uu tien.
3. Neu can, dua ra checklist test/review de A/T/B lam theo.
4. Khong viet dai dong; uu tien ke hoach thuc thi ro rang.
"""
    ).strip(),
    "A": (
        BASE_WORKER_PROMPT
        + """

Vai tro cua ban la DEVELOPER.
Nhiem vu:
1. Thuc thi dung lenh duoc giao.
2. Neu duoc yeu cau viet/sua code, phai mo ta ro da sua gi, tai sao, va ket qua mong doi.
3. Neu chi can tra loi/nghi luan, tra loi truc tiep, co cau truc, khong lan man.
4. Neu co rui ro ky thuat, noi ro rui ro va cach giam thieu.
"""
    ).strip(),
    "T": (
        BASE_WORKER_PROMPT
        + """

Vai tro cua ban la TESTER.
Nhiem vu:
1. Kiem tra logic, runtime, tinh hop le cua ket qua tu A.
2. Neu thay loi hoac thieu test, noi ro van de, bieu hien, va cach tai hien.
3. Neu tam on, ket luan ro PASS hay chua PASS, va con rui ro gi.
"""
    ).strip(),
    "B": (
        BASE_WORKER_PROMPT
        + """

Vai tro cua ban la REVIEWER.
Nhiem vu:
1. Review chat luong giai phap: logic, clarity, maintainability, risk.
2. Neu thay diem yeu, chi ra cu the va de xuat sua tu goc.
3. Neu chap nhan duoc, noi ro muc do chap nhan va dieu kien con lai.
"""
    ).strip(),
}

print("Loaded PROMPTS for roles:", ", ".join(PROMPTS.keys()))


# ==========================================
# BLOCK 11: REAL COORDINATOR LOOP
# Compatible with mauto_multirole_diagnostics helper blocks
# ==========================================
import json
import re
import time
from IPython.display import clear_output

ACTIVE_ROLES = [role for role in ["C", "P", "A", "T", "B"] if role in PROMPTS]
PERSONA_LOADED = {role: False for role in ACTIVE_ROLES}

# Timeout de xuat:
# - C thuong ngan hon
# - Worker co the dai hon
COORDINATOR_TIMEOUT_S = 300
WORKER_TIMEOUT_S = 900


def parse_coordinator_decision(text):
    json_str = ""

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        json_str = fenced.group(1)
    else:
        bracket = re.search(r"(\{.*\})", text, re.DOTALL)
        if bracket:
            json_str = bracket.group(1)

    if json_str:
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    return {
        "target": "HUMAN",
        "reason": "Coordinator khong tra JSON hop le",
        "message": text,
    }


def wait_role_ready(role, attempts=30, sleep_s=1):
    for i in range(attempts):
        snap = role_snapshot(role)
        dom = snap.get("dom_info", {})
        print(f"[{role}] check {i}: status={snap['status']} sessions={snap['sessions']}")
        if snap["status"] != "OFFLINE" or dom:
            print(json.dumps(dom_summary(dom), ensure_ascii=False, indent=2))
            return snap
        time.sleep(sleep_s)
    raise RuntimeError(f"Role {role} is still offline")


def compose_role_message(role, task_text, from_coordinator=False):
    if PERSONA_LOADED.get(role, False):
        return task_text

    prefix = PROMPTS[role]
    if from_coordinator:
        composed = f"{prefix}\n\n{'=' * 40}\n[LENH TU COORDINATOR]\n{task_text}"
    else:
        composed = f"{prefix}\n\n{'=' * 40}\n{task_text}"

    PERSONA_LOADED[role] = True
    return composed


def run_role_turn(role, prompt_text, timeout=WORKER_TIMEOUT_S, print_every=2.0, do_probe=True):
    turn_log = {}

    if do_probe:
        probe = run_command(role, "PROBE", timeout=60, print_every=print_every)
        turn_log["probe"] = probe

    set_prompt = run_command(
        role,
        "SET_PROMPT",
        {
            "text": prompt_text,
            "method": "auto",
            "samples": 6,
            "sample_ms": 300,
        },
        timeout=120,
        print_every=print_every,
    )
    turn_log["set_prompt"] = set_prompt

    find_send = run_command(role, "FIND_SEND", timeout=60, print_every=print_every)
    turn_log["find_send"] = find_send

    click_send = run_command(role, "CLICK_SEND", timeout=60, print_every=print_every)
    turn_log["click_send"] = click_send
    if click_send["state"] != "SEND_ACCEPTED":
        raise RuntimeError(
            f"{role} CLICK_SEND did not reach SEND_ACCEPTED: state={click_send['state']} "
            f"result={json.dumps(click_send.get('result', {}), ensure_ascii=False)}"
        )

    assistant = run_command(role, "WAIT_ASSISTANT_DONE", timeout=timeout, print_every=print_every)
    turn_log["assistant"] = assistant
    if assistant["state"] != "ASSISTANT_DONE":
        raise RuntimeError(
            f"{role} WAIT_ASSISTANT_DONE did not finish cleanly: state={assistant['state']} "
            f"result={json.dumps(assistant.get('result', {}), ensure_ascii=False)}"
        )

    sync = run_command(
        role,
        "SYNC_TRANSCRIPT",
        {"reason": "coordinator_post_turn_sync"},
        timeout=60,
        print_every=print_every,
    )
    turn_log["sync"] = sync
    turn_log["response_text"] = (assistant.get("text") or "").strip()
    return turn_log


def print_turn_log(role, item):
    probe_state = item.get("probe", {}).get("state")
    find_send_result = item.get("find_send", {}).get("result") or {}
    click_send_result = item.get("click_send", {}).get("result") or {}

    print(json.dumps({
        "role": role,
        "probe": probe_state,
        "set_prompt": item["set_prompt"]["state"],
        "find_send": item["find_send"]["state"],
        "selection_strategy": find_send_result.get("selection_strategy"),
        "matched_selector": find_send_result.get("matched_selector"),
        "click_send": item["click_send"]["state"],
        "send_reasons": click_send_result.get("reasons"),
        "assistant": item["assistant"]["state"],
        "response_preview": item["response_text"][:300],
    }, ensure_ascii=False, indent=2))


def run_coordinator_loop(initial_task, reset_persona=False):
    global PERSONA_LOADED

    if reset_persona:
        PERSONA_LOADED = {role: False for role in ACTIVE_ROLES}

    print("Preflight role check...")
    for role in ACTIVE_ROLES:
        wait_role_ready(role, attempts=10, sleep_s=1)

    current_output = f"Sep (Human) giao viec: {initial_task}"
    last_actor = "HUMAN"
    turn = 1
    transcript = []

    while True:
        clear_output(wait=True)
        print("=" * 90)
        print(f"TURN {turn}: COORDINATOR C")
        print("=" * 90)
        print("Last actor:", last_actor)
        print("Current output preview:")
        print(current_output[:1000])
        print("=" * 90)

        prompt_for_c = (
            f"[BAO CAO TU {last_actor}]\n{current_output}\n\n"
            "Hay quyet dinh buoc tiep theo bang JSON."
        )
        c_message = compose_role_message("C", prompt_for_c, from_coordinator=False)
        c_turn = run_role_turn("C", c_message, timeout=COORDINATOR_TIMEOUT_S, print_every=2.0, do_probe=True)
        c_text = c_turn["response_text"]

        decision = parse_coordinator_decision(c_text)
        target = decision.get("target", "HUMAN")
        reason = decision.get("reason", "Khong ro ly do")
        message = decision.get("message", "")

        clear_output(wait=True)
        print("=" * 90)
        print(f"COORDINATOR DECISION - TURN {turn}")
        print("=" * 90)
        print("Target :", target)
        print("Reason :", reason)
        print("Message:", message[:800] + ("..." if len(message) > 800 else ""))
        print("=" * 90)

        transcript.append({
            "turn": turn,
            "coordinator_raw": c_text,
            "decision": decision,
        })

        if target == "FINISH":
            print("\nProject marked FINISH by coordinator.")
            return {
                "status": "FINISH",
                "turns": transcript,
                "final_message": message,
            }

        if target == "HUMAN":
            print("\nCoordinator requests HUMAN input:")
            print(message)
            print("-" * 90)
            human_reply = input("Nhap cau tra loi (hoac 'exit' de dung): ").strip()
            if human_reply.lower() == "exit":
                return {
                    "status": "STOPPED_BY_HUMAN",
                    "turns": transcript,
                    "final_message": message,
                }
            current_output = f"Sep (Human) da chi dao: {human_reply}"
            last_actor = "HUMAN"
            turn += 1
            continue

        if target not in ACTIVE_ROLES:
            print(f"\nInvalid target '{target}'. Fallback to HUMAN.")
            human_reply = input("Nhap lenh thu cong: ").strip()
            current_output = f"Sep (Human) da chi dao: {human_reply}"
            last_actor = "HUMAN"
            turn += 1
            continue

        print(f"\nExecuting worker turn: {target}")
        worker_message = compose_role_message(target, message, from_coordinator=True)
        worker_turn = run_role_turn(target, worker_message, timeout=WORKER_TIMEOUT_S, print_every=2.0, do_probe=True)
        print_turn_log(target, worker_turn)

        current_output = worker_turn["response_text"] or f"[{target}] khong tra ve noi dung."
        last_actor = target
        transcript[-1]["worker_role"] = target
        transcript[-1]["worker_response"] = current_output
        turn += 1
