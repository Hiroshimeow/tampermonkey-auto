import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const dashboardPath = path.resolve('dashboard.html');
assert.equal(fs.existsSync(dashboardPath), true, 'dashboard.html must exist at the repository root');

const source = fs.readFileSync(dashboardPath, 'utf8');
assert.match(source, /<title>Stable Flow Kanban<\/title>/, 'dashboard must have the Phase 05 title');
assert.match(source, /id="dashboard-root"/, 'dashboard must expose a stable root marker');
assert.match(source, /data-dashboard-version="5"/, 'dashboard contract version must be explicit');
assert.match(source, /DASHBOARD_CONTRACT_VERSION\s*=\s*5/, 'script contract version must match markup');

const scriptMatch = source.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, 'dashboard must contain one inline script');
const script = scriptMatch[1];
new vm.Script(script, { filename: 'dashboard.html:inline-script' });

for (const state of ['BACKLOG', 'READY', 'RUNNING', 'REVIEW', 'BLOCKED', 'DONE']) {
  assert.match(source, new RegExp(`data-kanban-state="${state}"`), `${state} column marker must be stable`);
  assert.match(source, new RegExp(`>${state}<`), `${state} label must be visible`);
}

assert.match(script, /fetch\('\/api\/admin\/tasks/, 'dashboard must read durable tasks');
assert.match(script, /fetch\('\/api\/admin\/roles'/, 'dashboard must read generic role inventory');
assert.match(script, /fetch\('\/api\/admin\/flow'/, 'dashboard must retain active flow context');
for (const endpoint of ['move', 'wake', 'pause', 'resume']) {
  assert.match(script, new RegExp(`/api/admin/tasks/\\$\\{encodeURIComponent\\(taskId\\)\\}/${endpoint}`), `dashboard must use ${endpoint} task endpoint`);
}
assert.match(script, /method:\s*'PATCH'/, 'dashboard must support optimistic task editing and archive');
assert.match(script, /method:\s*'POST'/, 'dashboard must support create and bounded task actions');
assert.doesNotMatch(script, /fetch\([^\n]*\/api\/admin\/command/, 'dashboard must never call the browser command endpoint');
assert.doesNotMatch(script, /shell_command|executable|subprocess|powershell|cmd\.exe/i, 'dashboard must not expose arbitrary execution fields');

assert.match(source, /id="create-task"/, 'create control must exist');
assert.match(source, /id="task-dialog"/, 'create/edit drawer must exist');
assert.match(source, /id="task-detail"/, 'task detail drawer must exist');
for (const control of ['run-action', 'pause-action', 'edit-action', 'move-select', 'archive-action', 'resolve-sent', 'resolve-not-sent']) {
  assert.match(source, new RegExp(`class="[^"]*${control}`), `${control} task control must exist`);
}
for (const field of ['task-controller', 'task-logical', 'task-role-map', 'task-finish', 'task-schedule-kind', 'task-result-status', 'task-result-summary', 'task-blocker']) {
  assert.match(source, new RegExp(`id="${field}"`), `${field} editor field must exist`);
}
assert.match(source, /draggable="true"/, 'native drag and drop path must exist');
assert.match(source, /aria-label="Move task"/, 'keyboard-accessible move control must exist');
assert.match(source, /:focus-visible/, 'visible keyboard focus styling is required');
assert.match(source, /aria-live="polite"/, 'status and conflict announcements must be accessible');

for (const filter of ['filter-text', 'filter-controller', 'filter-repository', 'filter-status', 'filter-schedule', 'filter-archived']) {
  assert.match(source, new RegExp(`id="${filter}"`), `${filter} must exist`);
}
assert.match(source, /id="scheduler-health"/, 'scheduler health must be visible');
assert.match(source, /id="role-rail"/, 'live role rail must be visible');

assert.match(script, /lastGoodTasks/, 'task data must retain the last good projection');
assert.match(script, /lastGoodRoles/, 'role data must retain the last good projection');
assert.match(script, /lastGoodFlow/, 'flow data must retain the last good projection');
assert.match(script, /refreshInFlight/, 'polling must prevent overlap');
assert.match(script, /setInterval\(refreshDashboard, 2000\)/, 'dashboard must remain polling-based');
assert.match(script, /response\.status === 409/, 'optimistic conflicts must be handled explicitly');
assert.match(script, /retainEditsOnConflict/, '409 handling must retain user edits');
assert.match(script, /\.textContent\s*=/, 'remote values must be assigned through textContent');
assert.doesNotMatch(script, /\.innerHTML\s*=/, 'runtime rendering must not interpolate remote values into innerHTML');

assert.match(source, /Task store unavailable/, 'task-store errors must remain visible');
assert.match(source, /Scheduler error/, 'scheduler errors must remain visible');
assert.match(source, /data-stale/, 'partial failures must mark retained data stale');
assert.match(script, /external_target/, 'CDP seam metadata may be rendered');
assert.match(script, /transport/, 'transport seam metadata may be rendered');
assert.doesNotMatch(source, /data-cdp-action|CDP (?:focus|reload|open|close|screenshot|navigate)/i, 'Phase 06 CDP controls must not exist');
assert.doesNotMatch(source, /WebSocket|EventSource|localStorage|sessionStorage/, 'dashboard must remain dependency-free polling without client persistence or streaming');
assert.doesNotMatch(source, /https?:\/\//, 'dashboard must not depend on external assets or CDNs');

console.log('dashboard contract: PASS');
