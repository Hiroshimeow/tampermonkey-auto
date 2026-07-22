from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from apps.task_scheduler import build_launch_command


ROOT = Path(__file__).resolve().parents[1]


def dashboard_command(payload: dict) -> str:
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('dashboard.html', 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
function extractFunction(name) {
  const marker = `function ${name}`;
  let start = script.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const brace = script.indexOf('{', start);
  let depth = 0;
  let quote = '';
  let escaped = false;
  for (let index = brace; index < script.length; index += 1) {
    const char = script[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = '';
      continue;
    }
    if (char === "'" || char === '"' || char === '`') { quote = char; continue; }
    if (char === '{') depth += 1;
    if (char === '}' && --depth === 0) return script.slice(start, index + 1);
  }
  throw new Error(`unterminated ${name}`);
}
const context = {};
vm.runInNewContext([
  extractFunction('commandNumber'),
  extractFunction('quoteCli'),
  extractFunction('buildMainCommand'),
  extractFunction('buildRoleCommand'),
  extractFunction('buildLaunchCommand'),
  'globalThis.buildLaunchCommand = buildLaunchCommand;'
].join('\n'), context);
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(context.buildLaunchCommand(
  payload.logical_roles,
  payload.physical_role_map,
  payload.finish_roles,
  payload.execution_options,
  payload.prompt
));
"""
    result = subprocess.run(
        ["node", "-e", node_script],
        cwd=ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    "prompt",
    [
        'say "hello" then inspect C:\\temp\\',
        "trailing slash \\",
        "Unicode task: kiểm tra role C2 → C3",
    ],
)
def test_compact_dashboard_command_contains_only_visible_command_inputs(prompt: str) -> None:
    command = dashboard_command(
        {
            "logical_roles": ["DEV", "REVIEW", "PLAN"],
            "physical_role_map": {"DEV": "C2", "REVIEW": "C3", "PLAN": "C2"},
            "finish_roles": ["PLAN"],
            "execution_options": {
                "timeout": 1800,
                "request_timeout": 1200,
                "parallelism": 4,
                "max_turns": 0,
                "reload_after": 10,
            },
            "prompt": prompt,
        }
    )

    backend_command = build_launch_command(
        {
            "logical_roles": ["DEV", "REVIEW", "PLAN"],
            "physical_role_map": {"DEV": "C2", "REVIEW": "C3", "PLAN": "C2"},
            "finish_roles": ["PLAN"],
            "execution_options": {
                "timeout": 1800,
                "request_timeout": 1200,
                "parallelism": 4,
                "max_turns": 0,
                "reload_after": 10,
                "new_chat_on_handoff": False,
                "handoff_command_policy": "auto",
            },
            "prompt": prompt,
        }
    )

    assert command == backend_command
    assert command.startswith("uv run main.py ")
    for fragment in (
        '--role "DEV,REVIEW,PLAN"',
        '--browser-roles "C2,C3"',
        '--role-map "DEV=C2 REVIEW=C3 PLAN=C2"',
        '--finish-roles "PLAN"',
        "--timeout 1800",
        "--request-timeout 1200",
        "--parallelism 4",
        "--max-turns 0",
        "--reload-after 10",
        "--goal ",
    ):
        assert fragment in command

    for forbidden in (
        "--title",
        "--target-root",
        "--branch",
        "--controller-role",
        "--status",
        "--new-chat-on-handoff",
        "--handoff-command-policy",
    ):
        assert forbidden not in command


def test_single_role_dashboard_command_uses_direct_role_runner() -> None:
    payload = {
        "logical_roles": ["C2"],
        "physical_role_map": {"C2": "C2"},
        "finish_roles": ["C2"],
        "execution_options": {
            "timeout": 1800,
            "request_timeout": 1200,
            "parallelism": 4,
            "max_turns": 0,
            "reload_after": 10,
        },
        "prompt": "chỉ cần nói ok.",
    }
    command = dashboard_command(payload)
    backend_command = build_launch_command(
        {
            **payload,
            "execution_options": {
                **payload["execution_options"],
                "new_chat_on_handoff": False,
                "handoff_command_policy": "auto",
            },
        }
    )

    assert command == backend_command
    assert command == (
        'uv run role.py --role "C2" --timeout 1800 --request-timeout 1200 '
        '--prompt "chỉ cần nói ok."'
    )
    for forbidden in ("main.py", "--browser-roles", "--role-map", "--finish-roles", "--parallelism", "--max-turns", "--reload-after", "--goal"):
        assert forbidden not in command
