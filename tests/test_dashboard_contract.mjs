import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const dashboardPath = path.resolve('dashboard.html');
assert.equal(fs.existsSync(dashboardPath), true, 'dashboard.html must exist at the repository root');

const source = fs.readFileSync(dashboardPath, 'utf8');
assert.match(source, /<title>Stable Flow Runtime<\/title>/, 'dashboard must retain the runtime title');
assert.match(source, /id="dashboard-root"/, 'dashboard must expose a stable root marker');
assert.match(source, /data-dashboard-version="9"/, 'dashboard role-board contract version must be explicit');
assert.match(source, /DASHBOARD_CONTRACT_VERSION\s*=\s*9/, 'script contract version must match markup');

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
    if (char === '}') {
      depth -= 1;
      if (depth === 0) return script.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated function ${functionName}`);
}

for (const state of ['BACKLOG', 'READY', 'RUNNING', 'REVIEW', 'BLOCKED', 'DONE']) {
  assert.match(source, new RegExp(`data-kanban-state="${state}"`), `${state} column marker must remain stable`);
  assert.match(source, new RegExp(`>${state}<`), `${state} label must remain visible`);
}

// Role board is the primary runtime surface. The previous summary sections are gone.
for (const id of ['role-rail', 'role-count', 'role-detail-title', 'selected-role-state', 'role-metrics', 'selected-role-detail', 'role-responses', 'response-count', 'runtime-events', 'event-count']) {
  assert.match(source, new RegExp(`id="${id}"`), `${id} role-board surface must exist`);
}
assert.match(source, />Role board</, 'online role board heading must be visible');
assert.doesNotMatch(source, /<h2>Runtime roles<\/h2>|<h2>Active flow<\/h2>|<h2>System<\/h2>/, 'legacy runtime summary sections must be removed');
assert.doesNotMatch(source, /id="flow-detail"|id="system-detail"/, 'legacy summary detail containers must be removed');
assert.match(script, /lastGoodRoles\.filter\(\(role\) => role\.online === true\)/, 'role cards must render online physical roles only');
assert.match(script, /role\.role} · \$\{role\.configured_role \|\| role\.role} ↗/, 'card heading must show physical and configured role only');
assert.match(script, /link\.addEventListener\('click', \(event\) => event\.stopPropagation\(\)\)/, 'external chat link must not select the card');
assert.match(script, /card\.addEventListener\('click', \(\) => selectRole\(role\.role\)\)/, 'the rest of the role card must select that role');
assert.match(script, /https:\/\/chatgpt\.com\$\{chatPath\(role\)}/, 'role title must link to the observed ChatGPT conversation path');
assert.match(script, /shortConversationId/, 'role card must derive a short conversation ID from the real path');
assert.match(script, /configured_role/, 'configured logical role must be rendered');
assert.match(script, /current_logical_role/, 'current logical responsibility must be rendered');
assert.match(script, /turn \$\{Number\(role\.turn \|\| 0\)}/, 'role turn must be visible');
assert.match(script, /composer_attachment_count/, 'composer attachment count must be visible on cards');

// Selected role detail and timeline use role-scoped read APIs.
assert.match(script, /fetch\(`\/api\/admin\/role\/\$\{encodeURIComponent\(role\)}`\)/, 'selected role detail must use the role detail API');
assert.match(script, /fetch\(`\/api\/admin\/role\/\$\{encodeURIComponent\(role\)}\/timeline\?limit=100`\)/, 'timeline must use the semantic role-timeline endpoint');
assert.doesNotMatch(extractFunction('fetchRoleEvents'), /\/api\/admin\/events/, 'selected-role timeline must not use the raw event endpoint');
assert.match(script, /detail\.responses \|\| \[\]/, 'selected role must render multiple projected responses');
assert.match(script, /responses\.scrollTop = responses\.scrollHeight/, 'newest response must be focused on initial load and role changes');
assert.match(script, /focusNewestResponse/, 'auto-scroll must be bounded to initial load and selection changes');
assert.match(script, /current_task_id/, 'selected role detail must expose task evidence');
assert.match(script, /current_request_id/, 'selected role detail must expose request evidence');
assert.match(script, /message_counts/, 'selected role detail must expose transcript counts');
assert.match(script, /observation_seq/, 'selected role detail must expose observation evidence');
assert.match(script, /transport/, 'selected role detail must expose transport evidence');

// Browser commands are exact-role, bounded, and terminal-state driven.
assert.match(script, /fetch\('\/api\/admin\/command'/, 'role actions must use the existing admin command endpoint');
assert.match(script, /body: JSON\.stringify\(\{ role: roleName, action, payload: \{\} \}\)/, 'role actions must target the exact physical role');
assert.match(script, /CLEAR_COMPOSER_TEXT/, 'Clear txt must use the dedicated text-only command');
assert.match(script, /RELOAD_PAGE/, 'F5 must use the existing reload command');
assert.match(script, /fetch\(`\/api\/admin\/command\/\$\{encodeURIComponent\(commandId\)}`\)/, 'role actions must poll command status');
assert.match(script, /attempt < 60/, 'command status polling must be bounded');
assert.match(script, /textNode\('button', 'Clear txt'\)/, 'Clear txt control must be created for each online role');
assert.match(script, /textNode\('button', 'F5'\)/, 'F5 control must be created for each online role');
assert.match(script, /textNode\('button', '\+ task', 'primary'\)/, 'per-role task creation control must be created');

// Task control retains the existing Kanban and CAS workflow.
assert.match(script, /fetch\('\/api\/admin\/tasks/, 'dashboard must read durable tasks');
assert.match(script, /fetch\('\/api\/admin\/roles'/, 'dashboard must read generic role inventory');
assert.match(script, /fetch\('\/api\/admin\/flow'/, 'dashboard must retain flow liveness for topbar health');
for (const endpoint of ['move', 'wake', 'pause', 'resume']) {
  assert.match(script, new RegExp(`/api/admin/tasks/\\$\\{encodeURIComponent\\(taskId\\)\\}/${endpoint}`), `dashboard must preserve the ${endpoint} task endpoint`);
}
assert.match(script, /method:\s*'PATCH'/, 'dashboard must preserve optimistic task editing and archive');
assert.match(script, /response\.status === 409/, 'optimistic conflicts must be handled explicitly');
assert.match(script, /retainEditsOnConflict/, '409 handling must retain user edits');
assert.match(source, /id="create-task"/, 'create control must exist');
assert.match(source, /id="task-dialog"/, 'create/edit dialog must exist');
assert.match(source, /id="task-detail"/, 'task detail dialog must exist');
for (const control of ['run-action', 'pause-action', 'edit-action', 'move-select', 'archive-action', 'resolve-sent', 'resolve-not-sent']) {
  assert.match(source, new RegExp(`class="[^"]*${control}`), `${control} task control must exist`);
}
for (const field of [
  'task-controller', 'task-logical-order', 'task-logical-available', 'task-browser-targets', 'task-role-mapping',
  'task-timeout', 'task-request-timeout', 'task-parallelism', 'task-max-turns', 'task-reload-after',
  'task-new-chat', 'task-handoff-always', 'task-command-preview', 'task-schedule-kind',
  'task-result-status', 'task-result-summary', 'task-blocker'
]) {
  assert.match(source, new RegExp(`id="${field}"`), `${field} editor field must exist`);
}
assert.doesNotMatch(source, /id="task-logical"|id="task-role-map"|id="task-finish"/, 'raw comma/JSON role editor fields must be removed');
assert.match(source, /id="task-prompt" rows="2"/, 'prompt must start at two rows');
assert.match(script, /lineHeight \* 10 \+ 18/, 'prompt auto-grow must cap at ten rows before scrolling');
assert.match(source, /id="prompt-samples"/, 'prompt samples menu must exist');
assert.match(script, /PROMPT_SAMPLES/, 'prompt samples must be functional');
assert.match(source, /draggable="true"/, 'native drag and drop path must remain');
assert.match(source, /aria-label="Move task"/, 'keyboard-accessible task move control must remain');
assert.match(source, /:focus-visible/, 'visible keyboard focus styling is required');
assert.match(source, /aria-live="polite"/, 'status and conflict announcements must remain accessible');

// Task editor derives roles/mapping/options and persists one validated execution_options object.
assert.match(script, /BASE_LOGICAL_ROLES/, 'configured logical role choices must be data-driven');
assert.match(script, /onlineRoles\(\)\.map\(\(role\) => role\.role\)/, 'physical mapping targets must derive from online browser roles');
assert.doesNotMatch(extractFunction('editorPhysicalTargets'), /Object\.values\(editorRoleMap\)|task-controller/, 'stale mappings and controllers must not remain selectable targets');
assert.match(script, /const exact = live\.find\(\(role\) => role\.role === logicalRole\)/, 'default mapping must prefer an exact physical-role name match');
assert.match(script, /editorOriginRole/, 'default mapping must fall back to the origin role card');
assert.match(script, /validateOnlineTaskTargets/, 'save-time validation must reject offline controller and mapping targets');
assert.match(extractFunction('saveEditor'), /await fetchRoles\(\)/, 'save must refresh online-role evidence before persistence');
assert.doesNotMatch(script, /const browserTabs\s*=|['"]C1['"]|['"]C2['"]|WORKER_1/, 'dashboard must not hardcode physical browser roles');
assert.match(script, /execution_options: executionOptions/, 'create and PATCH payloads must persist execution_options');
assert.match(script, /task\.execution_options \|\| \{\}/, 'edit mode must restore persisted execution options');
assert.match(script, /timeout:\s*1800/, 'timeout default must be 1800');
assert.match(script, /request_timeout:\s*1200/, 'request-timeout default must be 1200');
assert.match(script, /parallelism:\s*4/, 'parallelism default must be 4');
assert.match(script, /max_turns:\s*0/, 'max-turns default must be 0');
assert.match(script, /reload_after:\s*10/, 'reload-after default must be 10');
assert.match(script, /new_chat_on_handoff:\s*false/, 'new-chat toggle must default off');
assert.match(script, /handoff_command_policy:\s*'auto'/, 'handoff-always toggle must default off/auto');
assert.match(script, /--new-chat-on-handoff/, 'enabled new-chat toggle must add the exact CLI flag');
assert.match(script, /--handoff-command-policy always/, 'enabled handoff toggle must add the exact CLI flag');
assert.match(script, /--finish-roles/, 'generated command must include derived finish authority');

const commandContext = {};
vm.runInNewContext([
  extractFunction('commandNumber'),
  extractFunction('quoteCli'),
  extractFunction('deriveFinishRoles'),
  extractFunction('buildMainCommand'),
  'globalThis.deriveFinishRoles = deriveFinishRoles;',
  'globalThis.buildMainCommand = buildMainCommand;'
].join('\n'), commandContext);
assert.deepEqual(
  JSON.parse(JSON.stringify(commandContext.deriveFinishRoles(['DEV', 'PLAN', 'MANAGER', 'REVIEW']))),
  ['MANAGER'],
  'MANAGER must have highest finish-role precedence'
);
assert.deepEqual(JSON.parse(JSON.stringify(commandContext.deriveFinishRoles(['DEV', 'PLAN', 'REVIEW']))), ['PLAN']);
assert.deepEqual(JSON.parse(JSON.stringify(commandContext.deriveFinishRoles(['DEV', 'REVIEW']))), ['REVIEW']);
assert.deepEqual(JSON.parse(JSON.stringify(commandContext.deriveFinishRoles(['DEV', 'A']))), ['DEV']);
const baseOptions = {
  timeout: 1800,
  request_timeout: 1200,
  parallelism: 4,
  max_turns: 0,
  reload_after: 10,
  new_chat_on_handoff: false,
  handoff_command_policy: 'auto'
};
const baseCommand = commandContext.buildMainCommand(
  ['DEV', 'REVIEW', 'PLAN'],
  { DEV: 'WORKER-A', REVIEW: 'WORKER-B', PLAN: 'WORKER-A' },
  ['PLAN'],
  baseOptions,
  'ship it'
);
for (const fragment of [
  'uv run python main.py',
  '--role "DEV,REVIEW,PLAN"',
  '--browser-roles "WORKER-A,WORKER-B"',
  '--role-map "DEV=WORKER-A REVIEW=WORKER-B PLAN=WORKER-A"',
  '--finish-roles "PLAN"',
  '--timeout 1800',
  '--request-timeout 1200',
  '--parallelism 4',
  '--max-turns 0',
  '--reload-after 10',
  '--goal "ship it"'
]) assert.ok(baseCommand.includes(fragment), `generated command must include ${fragment}`);
assert.equal(baseCommand.includes('--new-chat-on-handoff'), false, 'off new-chat toggle must omit the flag');
assert.equal(baseCommand.includes('--handoff-command-policy'), false, 'off handoff toggle must omit the flag');
const toggledCommand = commandContext.buildMainCommand(
  ['DEV'],
  { DEV: 'WORKER-A' },
  ['DEV'],
  { ...baseOptions, new_chat_on_handoff: true, handoff_command_policy: 'always' },
  'ship it'
);
assert.ok(toggledCommand.includes('--new-chat-on-handoff'));
assert.ok(toggledCommand.includes('--handoff-command-policy always'));
assert.throws(
  () => commandContext.buildMainCommand(
    ['DEV'],
    { DEV: 'WORKER-A' },
    ['DEV'],
    { ...baseOptions, handoff_command_policy: 'off' },
    'ship it'
  ),
  /auto or always/,
  'dashboard task command builder must reject the CLI-only off policy'
);

const targetContext = {
  lastGoodRoles: [{ role: 'ONLINE-A', online: true }, { role: 'OFFLINE-X', online: false }],
  editorRoleMap: { DEV: 'OFFLINE-X' },
  onlineRoles() { return [{ role: 'ONLINE-A', online: true }]; },
  byId() { return { value: 'OFFLINE-X' }; }
};
vm.runInNewContext([
  extractFunction('editorPhysicalTargets'),
  extractFunction('validateOnlineTaskTargets'),
  'globalThis.editorPhysicalTargets = editorPhysicalTargets;',
  'globalThis.validateOnlineTaskTargets = validateOnlineTaskTargets;'
].join('\n'), targetContext);
assert.deepEqual(
  JSON.parse(JSON.stringify(targetContext.editorPhysicalTargets())),
  ['ONLINE-A'],
  'only currently online physical roles may be selectable'
);
assert.throws(
  () => targetContext.validateOnlineTaskTargets('OFFLINE-X', { DEV: 'ONLINE-A' }, ['ONLINE-A']),
  /Controller role OFFLINE-X is offline/,
  'offline controller must fail closed'
);
assert.throws(
  () => targetContext.validateOnlineTaskTargets('ONLINE-A', { DEV: 'OFFLINE-X' }, ['ONLINE-A']),
  /DEV.*OFFLINE-X.*offline/,
  'offline mapping must fail closed'
);

const terminalContext = {};
vm.runInNewContext([
  extractFunction('commandTerminalOutcome'),
  'globalThis.commandTerminalOutcome = commandTerminalOutcome;'
].join('\n'), terminalContext);
assert.equal(terminalContext.commandTerminalOutcome('CLEAR_COMPOSER_TEXT', 'COMPOSER_TEXT_CLEARED').ok, true);
assert.equal(terminalContext.commandTerminalOutcome('RELOAD_PAGE', 'PAGE_RELOADING').ok, true);
for (const terminal of ['CANCELLED', 'EXPIRED', 'COMPOSER_TEXT_CLEAR_FAILED', 'ASSISTANT_DONE', 'UNKNOWN']) {
  assert.equal(terminalContext.commandTerminalOutcome('CLEAR_COMPOSER_TEXT', terminal).ok, false, `${terminal} must fail Clear txt`);
}
assert.equal(
  terminalContext.commandTerminalOutcome('RELOAD_PAGE', 'COMPOSER_TEXT_CLEARED').ok,
  false,
  'success for another action must fail closed'
);

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
const roleA = { detail: deferred(), timeline: deferred() };
const roleB = { detail: deferred(), timeline: deferred() };
const raceContext = {
  Promise,
  selectedRole: 'A',
  selectedRoleRefreshGeneration: 0,
  lastGoodRoleDetail: null,
  lastGoodRoleEvents: [],
  sourceStale: { events: false },
  fetchRoleDetail(role) { return role === 'A' ? roleA.detail.promise : roleB.detail.promise; },
  fetchRoleEvents(role) { return role === 'A' ? roleA.timeline.promise : roleB.timeline.promise; }
};
vm.runInNewContext([
  extractFunction('refreshSelectedRoleData'),
  'globalThis.refreshSelectedRoleData = refreshSelectedRoleData;'
].join('\n'), raceContext);
const firstRoleRefresh = raceContext.refreshSelectedRoleData();
raceContext.selectedRole = 'B';
const secondRoleRefresh = raceContext.refreshSelectedRoleData();
roleB.detail.resolve({ role: 'B' });
roleB.timeline.resolve({ role: 'B', events: [{ role: 'B', event: 'ASSISTANT_DONE' }] });
await secondRoleRefresh;
roleA.detail.resolve({ role: 'A' });
roleA.timeline.resolve({ role: 'A', events: [{ role: 'A', event: 'ASSISTANT_DONE' }] });
await firstRoleRefresh;
assert.equal(raceContext.lastGoodRoleDetail.role, 'B');
assert.deepEqual(
  JSON.parse(JSON.stringify(raceContext.lastGoodRoleEvents)),
  [{ role: 'B', event: 'ASSISTANT_DONE' }],
  'late role-A timeline must not overwrite selected role B'
);

const mismatchContext = {
  Promise,
  selectedRole: 'B',
  selectedRoleRefreshGeneration: 0,
  lastGoodRoleDetail: null,
  lastGoodRoleEvents: [{ role: 'B', event: 'OLD' }],
  sourceStale: { events: false },
  async fetchRoleDetail() { return { role: 'B' }; },
  async fetchRoleEvents() { return { role: 'B', events: [{ role: 'A', event: 'ASSISTANT_DONE' }] }; }
};
vm.runInNewContext([
  extractFunction('refreshSelectedRoleData'),
  'globalThis.refreshSelectedRoleData = refreshSelectedRoleData;'
].join('\n'), mismatchContext);
await mismatchContext.refreshSelectedRoleData();
assert.deepEqual(JSON.parse(JSON.stringify(mismatchContext.lastGoodRoleEvents)), []);
assert.equal(mismatchContext.sourceStale.events, true, 'mismatched event roles must fail closed');

// Existing reliability, health, and dependency-free constraints remain.
for (const filter of ['filter-text', 'filter-controller', 'filter-repository', 'filter-status', 'filter-schedule', 'filter-archived']) {
  assert.match(source, new RegExp(`id="${filter}"`), `${filter} must exist`);
}
assert.match(source, /id="scheduler-health"/, 'scheduler health must remain visible');
assert.match(source, /id="runner-health"/, 'runner process health must remain visible');
assert.match(source, /id="flow-stall-notice"/, 'stalled flow evidence must retain a dedicated notice');
assert.match(script, /liveness\?\.stalled/, 'dashboard must derive stall from flow liveness');
assert.match(script, /lastGoodTasks/, 'task data must retain the last good projection');
assert.match(script, /lastGoodRoles/, 'role data must retain the last good projection');
assert.match(script, /lastGoodFlow/, 'flow data must retain the last good projection');
assert.match(script, /lastGoodRoleEvents/, 'selected-role event data must retain the last good projection');
assert.match(script, /taskWorkspace\.hidden\s*=\s*!hasTasks/, 'empty task store must hide the Kanban workspace');
assert.match(script, /noTasksState\.hidden\s*=\s*hasTasks/, 'empty task store must show one meaningful empty state');
assert.match(script, /refreshInFlight/, 'polling must prevent overlap');
assert.match(script, /setInterval\(refreshDashboard, 2000\)/, 'dashboard must remain polling-based');
assert.match(script, /\.textContent\s*=/, 'remote values must be assigned through textContent');
assert.doesNotMatch(script, /\.innerHTML\s*=/, 'runtime rendering must not interpolate remote values into innerHTML');
assert.match(source, /Task store unavailable/, 'task-store errors must remain visible');
assert.match(source, /Scheduler error/, 'scheduler errors must remain visible');
assert.match(source, /data-stale/, 'partial failures must mark retained data stale');
assert.match(script, /external_target/, 'CDP seam metadata may still be rendered in task details');
assert.doesNotMatch(source, /data-cdp-action|CDP (?:focus|reload|open|close|screenshot|navigate)/i, 'unapproved CDP controls must not exist');
assert.doesNotMatch(source, /<script[^>]+src=|<link[^>]+stylesheet/i, 'dashboard must not depend on external assets or CDNs');
assert.doesNotMatch(source, /WebSocket|EventSource|localStorage|sessionStorage/, 'dashboard must remain polling-based without client persistence or streaming');

console.log('dashboard contract: PASS');
