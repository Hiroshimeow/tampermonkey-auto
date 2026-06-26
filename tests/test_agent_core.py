import importlib.util
from pathlib import Path


def load_agent_core_module():
    path = Path(__file__).resolve().parents[1] / "agents.py"
    spec = importlib.util.spec_from_file_location("agents_core_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_open_new_chat_skips_unknown_command(monkeypatch):
    core = load_agent_core_module()
    calls = []

    def fake_run_command(role, action, timeout=30, print_every=1.0):
        calls.append(action)
        if action == "NEW_CHAT":
            return {"state": "UNKNOWN_COMMAND"}
        return {"state": "NAVIGATED"}

    monkeypatch.setattr(core, "run_command", fake_run_command)
    monkeypatch.setattr(core.time, "sleep", lambda *_args: None)

    result = core.open_new_chat("SOLO", wait_s=0)

    assert calls == ["NEW_CHAT", "NAVIGATE_NEW"]
    assert result["ok"] is True
    assert result["action"] == "NAVIGATE_NEW"


def test_load_prompt_falls_back_to_base_role_for_numbered_instances(tmp_path, monkeypatch):
    core = load_agent_core_module()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "DEV.txt").write_text("DEV SYSTEM", encoding="utf-8")
    monkeypatch.setattr(core, "PROMPTS_DIR", prompts)

    assert core.load_prompt("DEV1") == "DEV SYSTEM"


def test_load_prompt_prefers_exact_instance_prompt(tmp_path, monkeypatch):
    core = load_agent_core_module()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "DEV.txt").write_text("DEV SYSTEM", encoding="utf-8")
    (prompts / "DEV1.txt").write_text("DEV1 SYSTEM", encoding="utf-8")
    monkeypatch.setattr(core, "PROMPTS_DIR", prompts)

    assert core.load_prompt("DEV1") == "DEV1 SYSTEM"
