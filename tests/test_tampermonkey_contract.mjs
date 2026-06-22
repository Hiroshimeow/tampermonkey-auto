import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const scriptPath = path.resolve('tampermonkey.js');
const source = fs.readFileSync(scriptPath, 'utf8');

assert.match(source, /action_delay_min_ms:\s*\d+/, 'tampermonkey.js must expose action_delay_min_ms');
assert.match(source, /action_delay_max_ms:\s*\d+/, 'tampermonkey.js must expose action_delay_max_ms');
assert.match(source, /send_delay_min_ms:\s*\d+/, 'tampermonkey.js must expose send_delay_min_ms');
assert.match(source, /send_delay_max_ms:\s*\d+/, 'tampermonkey.js must expose send_delay_max_ms');
assert.match(source, /send_accept_timeout_ms:\s*\d+/, 'tampermonkey.js must expose send_accept_timeout_ms');
assert.match(source, /send_accept_poll_ms:\s*\d+/, 'tampermonkey.js must expose send_accept_poll_ms');
assert.match(source, /assistant_post_stop_timeout_ms:\s*\d+/, 'tampermonkey.js must expose assistant_post_stop_timeout_ms');
assert.match(source, /function randomBetween\(min,\s*max\)/, 'tampermonkey.js must define randomBetween()');
assert.match(source, /await sleep\(randomBetween\(config\.action_delay_min_ms,\s*config\.action_delay_max_ms\)\)/, 'SET_PROMPT path must wait with action delay');
assert.match(source, /await sleep\(randomBetween\(config\.send_delay_min_ms,\s*config\.send_delay_max_ms\)\)/, 'CLICK_SEND path must wait with send delay');
assert.match(source, /function buildTurnContext\(snapshot,\s*commandId\)/, 'tampermonkey.js must define buildTurnContext()');
assert.match(source, /function isSendAccepted\(reasons\)/, 'tampermonkey.js must define isSendAccepted()');
assert.match(source, /lastAcceptedTurnContext = turnContext;/, 'CLICK_SEND must persist accepted turn context');
assert.match(source, /reason:\s*'missing_send_accept_context'/, 'WAIT_ASSISTANT_DONE must fail without accepted turn context');
assert.match(source, /if \(snapshot\.stop_visible\) \{[\s\S]*?continue;/, 'WAIT_ASSISTANT_DONE must keep waiting while stop_visible is true');
