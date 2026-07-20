from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from apps.task_scheduler import build_main_command
from apps.task_store import normalize_execution_options


ROOT = Path(__file__).resolve().parents[1]


def dashboard_command(task: dict) -> str:
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('dashboard.html', 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
function extractFunction(name) {
  const marker = `function ${name}`;
  let start = script.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const asyncStart = Math.max(0, start - 6);
  if (script.slice(asyncStart, start) === 'async ') start = asyncStart;
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
    if (char === "'" || char === '"' || char === '`') {
      quote = char;
      continue;
    }
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
  'globalThis.buildMainCommand = buildMainCommand;'
].join('\n'), context);
const task = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(context.buildMainCommand(
  task.logical_roles,
  task.physical_role_map,
  task.finish_roles,
  task.execution_options,
  task.prompt
));
"""
    result = subprocess.run(
        ["node", "-e", node_script],
        cwd=ROOT,
        input=json.dumps(task),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    "options,prompt",
    [
        (
            {
                "timeout": 1234.56789,
                "request_timeout": 7654.32109,
                "parallelism": 4,
                "max_turns": 0,
                "reload_after": 0.000001,
                "new_chat_on_handoff": False,
                "handoff_command_policy": "auto",
            },
            'say "hello" then inspect C:\\temp\\',
        ),
        (
            {
                "timeout": 1.0000001,
                "request_timeout": 86400,
                "parallelism": 32,
                "max_turns": 100000,
                "reload_after": 0.0000001,
                "new_chat_on_handoff": True,
                "handoff_command_policy": "always",
            },
            "trailing slash \\",
        ),
    ],
)
def test_dashboard_preview_is_byte_identical_to_scheduler_evidence(options: dict, prompt: str) -> None:
    task = {
        "logical_roles": ["DEV", "REVIEW", "PLAN"],
        "physical_role_map": {"DEV": "WORKER-A", "REVIEW": "WORKER-B", "PLAN": "WORKER-A"},
        "finish_roles": ["PLAN"],
        "execution_options": normalize_execution_options(options),
        "prompt": prompt,
    }

    assert dashboard_command(task) == build_main_command(task)


def test_dashboard_task_execution_policy_rejects_cli_only_off_value() -> None:
    with pytest.raises(ValueError, match="auto or always"):
        normalize_execution_options({"handoff_command_policy": "off"})
