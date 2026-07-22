import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const dashboardPath = path.resolve('dashboard.html');
assert.equal(fs.existsSync(dashboardPath), true, 'dashboard.html must exist');
const source = fs.readFileSync(dashboardPath, 'utf8');
assert.match(source, /<title>Stable Flow Runtime<\/title>/);
assert.match(source, /data-dashboard-version="12"/);
assert.match(source, /DASHBOARD_CONTRACT_VERSION\s*=\s*12/);

const scriptMatch = source.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, 'dashboard must contain one inline script');
const script = scriptMatch[1];
new vm.Script(script, { filename: 'dashboard.html:inline-script' });

function extractFunction(functionName) {
  const marker = `function ${functionName}`;
  let start = script.indexOf(marker);
  assert.notEqual(start, -1, `${functionName} must exist`);
  const asyncStart = Math.max(0, start - 6);
  if (script.slice(asyncStart, start) === 'async ') start = asyncStart;
  const braceStart = script.indexOf('{', start);
  let depth = 0;
  let quote = '';
  let escaped = false;
  for (let index = braceStart; index < script.length; index += 1) {
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
  throw new Error(`unterminated function ${functionName}`);
}

for (const id of [
  'dashboard-root', 'role-rail', 'role-count', 'role-detail-title', 'selected-role-state',
  'role-metrics', 'current-task', 'current-task-progress', 'current-task-text',
  'selected-role-detail', 'role-responses', 'response-count', 'runtime-events', 'event-count',
  'task-dialog', 'task-prompt', 'task-logical-order', 'task-logical-available',
  'task-browser-targets', 'task-role-mapping', 'task-timeout', 'task-request-timeout',
  'task-parallelism', 'task-max-turns', 'task-reload-after', 'task-command-preview', 'task-status', 'create-task'
]) assert.match(source, new RegExp(`id="${id}"`), `${id} must exist`);

// The global Kanban/task database UI is not part of the role dashboard.
assert.doesNotMatch(source, /Task control|No tasks configured|Create task only when|data-kanban-state|task-workspace|task-card-template|task-detail|filter-controller|filter-repository|filter-schedule|filter-archived/);
assert.doesNotMatch(script, /lastGoodTasks|renderBoard\(|saveEditor\(|openEditor\(|moveTask\(|wakeTask\(/);
assert.doesNotMatch(script, /fetch\(['"`]\/api\/admin\/tasks['"`]/, 'dashboard must not reload the global task database');

// The command composer contains only inputs represented in the generated command.
assert.doesNotMatch(source, />Title<|Target root|>Branch<|Controller role|Initial state|>Objective<|Scheduling|Execution result|Behavior toggles|task-new-chat|task-handoff-always/);
assert.match(source, />Task prompt</);
assert.match(source, />Logical roles/);
assert.match(source, />Online browser targets</);
assert.match(source, />Role mapping</);
assert.match(source, />Generated command</);
assert.match(source, /id="create-task"[^>]*type="submit">Create</);
assert.doesNotMatch(source, /Copy command/);
assert.match(source, /id="task-prompt" rows="2"/);
assert.match(script, /lineHeight \* 10 \+ 18/, 'prompt grows to ten lines then scrolls');
assert.doesNotMatch(script, /navigator\.clipboard/, 'Create must launch through the server, not copy text');
assert.match(extractFunction('submitTask'), /fetch\('\/api\/admin\/tasks\/launch'/);
assert.match(extractFunction('submitTask'), /const run = body\.run/);
assert.match(extractFunction('submitTask'), /Server did not confirm a launched runner process/);
assert.match(extractFunction('submitTask'), /run \$\{run\.run_id\} started \(PID \$\{run\.pid\}\)/);
assert.doesNotMatch(extractFunction('submitTask'), /body\.task|queued/);
assert.match(extractFunction('readJson'), /error\.status = response\.status/);
assert.match(extractFunction('submitTask'), /error\.status === 405/);
assert.match(source, /Task launch route is not active\. Restart Tampermonkey-Auto, then retry\./);
assert.match(extractFunction('openTaskComposer'), /const initialLogical = editorOriginRole/, 'each role card must start its own command');
assert.match(extractFunction('updateCommandPreview'), /buildLaunchCommand/);
assert.match(extractFunction('renderTaskComposer'), /field\.hidden = editorLogicalOrder\.length === 1/);
assert.match(source, /class="field multi-role-option">Parallelism/);

// Role board retains live/offline evidence and exact-role browser safety.
assert.match(script, /const onlineRoles = \(\) => lastGoodRoles\.filter\(\(role\) => role\.online === true\)/);
assert.match(script, /const visibleRoles = \(\) => lastGoodRoles\.filter/);
assert.match(extractFunction('renderRoles'), /visibleRoles\(\)/);
assert.doesNotMatch(extractFunction('renderRoles'), /onlineRoles\(\)/);
assert.match(script, /cached evidence/);
assert.match(script, /clearButton\.disabled = !role\.online/);
assert.match(script, /reloadButton\.disabled = !role\.online/);
assert.match(extractFunction('runRoleCommand'), /if \(!role\?\.online\)/);
assert.match(script, /CLEAR_COMPOSER_TEXT/);
assert.match(script, /RELOAD_PAGE/);
assert.match(script, /textNode\('button', '\+ task', 'primary'\)/);

// Selected role shows the latest observed user task and response progress.
assert.match(extractFunction('renderSelectedRole'), /detail\.observation\?\.last_user/);
assert.match(extractFunction('renderSelectedRole'), /current-task-progress/);
assert.match(extractFunction('renderSelectedRole'), /user \$\{Number\(counts\.user/);
assert.match(extractFunction('renderSelectedRole'), /· assistant \$\{Number\(counts\.assistant/);
assert.match(extractFunction('renderSelectedRole'), /latestTaskText/);
assert.match(script, /detail\.responses \|\| \[\]/);
assert.match(extractFunction('renderSelectedRole'), /followNewest \? responses\.scrollHeight : previousResponseTop/);

// Timeline is chronological and explicitly visualizes From -> To and state.
assert.match(extractFunction('renderEvents'), /flowEventView\(event\)/);
assert.match(extractFunction('renderEvents'), /flow-route/);
assert.doesNotMatch(extractFunction('renderEvents'), /\.reverse\(\)/, 'timeline must read top-to-bottom');
assert.match(script, /from_role/);
assert.match(script, /done_from/);
assert.match(script, /sent_to/);
assert.match(extractFunction('renderEvents'), /row\.className = 'flow-line'/);

const helperContext = {};
vm.runInNewContext([
  extractFunction('commandNumber'),
  extractFunction('quoteCli'),
  extractFunction('deriveFinishRoles'),
  extractFunction('buildMainCommand'),
  extractFunction('buildRoleCommand'),
  extractFunction('buildLaunchCommand'),
  extractFunction('latestTaskText'),
  extractFunction('flowEventView'),
  'globalThis.buildMainCommand = buildMainCommand;',
  'globalThis.buildLaunchCommand = buildLaunchCommand;',
  'globalThis.deriveFinishRoles = deriveFinishRoles;',
  'globalThis.latestTaskText = latestTaskText;',
  'globalThis.flowEventView = flowEventView;'
].join('\n'), helperContext);

const options = { timeout: 1800, request_timeout: 1200, parallelism: 4, max_turns: 0, reload_after: 10 };
const command = helperContext.buildMainCommand(
  ['DEV', 'REVIEW', 'PLAN'],
  { DEV: 'C2', REVIEW: 'C3', PLAN: 'C2' },
  ['PLAN'],
  options,
  'ship "this" from C:\\repo\\'
);
assert.equal(command, 'uv run main.py --role "DEV,REVIEW,PLAN" --browser-roles "C2,C3" --role-map "DEV=C2 REVIEW=C3 PLAN=C2" --finish-roles "PLAN" --timeout 1800 --request-timeout 1200 --parallelism 4 --max-turns 0 --reload-after 10 --goal "ship \\"this\\" from C:\\repo\\\\"');
const singleCommand = helperContext.buildLaunchCommand(
  ['C2'],
  { C2: 'C2' },
  ['C2'],
  options,
  'ch? c?n n?i ok.'
);
assert.equal(singleCommand, 'uv run role.py --role "C2" --timeout 1800 --request-timeout 1200 --prompt "ch? c?n n?i ok."');
for (const forbidden of ['main.py', '--browser-roles', '--role-map', '--finish-roles', '--parallelism', '--max-turns', '--reload-after', '--goal']) {
  assert.equal(singleCommand.includes(forbidden), false, `${forbidden} must not appear in a direct role command`);
}
for (const forbidden of ['--new-chat-on-handoff', '--handoff-command-policy', '--title', '--branch', '--target-root', '--status']) {
  assert.equal(command.includes(forbidden), false, `${forbidden} must not be invented`);
}
assert.deepEqual(JSON.parse(JSON.stringify(helperContext.deriveFinishRoles(['DEV', 'PLAN', 'REVIEW']))), ['PLAN']);
assert.deepEqual(JSON.parse(JSON.stringify(helperContext.deriveFinishRoles(['DEV', 'REVIEW']))), ['REVIEW']);
assert.deepEqual(JSON.parse(JSON.stringify(helperContext.deriveFinishRoles(['C2']))), ['C2']);

assert.equal(helperContext.latestTaskText('PROMPT_ROLE: DEV\nGOAL:\nFix the dashboard\nROUTE_JSON_CONTRACT:\n{}'), 'Fix the dashboard');
assert.equal(helperContext.latestTaskText('plain user task'), 'plain user task');

assert.deepEqual(
  JSON.parse(JSON.stringify(helperContext.flowEventView({ event: 'FLOW_RUNNING', state: 'RUNNING', logical_role: 'DEV', from_role: 'PLAN' }))),
  { state: 'RUNNING', route: 'PLAN -> DEV', summary: 'DEV is running' }
);
assert.deepEqual(
  JSON.parse(JSON.stringify(helperContext.flowEventView({ event: 'FLOW_DONE', state: 'DONE', logical_role: 'DEV', sent_to: 'REVIEW' }))),
  { state: 'DONE', route: 'DEV -> REVIEW', summary: 'DEV completed' }
);
assert.deepEqual(
  JSON.parse(JSON.stringify(helperContext.flowEventView({ event: 'FLOW_WAITING', state: 'WAITING', logical_role: 'REVIEW' }))),
  { state: 'WAITING', route: 'REVIEW', summary: 'REVIEW is waiting' }
);

// Selected-role reads remain generation-bound.
assert.match(script, /fetch\(`\/api\/admin\/role\/\$\{encodeURIComponent\(role\)}\?response_limit=10`\)/);
assert.match(script, /TIMELINE_LIMIT\s*=\s*30/);
assert.match(script, /fetch\(`\/api\/admin\/role\/\$\{encodeURIComponent\(role\)}\/timeline\?limit=\$\{TIMELINE_LIMIT\}`\)/);
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
const roleA = { detail: deferred(), timeline: deferred() };
const roleB = { detail: deferred(), timeline: deferred() };
const raceContext = {
  Promise,
  Date,
  Map,
  ROLE_DATA_TTL_MS: 5000,
  roleDataCache: new Map(),
  roleDataInflight: new Map(),
  selectedRole: 'A',
  selectedRoleRefreshGeneration: 0,
  lastGoodRoleDetail: null,
  lastGoodRoleEvents: [],
  sourceStale: { events: false },
  fetchRoleDetail(role) { return role === 'A' ? roleA.detail.promise : roleB.detail.promise; },
  fetchRoleEvents(role) { return role === 'A' ? roleA.timeline.promise : roleB.timeline.promise; }
};
vm.runInNewContext([
  extractFunction('applyRoleCache'),
  extractFunction('refreshSelectedRoleData'),
  'globalThis.refreshSelectedRoleData = refreshSelectedRoleData;'
].join('\n'), raceContext);
const first = raceContext.refreshSelectedRoleData();
raceContext.selectedRole = 'B';
const second = raceContext.refreshSelectedRoleData();
roleB.detail.resolve({ role: 'B' });
roleB.timeline.resolve({ role: 'B', events: [{ role: 'B', event: 'FLOW_RUNNING' }] });
await second;
roleA.detail.resolve({ role: 'A' });
roleA.timeline.resolve({ role: 'A', events: [{ role: 'A', event: 'FLOW_DONE' }] });
await first;
assert.equal(raceContext.lastGoodRoleDetail.role, 'B');
assert.equal(raceContext.lastGoodRoleEvents[0].role, 'B');

assert.match(source, /:focus-visible/);
assert.match(source, /aria-live="polite"/);
assert.match(script, /refreshInFlight/);
assert.match(script, /ROLE_DATA_TTL_MS\s*=\s*5000/);
assert.match(script, /roleDataCache = new Map\(\)/);
assert.match(script, /roleDataInflight = new Map\(\)/);
assert.match(script, /roleCardNodes = new Map\(\)/);
assert.match(script, /roleCardKeys = new Map\(\)/);
assert.match(extractFunction('roleCardSignature'), /join\('\\u001f'\)/);
assert.match(extractFunction('renderRoles'), /roleCardKeys\.get\(role\.role\) !== key/);
assert.match(extractFunction('renderRoles'), /card\.replaceWith\(replacement\)/);
assert.match(extractFunction('renderRoles'), /rail\.insertBefore\(card, cursor\)/);
assert.doesNotMatch(extractFunction('renderRoles'), /clearNode\(rail\);[\s\S]*for \(const role of roles\) rail\.appendChild/, 'role polling must not rebuild the whole rail');
assert.match(script, /target\.textContent !== text/);
assert.match(extractFunction('renderSelectedRole'), /renderKey === selectedRoleRenderKey/);
assert.match(extractFunction('renderEvents'), /renderKey === timelineRenderKey/);
assert.match(source, /\.role-board-panel\s*\{[\s\S]*?width: min\(100%, 1120px\);[\s\S]*?margin: 10px auto 0;/);
assert.match(source, /\.role-board\s*\{[\s\S]*?grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);/);
assert.match(source, /@media \(max-width: 1040px\)[\s\S]*?repeat\(3, minmax\(0, 1fr\)\)/);
assert.match(source, /@media \(max-width: 760px\)[\s\S]*?repeat\(2, minmax\(0, 1fr\)\)/);
assert.match(source, /@media \(max-width: 560px\)[\s\S]*?\.role-board \{ grid-template-columns: 1fr; \}/);
assert.match(source, /\.role-card\s*\{[\s\S]*?min-height: 154px;[\s\S]*?content-visibility: auto;[\s\S]*?contain-intrinsic-size: 154px;/);
assert.doesNotMatch(source, /repeat\(auto-fit, minmax\(270px, 1fr\)\)/);
assert.match(source, /#runtime-events\s*\{[\s\S]*?max-height: clamp\(300px, 52vh, 560px\);[\s\S]*?overflow-y: auto;/);
assert.match(source, /@media \(max-width: 560px\)[\s\S]*?#runtime-events \{ max-height: 60vh;/);
assert.match(script, /document\.hidden/);
assert.match(script, /refreshDashboard\(\{ forceRole: true \}\);/);
assert.match(script, /visibilitychange/);
assert.match(extractFunction('renderHealth'), /terminal \? `Last flow \$\{flow\.request_id\} · \$\{String\(flow\.terminal_status\)\.toUpperCase\(\)\}`/);
assert.doesNotMatch(source, /Â·|�|A�|��|���/, 'dashboard source must not contain mojibake');
assert.doesNotMatch(script, /\.innerHTML\s*=/);
assert.doesNotMatch(source, /data-cdp-action|CDP (?:focus|reload|open|close|screenshot|navigate)/i);
assert.doesNotMatch(source, /<script[^>]+src=|<link[^>]+stylesheet/i);
assert.doesNotMatch(source, /WebSocket|EventSource|localStorage|sessionStorage/);

console.log('dashboard contract: PASS');
