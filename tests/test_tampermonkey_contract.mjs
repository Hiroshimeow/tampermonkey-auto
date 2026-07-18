import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const scriptPath = path.resolve('tampermonkey.js');
const source = fs.readFileSync(scriptPath, 'utf8');
const metadataVersion = source.match(/\/\/ @version\s+([^\s]+)/)?.[1];
const bridgeVersion = source.match(/const BRIDGE_VERSION = 'standalone-([^']+)'/)?.[1];
assert.equal(bridgeVersion, metadataVersion, 'userscript metadata and bridge runtime versions must stay in sync');
assert.equal(metadataVersion, '1.0.4', 'watchdog sampling release must identify itself as version 1.0.4');
assert.match(source, /bridge_version:\s*BRIDGE_VERSION/, 'domSnapshot must expose the active userscript version');
assert.match(source, /let flowStatus = null;/, 'browser poll state must retain only this tab flow status');
assert.match(source, /flowStatus = response\.flow_status \|\| null;/, 'status poll must update flow UI state from backend');
assert.match(source, /function flowStatusMarkup\(status\)/, 'flow status rendering must be isolated in a small helper');
assert.doesNotMatch(source, />QUEUED</, 'flow UI must never render a QUEUED state');
assert.match(source, /composer_watchdog_ms:\s*60000/, 'stale composer watchdog must use a 60 second window');
assert.match(source, /composer_watchdog_poll_ms:\s*20000/, 'stale composer watchdog must sample every 20 seconds');
assert.match(source, /let composerWatchdogTimer = null;/, 'watchdog must run independently from command polling');
assert.match(source, /setInterval\(checkComposerWatchdog,\s*Math\.max\(1000,\s*Number\(config\.composer_watchdog_poll_ms \|\| 20000\)\)\)/, 'watchdog must sample independently every 20 seconds');
assert.match(source, /composer_watchdog_enabled:\s*!!composerWatchdogTimer/, 'DOM snapshot must prove the active tab loaded the watchdog build');
assert.match(source, /composer_watchdog_age_ms:/, 'DOM snapshot must expose watchdog progress for live verification');
assert.match(source, /function composerWatchdogTransition\(/, 'watchdog timing must use a testable state transition');
assert.match(source, /function watchdogComposerElement\(/, 'watchdog must use a strict ChatGPT-composer locator');
assert.match(source, /function isComposerTransportActive\(/, 'watchdog must recognize active composer transport commands');
assert.match(source, /isComposerTransportActive\(activeCommandAction\)/, 'watchdog must not clear while composer transport owns the command');
assert.match(source, /function rebaseComposerWatchdogDuringTransport\(/, 'transport deferral must restart the full watchdog window');
assert.match(source, /selectorButtonCandidates\(selectors, root\)/, 'direct Send selectors must remain inside the strict composer root');
assert.doesNotMatch(source, /const directCandidates = selectorButtonCandidates\(selectors\);/, 'Send lookup must not use page-wide direct selectors');
assert.match(source, /function clearStaleComposer\(/, 'watchdog must have a composer-scoped cleanup helper');
assert.match(source, /function waitForOwnedSendButton\(/, 'CLICK_SEND must use a testable send readiness wait');
assert.match(source, /function isStopButtonMeta\(meta\)/, 'send selection must explicitly reject stop-generation controls');
assert.match(source, /function isSemanticSendButtonMeta\(meta\)/, 'send selection must require an explicit Send semantic');
assert.match(source, /if \(isStopButtonMeta\(meta\)\) \{\s*return null;/, 'composer-scoped send candidates must discard stop controls before scoring');
assert.match(source, /if \(!isSemanticSendButtonMeta\(meta\)\) \{\s*return null;/, 'composer-scoped candidates must discard unrelated submit buttons');

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
assert.match(source, /function chatRootElement\(\)/, 'message parsing must be scoped to the chat root');
assert.match(source, /root\.querySelectorAll\('\[data-message-author-role\]'\)/, 'messageElements must not query the whole document');
assert.match(source, /function imageSummary\(root\)/, 'message parsing must include image metadata');
assert.match(source, /image_count:\s*images\.length/, 'each parsed message must expose image_count');
assert.match(source, /acc\.images = \(acc\.images \|\| 0\) \+ item\.image_count;/, 'message counts must include total images');
assert.match(source, /lastAcceptedTurnContext = turnContext;/, 'CLICK_SEND must persist accepted turn context');
assert.match(source, /reason:\s*'missing_send_accept_context'/, 'WAIT_ASSISTANT_DONE must fail without accepted turn context');
assert.match(source, /const deadline = Date\.now\(\) \+[^;]*timeout_ms/, 'WAIT_ASSISTANT_DONE must have a command-scoped wall clock deadline');
assert.match(source, /Date\.now\(\) >= deadline[\s\S]*?ASSISTANT_TIMEOUT/, 'WAIT_ASSISTANT_DONE deadline must terminate even while STOP remains visible');
assert.match(source, /if \(snapshot\.stop_visible\) \{[\s\S]*?continue;/, 'WAIT_ASSISTANT_DONE must keep waiting while stop_visible is true');
assert.match(source, /function looksIncompleteAssistantText\(text\)/, 'WAIT_ASSISTANT_DONE must detect partial JSON/code block text');
assert.match(source, /!looksIncompleteAssistantText\(finalText\)/, 'WAIT_ASSISTANT_DONE must not report done for incomplete final text');
assert.match(source, /function hasManualComposerInput\(snapshot\)/, 'tampermonkey.js must centralize manual composer detection');
assert.match(source, /function normalizeComposerText\(text\)/, 'composer ownership must use conservative normalization');
assert.match(source, /function composerOwnsExpectedPrompt\(snapshot,\s*expectedText\)/, 'CLICK_SEND must verify expected prompt ownership');
assert.match(source, /composer_prompt_hash:/, 'domSnapshot must expose stable composer prompt identity');
assert.match(source, /page_instance_id:\s*PAGE_INSTANCE_ID/, 'domSnapshot must expose a per-load page generation');
assert.match(source, /const ROLE_OWNER_ID = sessionStorage\.getItem/, 'role ownership must survive a normal reload with a stable tab token');
assert.match(source, /function roleClaimId\(/, 'each role assignment must retain its own claim identity');
assert.match(source, /api\/release-role[\s\S]*?role_claim_id:/, 'role release must carry the claim identity');
assert.match(source, /await report\(reload \? 'ROLE_TAKEOVER_RELOADING' : 'ROLE_SET'[\s\S]*?api\/release-role/, 'A-to-B replacement must report under A before releasing A');
assert.match(source, /if \(\(typeof roleAssignmentGeneration[^}]+\) \|\| nextRole\(\) !== requestRole\) \{\s*return;\s*\}/, 'a stale assignment response must return before applying state');
assert.doesNotMatch(source, /roleAssignmentGeneration[^}]+schedulePoll\(\);\s*return;/, 'stale assignment response must rely on pollOnce finally for its single next poll');
assert.match(source, /const payload = \{[\s\S]*?page_instance_id:\s*PAGE_INSTANCE_ID,[\s\S]*?command_id:/, 'report payload must identify the exact page instance');
assert.match(source, /api\/sync[\s\S]*?page_instance_id:\s*PAGE_INSTANCE_ID/, 'sync payload must identify the exact page instance');
assert.match(source, /api\/sync[\s\S]*?role_owner_id:[\s\S]*?role_claim_id:/, 'the real sync payload must carry stable owner and claim identity');
assert.match(source, /function handleRoleAssignmentMessage\(event\)/, 'the real MAUTO_SET_ROLE callback must be testable as a named handler');
assert.match(source, /api\/reserve-role-claim/, 'a new explicit assignment must reserve a server-issued monotonic claim before polling');
assert.match(source, /async function assignRole\(role\)/, 'role assignment must await claim reservation before publishing a new role');
assert.match(source, /let roleAssignmentIntentGeneration = 0;/, 'reservation responses must be guarded by a separate assignment-intent generation');
assert.match(source, /intentGeneration !== roleAssignmentIntentGeneration/, 'stale reservation responses must be discarded before mutating local ownership');
assert.match(source, /api\/status[\s\S]*?page_instance_id:\s*PAGE_INSTANCE_ID/, 'status payload must identify the exact page instance');
assert.match(source, /page_path:\s*window\.location\.pathname/, 'domSnapshot must expose the current page path');
assert.match(source, /function isRealComposerAttachment\(meta\)/, 'tampermonkey.js must filter composer controls out of attachment detection');
assert.match(source, /composer-plus-btn/, 'composer attachment detection must ignore the Add files button');
assert.match(source, /realComposerAttachmentCount\(snapshot\) > 0/, 'manual input detection must count only real composer attachments');
assert.match(source, /manual_input_pending:/, 'domSnapshot must expose manual_input_pending');
assert.match(source, /PASTE_BLOCKED_MANUAL_INPUT/, 'SET_PROMPT must refuse to overwrite manual composer input');
assert.match(source, /reuse_existing_expected_prompt/, 'SET_PROMPT must reuse an exact automation-owned prompt');
assert.match(source, /composer_ownership_mismatch/, 'SET_PROMPT must reject divergent composer text');
assert.match(source, /expected_prompt_verified:/, 'SET_PROMPT must verify the pasted prompt');
assert.match(source, /SEND_BLOCKED_OWNERSHIP_LOST/, 'CLICK_SEND must stop when expected prompt ownership is lost');
assert.match(source, /await sleep\(randomBetween\(config\.send_delay_min_ms,\s*config\.send_delay_max_ms\)\);\s*let submitAttempt = await attemptOwnedButtonClick\(expectedText\);/, 'CLICK_SEND must refresh ownership and readiness after the randomized delay');
assert.match(source, /composerMatchesExpectedPrompt\(submitAttempt\.snapshot, expectedText\)[\s\S]*?submitAttempt = await attemptOwnedButtonClick\(expectedText\);/, 'CLICK_SEND must wait through a transient upload while the expected prompt text remains owned');
assert.doesNotMatch(source, /requestSubmit/, 'userscript must not add hidden submit retries; Python owns the single retry budget');
assert.match(source, /if \(hasManualComposerInput\(snapshot\)\) \{[\s\S]*?MANUAL_INPUT_PENDING[\s\S]*?continue;/, 'WAIT_ASSISTANT_DONE must not finish or overwrite while the user is steering or has attachments');
assert.match(source, /function handleNavigateNewChat\(command\)/, 'tampermonkey.js must implement new-chat navigation');
assert.match(source, /action === 'NEW_CHAT' \|\| action === 'NAVIGATE_NEW'/, 'NEW_CHAT and NAVIGATE_NEW must be supported');
assert.match(source, /window\.location\.assign\('\/'\)/, 'new-chat navigation must use the current tab to open ChatGPT root');
assert.match(source, /function handleReloadPage\(command,\s*hard = false\)/, 'tampermonkey.js must implement explicit page reload commands');
assert.match(source, /action === 'RESET_PAGE' \|\| action === 'RELOAD_PAGE' \|\| action === 'RELOAD'/, 'reset and reload commands must be supported');
assert.match(source, /action === 'HARD_RELOAD'/, 'hard reload command must be supported');
assert.match(source, /function handleCloseWindow\(command\)/, 'tampermonkey.js must implement close-window command with browser-block reporting');
assert.match(source, /WINDOW_CLOSE_BLOCKED/, 'close-window command must report when the browser blocks tab closing');
assert.match(source, /function handleUploadFiles\(command\)/, 'tampermonkey.js must implement browser file upload command');
assert.match(source, /function uploadPayloadFiles\(payload\)/, 'file upload must rebuild File objects from payload');
assert.match(source, /base64ToBytes\(data\)/, 'file upload must decode local file bytes in browser');
assert.match(source, /new File\(\[bytes\], name, \{ type \}\)/, 'file upload must create real File objects');
assert.match(source, /new ClipboardEvent\('paste'/, 'explicit clipboard-style paste support must remain available');
assert.match(source, /method === 'auto'\s*\? \['nativeValue', 'directTextContent'\]/, 'automatic text injection must avoid clipboard paste that ChatGPT can convert into an attachment');
assert.match(source, /new DragEvent\(eventName/, 'file upload must support drag/drop transport');
assert.match(source, /querySelectorAll\('input\[type="file"\]'\)/, 'file-input upload helper is retained as documented reference code');
assert.match(source, /action === 'UPLOAD_FILE' \|\| action === 'UPLOAD_FILES' \|\| action === 'PASTE_IMAGE' \|\| action === 'PASTE_FILES'/, 'upload command aliases must be routed');
assert.match(source, /composer_attachments:/, 'domSnapshot must expose composer attachment metadata');
assert.match(source, /const composerAttachments = composerAttachmentSummary\(composerRoot\);/, 'attachment detection must use the same composer root as the snapshot');
assert.doesNotMatch(source, /closestComposerRoot\(\) \|\| document/, 'attachment detection must not fall back to scanning the whole page');
assert.match(source, /function choicePromptCandidates\(\)/, 'bridge must detect safe ChatGPT choice prompts when composer is hidden');
assert.match(source, /choice_prompt_pending:/, 'domSnapshot must expose choice prompt blocking state');
assert.match(source, /choice_prompt_candidates:/, 'domSnapshot must expose safe choice prompt candidates');
assert.match(source, /action === 'CLICK_CHOICE_PROMPT'/, 'bridge must support clicking safe choice prompts');
assert.match(source, /CHOICE_PROMPT_CLICKED/, 'choice prompt click command must report success');

assert.match(source, /function dismissUploadOverlays\(\)/, 'upload flow must detect and dismiss stale upload overlays');
assert.match(source, /already uploaded this file/, 'upload overlay cleanup must handle duplicate-file modal');
assert.match(source, /dismiss_overlay_before_upload/, 'UPLOAD_FILES must run overlay cleanup before injecting files');
assert.match(source, /const uploadMethod = 'drop'/, 'UPLOAD_FILES runtime must use drop-only upload transport');
assert.match(source, /const checkAfterAttempt = async \(label\)/, 'UPLOAD_FILES must check success after each individual target attempt');
assert.match(source, /if \(targetAttempted && await checkAfterAttempt/, 'UPLOAD_FILES must stop trying more drop targets after first successful upload');
assert.doesNotMatch(source, /function roleFromUrl\(\)/, 'bridge must not read role from URL');
assert.doesNotMatch(source, /setRole\(urlRole\)/, 'bridge must not persist URL-provided role');
assert.doesNotMatch(source, /searchParams\.set\('mauto_role'/, 'bridge must not write role into URL');
assert.match(source, /api\/claim-role/, 'bridge must claim queued roles through the local server');
assert.match(source, /function clearRole\(\)/, 'bridge must define an explicit role clear path');
assert.match(source, /sessionStorage\.removeItem\('chatgpt_agent_role'\)/, 'clearing role must remove per-tab session role');
assert.match(source, /localStorage\.removeItem\('chatgpt_agent_role'\)/, 'clearing role must remove legacy persisted localStorage role');
assert.doesNotMatch(source, /localStorage\.setItem\('chatgpt_agent_role'/, 'role must not be shared across tabs through localStorage');

function extractFunction(functionName) {
    const asyncMarker = `async function ${functionName}`;
    const syncMarker = `function ${functionName}`;
    const start = source.indexOf(asyncMarker) !== -1 ? source.indexOf(asyncMarker) : source.indexOf(syncMarker);
    assert.notEqual(start, -1, `${functionName} must exist`);
    const bodyMarker = source.indexOf(') {', start);
    assert.notEqual(bodyMarker, -1, `${functionName} body must exist`);
    const braceStart = bodyMarker + 2;
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = braceStart; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) {
                escaped = false;
            } else if (char === '\\') {
                escaped = true;
            } else if (char === quote) {
                quote = '';
            }
            continue;
        }
        if (char === "'" || char === '"' || char === '`') {
            quote = char;
            continue;
        }
        if (char === '{') {
            depth += 1;
        } else if (char === '}') {
            depth -= 1;
            if (depth === 0) {
                return source.slice(start, index + 1);
            }
        }
    }
    throw new Error(`unterminated function ${functionName}`);
}

const context = {};
vm.runInNewContext(
    `${extractFunction('normalizeComposerText')}; ${extractFunction('stopElement')}; ${extractFunction('attemptOwnedButtonClick')}; globalThis.normalizeComposerText = normalizeComposerText; globalThis.stopElement = stopElement; globalThis.attemptOwnedButtonClick = attemptOwnedButtonClick;`,
    context,
);
const normalizeComposerText = context.normalizeComposerText;
const stopElement = context.stopElement;
const attemptOwnedButtonClick = context.attemptOwnedButtonClick;
context.uniqueElements = (elements) => Array.from(new Set(elements.filter(Boolean)));
assert.equal(normalizeComposerText('a\n\n\nb'), 'a\n\nb', 'contenteditable-expanded paragraph breaks must canonicalize');
assert.equal(normalizeComposerText('a\nb'), 'a\nb', 'single line breaks must remain significant');
assert.notEqual(normalizeComposerText('a  b'), normalizeComposerText('a b'), 'ordinary spaces must remain significant');

const sendCandidateContext = {};
vm.runInNewContext(
    `${extractFunction('isStopButtonMeta')}; ${extractFunction('isSemanticSendButtonMeta')}; globalThis.isStopButtonMeta = isStopButtonMeta; globalThis.isSemanticSendButtonMeta = isSemanticSendButtonMeta;`,
    sendCandidateContext,
);
const isStopButtonMeta = sendCandidateContext.isStopButtonMeta;
const isSemanticSendButtonMeta = sendCandidateContext.isSemanticSendButtonMeta;
assert.equal(
    isStopButtonMeta({ aria_label: 'Stop answering', data_testid: 'stop-button', label: '', type: 'submit' }),
    true,
    'visible enabled type=submit Stop answering must never count as Send',
);
assert.equal(
    isStopButtonMeta({ aria_label: 'Send prompt', data_testid: 'send-button', label: '', type: 'submit' }),
    false,
    'real Send prompt must remain eligible',
);
assert.equal(
    isSemanticSendButtonMeta({ aria_label: 'Submit feedback', data_testid: '', label: '', type: 'submit' }),
    false,
    'an unrelated composer submit control must not count as Send',
);
assert.equal(
    isSemanticSendButtonMeta({ aria_label: 'Send prompt', data_testid: 'send-button', label: '', type: 'submit' }),
    true,
    'the real ChatGPT Send control must count as Send',
);

const flowUiContext = {};
vm.runInNewContext(
    `${extractFunction('flowStatusMarkup')}; globalThis.flowStatusMarkup = flowStatusMarkup;`,
    flowUiContext,
);
const flowStatusMarkup = flowUiContext.flowStatusMarkup;
const runningMarkup = flowStatusMarkup({
    state: 'RUNNING',
    from_role: 'User',
});
assert.match(runningMarkup, /id="mauto-flow-state"/, 'running state must use a dedicated compact UI element');
assert.match(runningMarkup, /color:#ff5c5c/, 'RUNNING must render red');
assert.match(runningMarkup, />RUNNING</, 'running state label must be visible');
assert.match(runningMarkup, /From: User/, 'turn 1 must show From: User');

const waitingMarkup = flowStatusMarkup({ state: 'WAITING' });
assert.match(waitingMarkup, /color:#d6a84b/, 'WAITING must render amber');
assert.match(waitingMarkup, />WAITING</, 'waiting state label must be visible');
assert.doesNotMatch(waitingMarkup, /mauto-flow-detail/, 'unreached waiting roles must not show predictive detail');
const doneMarkup = flowStatusMarkup({ state: 'DONE', done_from: 'A', sent_to: 'B' });
assert.match(doneMarkup, /color:#10a37f/, 'DONE must render green');
assert.match(doneMarkup, />DONE</, 'completed role must render DONE');
assert.match(doneMarkup, /Done From: A/, 'completed role must identify the real caller');
assert.match(doneMarkup, /Sent to: B/, 'completed role must render its validated route');
assert.equal(flowStatusMarkup(null), '', 'nonparticipant role must retain the old UI without a flow block');
assert.equal(flowStatusMarkup({ state: 'QUEUED' }), '', 'unknown states must not render');
const hostileMarkup = flowStatusMarkup({
    state: 'RUNNING',
    from_role: '</div><style>textarea{display:none}</style><div>',
});
assert.doesNotMatch(hostileMarkup, /<style>/, 'flow detail must not inject markup into the ChatGPT page');
assert.match(hostileMarkup, /&lt;style&gt;/, 'flow detail must be HTML escaped');

const completionContext = {};
vm.runInNewContext(
    `${extractFunction('jsonBraceDepth')}; ${extractFunction('looksIncompleteAssistantText')}; globalThis.looksIncompleteAssistantText = looksIncompleteAssistantText;`,
    completionContext,
);
const looksIncompleteAssistantText = completionContext.looksIncompleteAssistantText;
assert.equal(looksIncompleteAssistantText('JSON'), true, 'a bare renderer language label must not finish a turn');
assert.equal(looksIncompleteAssistantText('json\n  '), true, 'an empty renderer language block must remain incomplete');
assert.equal(looksIncompleteAssistantText('```json\n```'), true, 'an empty fenced JSON block must remain incomplete');
assert.equal(looksIncompleteAssistantText('```\n```'), true, 'an empty generic code block must remain incomplete');
assert.equal(looksIncompleteAssistantText('JSON\n```json\n```'), true, 'leading renderer label plus empty JSON fence must remain incomplete');
assert.equal(looksIncompleteAssistantText('json\n```\n```'), true, 'lowercase renderer label plus empty generic fence must remain incomplete');
assert.equal(looksIncompleteAssistantText('``` json\n   \n```'), true, 'whitespace before the language and in the body must remain incomplete');
assert.equal(looksIncompleteAssistantText('```json\n{"PLAN":"continue"}\n```'), false, 'valid fenced route JSON may finish browser waiting');
assert.equal(looksIncompleteAssistantText('{"PLAN":"continue"}'), false, 'valid unfenced route JSON may finish browser waiting');
assert.equal(looksIncompleteAssistantText('JSON\n{"PLAN":"continue"}'), false, 'complete route JSON may finish browser waiting');

const watchdogContext = {};
vm.runInNewContext(
    `${extractFunction('composerWatchdogTransition')}; ${extractFunction('isComposerTransportActive')}; ${extractFunction('rebaseComposerWatchdogDuringTransport')}; ${extractFunction('watchdogComposerElement')}; globalThis.composerWatchdogTransition = composerWatchdogTransition; globalThis.isComposerTransportActive = isComposerTransportActive; globalThis.rebaseComposerWatchdogDuringTransport = rebaseComposerWatchdogDuringTransport; globalThis.watchdogComposerElement = watchdogComposerElement;`,
    watchdogContext,
);
const composerWatchdogTransition = watchdogContext.composerWatchdogTransition;
const isComposerTransportActive = watchdogContext.isComposerTransportActive;
const rebaseComposerWatchdogDuringTransport = watchdogContext.rebaseComposerWatchdogDuringTransport;
const watchdogComposerElement = watchdogContext.watchdogComposerElement;
let watchdogState = composerWatchdogTransition(null, '', false, 0, 30000);
assert.equal(watchdogState.action, 'RESET', 'clean composer must reset watchdog state');
watchdogState = composerWatchdogTransition(watchdogState, 'draft-a', true, 1000, 60000);
assert.equal(watchdogState.action, 'WAIT');
assert.equal(watchdogState.started_at, 1000);
watchdogState = composerWatchdogTransition(watchdogState, 'draft-a', true, 60999, 60000);
assert.equal(watchdogState.action, 'WAIT', 'unchanged draft must survive until the full 60 seconds');
watchdogState = composerWatchdogTransition(watchdogState, 'draft-b', true, 61000, 60000);
assert.equal(watchdogState.action, 'WAIT', 'changed draft means user activity and must not be cleared');
assert.equal(watchdogState.started_at, 61000, 'user activity must restart the full window');
watchdogState = composerWatchdogTransition(watchdogState, 'draft-b', true, 120999, 60000);
assert.equal(watchdogState.action, 'WAIT');
watchdogState = composerWatchdogTransition(watchdogState, 'draft-b', true, 121000, 60000);
assert.equal(watchdogState.action, 'CLEAR', 'unchanged dirty composer must clear after 60 seconds');
assert.equal(isComposerTransportActive('CLICK_SEND'), true, 'watchdog must defer a 60-second clear through the final send delay');
assert.equal(isComposerTransportActive('SET_PROMPT'), true);
assert.equal(isComposerTransportActive('UPLOAD_FILES'), true);
assert.equal(isComposerTransportActive('WAIT'), false);
const rebasedWatchdog = rebaseComposerWatchdogDuringTransport(
    { signature: 'automation-prompt', started_at: 1000, action: 'CLEAR' },
    'automation-prompt',
    61000,
);
assert.equal(rebasedWatchdog.action, 'WAIT', 'active transport must cancel the expired CLEAR action');
assert.equal(rebasedWatchdog.started_at, 61000, 'active transport must start a fresh full watchdog window');
assert.equal(
    composerWatchdogTransition(rebasedWatchdog, 'automation-prompt', true, 61001, 60000).action,
    'WAIT',
    'the first tick after transport ends must not clear the composer',
);

const watchdogBoundaryContext = {};
vm.runInNewContext(
    `let stopped = false;
     let activeCommandAction = 'UPLOAD_FILES';
     let composerWatchdogState = { signature: 'automation-prompt', started_at: 1000, action: 'WAIT' };
     const config = { composer_watchdog_ms: 60000 };
     function scheduleSync() {}
     ${extractFunction('composerWatchdogTransition')};
     ${extractFunction('isComposerTransportActive')};
     ${extractFunction('rebaseComposerWatchdogDuringTransport')};
     ${extractFunction('checkComposerWatchdog')};
     globalThis.checkComposerWatchdog = checkComposerWatchdog;
     globalThis.getComposerWatchdogState = () => composerWatchdogState;
     globalThis.finishTransport = () => { activeCommandAction = ''; };`,
    watchdogBoundaryContext,
);
let boundaryClears = 0;
const boundaryDependencies = {
    allowStopped: true,
    composer: () => ({}),
    snapshot: () => ({}),
    signature: () => 'automation-prompt',
    dirty: () => true,
    now: () => 60000,
    timeout_ms: 60000,
    clear: () => { boundaryClears += 1; },
};
watchdogBoundaryContext.checkComposerWatchdog(boundaryDependencies);
assert.equal(watchdogBoundaryContext.getComposerWatchdogState().started_at, 60000, 'transport at age 59 seconds must rebase immediately');
watchdogBoundaryContext.finishTransport();
watchdogBoundaryContext.checkComposerWatchdog({ ...boundaryDependencies, now: () => 61000 });
assert.equal(boundaryClears, 0, 'the first tick after the transport boundary must retain the composer');

const unrelatedTextarea = { id: 'feedback' };
const strictRootWithoutComposer = {
    querySelectorAll(selector) {
        return selector === 'textarea' ? [unrelatedTextarea] : [];
    },
};
assert.equal(
    watchdogComposerElement({ root: strictRootWithoutComposer, isVisible: () => true, isDisabled: () => false }),
    null,
    'watchdog must ignore a page containing only an unrelated textarea',
);
const realComposer = { id: 'prompt-textarea' };
const strictRootWithComposer = {
    querySelectorAll(selector) {
        return selector === 'div#prompt-textarea' ? [realComposer] : [];
    },
};
assert.equal(
    watchdogComposerElement({ root: strictRootWithComposer, isVisible: () => true, isDisabled: () => false }),
    realComposer,
    'watchdog must recognize the actual ChatGPT prompt textarea',
);

const buttonMetaContext = {};
vm.runInNewContext(`${extractFunction('buttonMeta')}; globalThis.buttonMeta = buttonMeta;`, buttonMetaContext);
const missingButtonMeta = buttonMetaContext.buttonMeta(null);
assert.equal(missingButtonMeta.label, '');
assert.equal(missingButtonMeta.aria_label, null);
assert.equal(missingButtonMeta.data_testid, null);

const readinessContext = {};
vm.runInNewContext(
    `${extractFunction('waitForOwnedSendButton')}; globalThis.waitForOwnedSendButton = waitForOwnedSendButton;`,
    readinessContext,
);
const waitForOwnedSendButton = readinessContext.waitForOwnedSendButton;
let readinessClock = 0;
let readinessRead = 0;
const readinessStates = [
    { snapshot: { composer_text: 'expected', send_enabled: null, attachments: 0 }, button: null },
    { snapshot: { composer_text: 'expected', send_enabled: false, attachments: 0 }, button: { disabled: true } },
    { snapshot: { composer_text: 'expected', send_enabled: true, attachments: 0 }, button: { disabled: false } },
];
const readiness = await waitForOwnedSendButton('expected', 1000, {
    now: () => readinessClock,
    sleep: async (ms) => { readinessClock += ms; },
    poll_ms: 100,
    stopped: () => false,
    read: () => readinessStates[Math.min(readinessRead++, readinessStates.length - 1)],
    matches: (snapshot, expected) => snapshot.composer_text === expected,
    owns: (snapshot, expected) => snapshot.composer_text === expected && snapshot.attachments === 0,
    clickable: (button) => !!button && !button.disabled,
});
assert.equal(readiness.status, 'READY', 'send readiness must wait through missing and disabled button states');
assert.equal(readinessRead, 3, 'send readiness must re-read the live button before proceeding');
const uploadReadiness = await waitForOwnedSendButton('', 1000, {
    now: () => 0,
    sleep: async () => {},
    poll_ms: 100,
    stopped: () => false,
    read: () => ({ snapshot: { composer_text: 'upload prompt', send_enabled: true }, button: { disabled: false } }),
    matches: () => false,
    owns: () => false,
    clickable: (button) => !!button && !button.disabled,
});
assert.equal(uploadReadiness.status, 'READY', 'upload flow without expected_text must still proceed once Send is ready');

const sidebarStop = { id: 'sidebar-conversation-stop-title' };
context.selectFirst = () => sidebarStop;
assert.equal(
    stopElement({ root: { querySelectorAll: () => [] }, isVisible: () => true }),
    null,
    'a global sidebar button containing Stop must not count as generation activity',
);
const hiddenStop = { id: 'hidden-stop' };
const visibleStop = { id: 'visible-stop' };
assert.equal(
    stopElement({
        root: { querySelectorAll: () => [hiddenStop, visibleStop] },
        isVisible: (element) => element === visibleStop,
    }),
    visibleStop,
    'generation detection must select a visible stop button inside the composer root',
);
const buttonRef = { element: { id: 'send' }, strategy: 'test' };
const owns = (snapshot, expectedText) => snapshot.composer_text === expectedText && snapshot.attachment_count === 0;

async function executeAttempt(snapshot, click) {
    return attemptOwnedButtonClick('expected prompt', {
        snapshot: () => ({ ...snapshot }),
        findButton: () => buttonRef,
        isClickable: () => true,
        owns,
        click,
    });
}

let clickCount = 0;
let outcome = await executeAttempt(
    { composer: true, composer_text: 'manual mutation', attachment_count: 0, send_enabled: true },
    () => { clickCount += 1; },
);
assert.equal(outcome.status, 'SEND_BLOCKED_OWNERSHIP_LOST');
assert.equal(clickCount, 0, 'text mutation during delay must not click send');

outcome = await executeAttempt(
    { composer: true, composer_text: 'expected prompt', attachment_count: 1, send_enabled: true },
    () => { clickCount += 1; },
);
assert.equal(outcome.status, 'SEND_BLOCKED_OWNERSHIP_LOST');
assert.equal(clickCount, 0, 'attachment added during delay must not click send');

outcome = await executeAttempt(
    { composer: true, composer_text: 'expected prompt', attachment_count: 0, send_enabled: undefined },
    () => { clickCount += 1; },
);
assert.equal(outcome.status, 'SEND_FAILED');
assert.equal(clickCount, 0, 'unknown send readiness must not click send');

outcome = await executeAttempt(
    { composer: true, composer_text: 'expected prompt', attachment_count: 0, send_enabled: true },
    () => { throw new Error('click failed'); },
);
assert.equal(outcome.status, 'SEND_FAILED');
assert.equal(outcome.reason, 'send_click_threw');
assert.equal(clickCount, 0, 'click failure must not invoke an unowned fallback submit');

outcome = await executeAttempt(
    { composer: true, composer_text: 'expected prompt', attachment_count: 0, send_enabled: true },
    () => { clickCount += 1; },
);
assert.equal(outcome.status, 'CLICKED');
assert.equal(clickCount, 1, 'owned ready prompt must click exactly once');

assert.match(source, /let activeCommandRole = '';/, 'command execution must retain an immutable leased role');
assert.match(source, /function requestRole\(commandId = ''\)/, 'report and sync role selection must be centralized');
assert.match(source, /activeCommandRole = role;[\s\S]*?await executeCommand\(command\)/, 'polling must bind the exact leasing role before command execution');
assert.match(source, /activeCommandRole = '';[\s\S]*?activeCommandAction = '';/, 'command cleanup must clear the immutable role with active command state');
assert.match(source, /function start\(\)[\s\S]*?activeCommandRole = '';/, 'start must clear stale command role state');
assert.match(source, /function stop\(\)[\s\S]*?activeCommandRole = '';/, 'stop must clear stale command role state');
assert.match(source, /const heartbeatEveryMs = Math\.min\([\s\S]*?5000/, 'silent command heartbeat must be capped at five seconds');

const phase1HeartbeatContext = {};
vm.runInNewContext(
    `let stopped = false;
     let clockMs = 0;
     let reports = [];
     let snapshotFactory = () => ({
         stop_visible: true,
         composer_text_len: 0,
         composer_attachments: [],
         messages: {
             counts: { user: 1, assistant: 0, images: 0 },
             messages: [],
             last_user: { text: 'prompt' },
             last_assistant: { text: '' }
         }
     });
     let lastAcceptedTurnContext = {
         before_user_count: 1,
         before_assistant_count: 0,
         before_last_assistant_text: '',
         accepted_at: 1
     };
     const config = {
         report_wait_every_ms: 20000,
         assistant_quiet_ms: 1000,
         assistant_force_sync_quiet_ms: 5000,
         assistant_post_stop_timeout_ms: 15000,
         auto_reload_on_assistant_timeout: false,
         reload_after_timeout_ms: 0
     };
     const Date = { now: () => clockMs };
     const window = { location: { reload() {} } };
     function setTimeout() {}
     function domSnapshot() { return snapshotFactory(clockMs); }
     function hasManualComposerInput() { return false; }
     function looksIncompleteAssistantText(text) { return !String(text || '').trim(); }
     async function report(state, commandId, extra = {}) {
         reports.push({ state, commandId, extra, at: clockMs });
         return { status: 'OK' };
     }
     async function sleep(ms) { clockMs += ms; }
     async function syncTranscript() {
         const snapshot = domSnapshot();
         return {
             snapshot,
             transcript: {
                 messages: snapshot.messages.messages,
                 counts: snapshot.messages.counts,
                 last_user: snapshot.messages.last_user,
                 last_assistant: snapshot.messages.last_assistant
             },
             response: { status: 'OK' }
         };
     }
     ${extractFunction('handleWaitAssistantDone')};
     globalThis.runSilentTimeout = async () => {
         clockMs = 0;
         reports = [];
         stopped = false;
         snapshotFactory = () => ({
             stop_visible: true,
             composer_text_len: 0,
             composer_attachments: [],
             messages: {
                 counts: { user: 1, assistant: 0, images: 0 },
                 messages: [],
                 last_user: { text: 'prompt' },
                 last_assistant: { text: '' }
             }
         });
         lastAcceptedTurnContext = {
             before_user_count: 1,
             before_assistant_count: 0,
             before_last_assistant_text: '',
             accepted_at: 1
         };
         await handleWaitAssistantDone({ command_id: 'cmd-silent', payload: { timeout_ms: 30000 } });
         return { reports, clockMs };
     };
     globalThis.runHydratedCompletion = async () => {
         clockMs = 0;
         reports = [];
         stopped = false;
         snapshotFactory = (now) => {
             const done = now >= 6000;
             return {
                 stop_visible: !done,
                 composer_text_len: 0,
                 composer_attachments: [],
                 messages: {
                     counts: { user: 1, assistant: done ? 1 : 0, images: 0 },
                     messages: done ? [{ role: 'assistant', text: '{\"PLAN\":\"continue\"}' }] : [],
                     last_user: { text: 'prompt' },
                     last_assistant: done ? { text: '{\"PLAN\":\"continue\"}' } : { text: '' }
                 }
             };
         };
         lastAcceptedTurnContext = {
             before_user_count: 1,
             before_assistant_count: 0,
             before_last_assistant_text: '',
             accepted_at: 1
         };
         await handleWaitAssistantDone({ command_id: 'cmd-done', payload: { timeout_ms: 30000 } });
         return { reports, clockMs };
     };`,
    phase1HeartbeatContext,
);
const phase1SilentWait = await phase1HeartbeatContext.runSilentTimeout();
const phase1SilentHeartbeats = phase1SilentWait.reports.filter((item) => item.extra?.result?.heartbeat === true);
const phase1SilentTerminals = phase1SilentWait.reports.filter((item) => item.state === 'ASSISTANT_TIMEOUT' || item.state === 'ASSISTANT_DONE');
assert.ok(phase1SilentHeartbeats.length >= 2, 'a 30-second unchanged response must emit repeated owner heartbeats');
assert.ok(phase1SilentHeartbeats[0].at < 10000, 'the first silent owner heartbeat must occur before the backend online TTL');
assert.ok(phase1SilentHeartbeats.every((item) => item.state === 'ASSISTANT_PROGRESS'), 'heartbeats must remain nonterminal progress reports');
assert.ok(phase1SilentHeartbeats.every((item) => item.commandId === 'cmd-silent'), 'heartbeats must retain the active command id');
assert.ok(phase1SilentHeartbeats.every((item) => item.extra.dom_info.stop_visible === true), 'heartbeats must include the current unchanged snapshot');
assert.equal(phase1SilentTerminals.length, 1, 'silent timeout must emit exactly one terminal result');
assert.equal(phase1SilentTerminals[0].state, 'ASSISTANT_TIMEOUT');
assert.equal(phase1SilentTerminals[0].at, 30000, 'heartbeats must not move the original command deadline');
assert.equal(phase1SilentWait.reports.at(-1).state, 'ASSISTANT_TIMEOUT', 'no heartbeat may occur after terminal timeout');

const phase1HydratedWait = await phase1HeartbeatContext.runHydratedCompletion();
const phase1HydratedTerminals = phase1HydratedWait.reports.filter((item) => item.state === 'ASSISTANT_TIMEOUT' || item.state === 'ASSISTANT_DONE');
assert.equal(phase1HydratedTerminals.length, 1, 'hydrated completion must emit one terminal result');
assert.equal(phase1HydratedTerminals[0].state, 'ASSISTANT_DONE');
assert.equal(phase1HydratedWait.reports.at(-1).state, 'ASSISTANT_DONE', 'no heartbeat may occur after hydrated completion');

const phase1ImmutableRoleContext = {};
vm.runInNewContext(
    `let stopped = false;
     let visibleRole = 'A';
     let activeCommandId = 'cmd-role';
     let activeCommandRole = 'A';
     const PAGE_INSTANCE_ID = 'page-a';
     const ROLE_OWNER_ID = 'tab-a';
     const SERVER_URL = 'http://127.0.0.1:8500';
     const config = { reload_after_timeout_ms: 0 };
     const requests = [];
     const window = { location: { pathname: '/c/a', href: 'https://chatgpt.com/c/a', assign() {} } };
     function setTimeout() {}
     function roleClaimId(role) { return role === 'A' ? 'claim-a' : 'claim-b'; }
     function nextRole() { return visibleRole; }
     function setRole(role) { visibleRole = String(role || '').trim().toUpperCase(); return visibleRole; }
     async function assignRole(role) { return setRole(role); }
     function cleanNavigationUrl(value) { return String(value || '/'); }
     function domSnapshot() {
         return {
             messages: {
                 messages: [],
                 counts: { user: 1, assistant: 0, images: 0 },
                 last_user: { text: 'prompt' },
                 last_assistant: null
             }
         };
     }
     async function request(method, url, payload) { requests.push({ method, url, payload }); return { status: 'OK' }; }
     function updateConfig() {}
     ${extractFunction('requestRole')};
     ${extractFunction('report')};
     ${extractFunction('syncTranscript')};
     ${extractFunction('roleFromPayload')};
     ${extractFunction('handleSetOrTakeoverRole')};
     globalThis.runRoleChange = async () => {
         await handleSetOrTakeoverRole({ command_id: 'cmd-role', payload: { role: 'B' } }, false);
         await syncTranscript('command-sync');
         activeCommandId = 'cmd-normal';
         activeCommandRole = 'A';
         await report('PROBE_DONE', 'cmd-normal', {});
         activeCommandRole = '';
         activeCommandId = '';
         await report('IDLE_REPORT', '', {});
         await syncTranscript('idle-sync');
         return { requests, visibleRole };
     };`,
    phase1ImmutableRoleContext,
);
const phase1RoleChangeRun = await phase1ImmutableRoleContext.runRoleChange();
const phase1RoleChangeReport = phase1RoleChangeRun.requests.find((item) => item.url.endsWith('/api/report') && item.payload.state === 'ROLE_SET');
const phase1CommandSync = phase1RoleChangeRun.requests.find((item) => item.url.endsWith('/api/sync') && item.payload.reason === 'command-sync');
const phase1NormalCommandReport = phase1RoleChangeRun.requests.find((item) => item.url.endsWith('/api/report') && item.payload.state === 'PROBE_DONE');
const phase1IdleReport = phase1RoleChangeRun.requests.find((item) => item.url.endsWith('/api/report') && item.payload.state === 'IDLE_REPORT');
const phase1IdleSync = phase1RoleChangeRun.requests.find((item) => item.url.endsWith('/api/sync') && item.payload.reason === 'idle-sync');
assert.equal(phase1RoleChangeRun.visibleRole, 'B', 'role-changing command must update the visible tab role');
assert.equal(phase1RoleChangeReport.payload.role, 'A', 'A-to-B role change must report under immutable leased role A');
assert.equal(phase1CommandSync.payload.role, 'A', 'command-scoped sync must remain under immutable leased role A');
assert.equal(phase1NormalCommandReport.payload.role, 'A', 'normal commands must retain their leased role');
assert.equal(phase1IdleReport.payload.role, 'B', 'ordinary reports must return to visible role B after cleanup');
assert.equal(phase1IdleSync.payload.role, 'B', 'ordinary sync must return to visible role B after cleanup');
assert.equal(phase1CommandSync.payload.role_owner_id, 'tab-a', 'real current-owner command sync must include the stable owner identity');
assert.equal(phase1CommandSync.payload.role_claim_id, 'claim-a', 'real current-owner command sync must include the leased claim identity');

const sameRoleCommandContext = {};
vm.runInNewContext(
    `let stopped = false;
     let visibleRole = 'A';
     let activeCommandId = 'cmd-same-role';
     let activeCommandRole = 'A';
     let roleAssignmentGeneration = 0;
     let roleAssignmentIntentGeneration = 0;
     let roleClaimPending = false;
     let flowStatus = null;
     const PAGE_INSTANCE_ID = 'page-a';
     const ROLE_OWNER_ID = 'tab-a';
     const SERVER_URL = 'http://127.0.0.1:8500';
     const config = { reload_after_timeout_ms: 0 };
     const storage = new Map([['chatgpt_agent_role', 'A'], ['mauto_role_claim_id:A', 'claim-old']]);
     const sessionStorage = { getItem(key) { return storage.get(key) || ''; }, setItem(key, value) { storage.set(key, String(value)); }, removeItem(key) { storage.delete(key); } };
     const localStorage = { removeItem() {} };
     const window = { location: { pathname: '/c/a', href: 'https://chatgpt.com/c/a', assign() {} } };
     const reports = [];
     const requests = [];
     function setTimeout() {}
     function roleClaimKey(role) { return 'mauto_role_claim_id:' + String(role || '').trim().toUpperCase(); }
     function roleClaimId(role = nextRole()) { return sessionStorage.getItem(roleClaimKey(role)) || ''; }
     function beginRoleClaim(role) { sessionStorage.setItem(roleClaimKey(role), 'claim-new'); return 'claim-new'; }
     function nextRole() { return visibleRole; }
     function cleanNavigationUrl(value) { return String(value || '/'); }
     function domSnapshot() { return { messages: { messages: [], counts: {}, last_user: null, last_assistant: null } }; }
     async function request(method, url, payload) { requests.push({ url, payload }); if (url.endsWith('/api/report')) reports.push(payload); return { status: 'OK' }; }
     function updateConfig() {}
     ${extractFunction('setRole')};
     ${extractFunction('assignRole')};
     ${extractFunction('requestRole')};
     ${extractFunction('report')};
     ${extractFunction('roleFromPayload')};
     ${extractFunction('handleSetOrTakeoverRole')};
     globalThis.run = async () => { await handleSetOrTakeoverRole({ command_id: 'cmd-same-role', payload: { role: 'A' } }, false); return { reports, requests, claim: roleClaimId('A'), visibleRole }; };`,
    sameRoleCommandContext,
);
const sameRoleCommand = await sameRoleCommandContext.run();
assert.equal(sameRoleCommand.reports.length, 1, 'same-role command must emit exactly one terminal report');
assert.equal(sameRoleCommand.reports[0].state, 'ROLE_SET');
assert.equal(sameRoleCommand.reports[0].role_claim_id, 'claim-old', 'same-role terminal report must retain the leased claim');
assert.equal(sameRoleCommand.claim, 'claim-old', 'same-role assignment must not create a replacement claim');
assert.equal(sameRoleCommand.requests.filter((item) => item.url.endsWith('/api/reserve-role-claim')).length, 0, 'same-role assignment must skip claim reservation');

const reservationOrderContext = {};
vm.runInNewContext(
    `let roleAssignmentGeneration = 0;
     let roleAssignmentIntentGeneration = 0;
     let roleClaimPending = false;
     let flowStatus = null;
     const SERVER_URL = 'http://127.0.0.1:8500';
     const storage = new Map([['chatgpt_agent_role', 'A'], ['mauto_role_claim_id:A', 'g-0-a']]);
     const sessionStorage = { getItem(key) { return storage.get(key) || ''; }, setItem(key, value) { storage.set(key, String(value)); }, removeItem(key) { storage.delete(key); } };
     const localStorage = { removeItem() {} };
     const pending = [];
     function nextRole() { return sessionStorage.getItem('chatgpt_agent_role') || 'NONE'; }
     function roleClaimKey(role) { return 'mauto_role_claim_id:' + String(role || '').trim().toUpperCase(); }
     function roleClaimId(role = nextRole()) { return sessionStorage.getItem(roleClaimKey(role)) || ''; }
     function beginRoleClaim(role, claimId) { sessionStorage.setItem(roleClaimKey(role), claimId); return claimId; }
     function clearRole() { sessionStorage.removeItem('chatgpt_agent_role'); return ''; }
     function updateConfig() {}
     async function request(method, url, payload) { return new Promise((resolve) => pending.push({ url, payload, resolve })); }
     ${extractFunction('setRole')};
     ${extractFunction('assignRole')};
     globalThis.run = async () => {
         const older = assignRole('B');
         const newer = assignRole('C');
         pending[1].resolve({ role_claim_id: 'g-2-c' });
         await newer;
         const afterNew = { role: nextRole(), b: roleClaimId('B'), c: roleClaimId('C') };
         pending[0].resolve({ role_claim_id: 'g-1-b' });
         await older;
         return { afterNew, afterOld: { role: nextRole(), b: roleClaimId('B'), c: roleClaimId('C') }, roleClaimPending };
     };`,
    reservationOrderContext,
);
const reservationOrder = await reservationOrderContext.run();
assert.equal(reservationOrder.afterNew.role, 'C', 'newer completed reservation must own local role state');
assert.equal(reservationOrder.afterNew.b, '', 'newer reservation must not persist the older claim');
assert.equal(reservationOrder.afterNew.c, 'g-2-c');
assert.equal(reservationOrder.afterOld.role, 'C', 'late older reservation must not mutate the newer role');
assert.equal(reservationOrder.afterOld.b, '', 'late older reservation must not persist its claim');
assert.equal(reservationOrder.afterOld.c, 'g-2-c');

async function runReservationHandlerScenario(actions, assignRoleSource = extractFunction('assignRole')) {
    const context = {};
    vm.runInNewContext(
        `let stopped = false;
         let roleAssignmentGeneration = 0;
         let roleAssignmentIntentGeneration = 0;
         let roleClaimPending = false;
         let pollTimer = null;
         let flowStatus = null;
         const PAGE_INSTANCE_ID = 'page-a';
         const ROLE_OWNER_ID = 'tab-a';
         const SERVER_URL = 'http://127.0.0.1:8500';
         const config = { poll_ms: 800 };
         const storage = new Map([['chatgpt_agent_role', 'A'], ['mauto_role_claim_id:A', 'g-0-a']]);
         const sessionStorage = { getItem(k) { return storage.get(k) || ''; }, setItem(k, v) { storage.set(k, String(v)); }, removeItem(k) { storage.delete(k); } };
         const localStorage = { removeItem() {} };
         const window = { location: { origin: 'https://chatgpt.com', pathname: '/c/a' } };
         const pending = []; const requests = []; const releases = []; const timers = new Map(); let timerId = 0; let scheduleCalls = 0;
         function setTimeout(fn) { scheduleCalls += 1; const id = ++timerId; timers.set(id, fn); return id; }
         function clearTimeout(id) { timers.delete(id); }
         function nextRole() { return sessionStorage.getItem('chatgpt_agent_role') || 'NONE'; }
         function roleClaimKey(role) { return 'mauto_role_claim_id:' + String(role || '').trim().toUpperCase(); }
         function roleClaimId(role = nextRole()) { return sessionStorage.getItem(roleClaimKey(role)) || ''; }
         function beginRoleClaim(role, claim) { sessionStorage.setItem(roleClaimKey(role), claim); return claim; }
         let promptValue = null;
         function prompt() { return promptValue; }
         function updateConfig() {} function updateUI() {}
         async function request(method, url, payload) { requests.push({ url, payload }); if (url.endsWith('/api/reserve-role-claim')) return new Promise((resolve, reject) => pending.push({ payload, resolve, reject })); if (url.endsWith('/api/release-role')) releases.push(payload); return { status: 'OK' }; }
         ${extractFunction('clearRole')}; ${extractFunction('setRole')}; ${assignRoleSource}; ${extractFunction('releaseRoleClaim')}; ${extractFunction('handleClearRole')}; ${extractFunction('handleManualSetRole')}; ${extractFunction('schedulePoll')}; ${extractFunction('handleRoleAssignmentMessage')};
         globalThis.start = (role) => handleRoleAssignmentMessage({ origin: 'https://chatgpt.com', data: { type: 'MAUTO_SET_ROLE', role } });
         globalThis.clear = () => handleClearRole();
         globalThis.manual = (role) => { promptValue = role; return handleManualSetRole(); };
         globalThis.resolve = (index, value) => pending[index].resolve(typeof value === 'string' ? { role_claim_id: value } : value);
         globalThis.reject = (index) => pending[index].reject(new Error('reservation failed'));
         globalThis.state = () => ({ role: nextRole(), a: roleClaimId('A'), b: roleClaimId('B'), c: roleClaimId('C'), pending: roleClaimPending, releases: releases.length, timers: timers.size, scheduleCalls, requests });`,
        context,
    );
    return actions(context);
}

const cFirst = await runReservationHandlerScenario(async (h) => { const b = h.start('B'); const c = h.start('C'); h.resolve(1, 'g-2-c'); await c; const beforeLate = h.state(); h.resolve(0, 'g-1-b'); await b; return { beforeLate, after: h.state() }; });
assert.equal(cFirst.after.role, 'C'); assert.equal(cFirst.after.b, ''); assert.equal(cFirst.after.releases, 1); assert.equal(cFirst.after.timers, 1); assert.equal(cFirst.after.scheduleCalls, 1); assert.equal(cFirst.after.releases, cFirst.beforeLate.releases); assert.equal(cFirst.after.scheduleCalls, cFirst.beforeLate.scheduleCalls);
const bFirst = await runReservationHandlerScenario(async (h) => { const b = h.start('B'); const c = h.start('C'); h.resolve(0, 'g-1-b'); await b; h.resolve(1, 'g-2-c'); await c; return h.state(); });
assert.equal(bFirst.role, 'C'); assert.equal(bFirst.b, ''); assert.equal(bFirst.releases, 1); assert.equal(bFirst.timers, 1); assert.equal(bFirst.scheduleCalls, 1);
const sameCancels = await runReservationHandlerScenario(async (h) => { const b = h.start('B'); await h.start('A'); h.resolve(0, 'g-1-b'); await b; return h.state(); });
assert.equal(sameCancels.role, 'A'); assert.equal(sameCancels.b, ''); assert.equal(sameCancels.releases, 0); assert.equal(sameCancels.timers, 1); assert.equal(sameCancels.scheduleCalls, 1);
const clearCancels = await runReservationHandlerScenario(async (h) => { const b = h.start('B'); await h.clear(); h.resolve(0, 'g-1-b'); await b; return h.state(); });
assert.equal(clearCancels.role, 'NONE'); assert.equal(clearCancels.b, ''); assert.equal(clearCancels.releases, 1); assert.equal(clearCancels.timers, 0); assert.equal(clearCancels.scheduleCalls, 0);
const reserveFailure = await runReservationHandlerScenario(async (h) => { const b = h.start('B'); h.reject(0); try { await b; } catch (_) {} return h.state(); });
assert.equal(reserveFailure.role, 'A'); assert.equal(reserveFailure.a, 'g-0-a'); assert.equal(reserveFailure.b, ''); assert.equal(reserveFailure.pending, false);
assert.equal(reserveFailure.releases, 0); assert.equal(reserveFailure.scheduleCalls, 0); assert.equal(reserveFailure.requests.filter((item) => item.url.endsWith('/api/status') && item.payload?.role === 'B' && item.payload?.claim_role === true).length, 0);
const manualReserveFailure = await runReservationHandlerScenario(async (h) => { const b = h.manual('B'); h.reject(0); await b; return h.state(); });
assert.equal(manualReserveFailure.role, 'A', 'manual role assignment must remain on A after reservation transport failure'); assert.equal(manualReserveFailure.a, 'g-0-a'); assert.equal(manualReserveFailure.b, ''); assert.equal(manualReserveFailure.pending, false);
assert.equal(manualReserveFailure.releases, 0); assert.equal(manualReserveFailure.scheduleCalls, 0); assert.equal(manualReserveFailure.requests.filter((item) => item.url.endsWith('/api/status') && item.payload?.role === 'B' && item.payload?.claim_role === true).length, 0);
for (const badReservation of [{}, { role_claim_id: '' }, null]) {
    const bad = await runReservationHandlerScenario(async (h) => { const b = h.start('B'); h.resolve(0, badReservation); await b; return h.state(); });
    assert.equal(bad.role, 'A'); assert.equal(bad.a, 'g-0-a'); assert.equal(bad.b, ''); assert.equal(bad.pending, false); assert.equal(bad.releases, 0); assert.equal(bad.scheduleCalls, 0);
    assert.equal(bad.requests.filter((item) => item.url.endsWith('/api/status') && item.payload?.role === 'B' && item.payload?.claim_role === true).length, 0);
}
const unguardedAssignRole = extractFunction('assignRole').replace(/\s*if \(intentGeneration !== roleAssignmentIntentGeneration\) return '';\n/, '\n');
const behavioralRed = await runReservationHandlerScenario(async (h) => { const b = h.start('B'); const c = h.start('C'); h.resolve(1, 'g-2-c'); await c; h.resolve(0, 'g-1-b'); await b; return h.state(); }, unguardedAssignRole);
assert.throws(() => assert.equal(behavioralRed.role, 'C', 'late older reservation must not mutate the newer role'), /late older reservation must not mutate the newer role/, 'the unguarded mutant must fail the normal final-role-C invariant');

const commandReservationFailureContext = {};
vm.runInNewContext(
    `let stopped = false;
     let activeCommandId = 'cmd-role';
     let activeCommandRole = 'A';
     let roleAssignmentGeneration = 0;
     let roleAssignmentIntentGeneration = 0;
     let roleClaimPending = false;
     let flowStatus = null;
     const PAGE_INSTANCE_ID = 'page-a';
     const ROLE_OWNER_ID = 'tab-a';
     const SERVER_URL = 'http://127.0.0.1:8500';
     const storage = new Map([['chatgpt_agent_role', 'A'], ['mauto_role_claim_id:A', 'g-0-a']]);
     const sessionStorage = { getItem(k) { return storage.get(k) || ''; }, setItem(k, v) { storage.set(k, String(v)); }, removeItem(k) { storage.delete(k); } };
     const localStorage = { removeItem() {} };
     const window = { location: { pathname: '/c/a', href: 'https://chatgpt.com/c/a' } };
     const requests = []; const releases = []; const console = { warn() {} };
     function nextRole() { return sessionStorage.getItem('chatgpt_agent_role') || 'NONE'; }
     function roleClaimKey(role) { return 'mauto_role_claim_id:' + String(role || '').trim().toUpperCase(); }
     function roleClaimId(role = nextRole()) { return sessionStorage.getItem(roleClaimKey(role)) || ''; }
     function beginRoleClaim(role, claim) { sessionStorage.setItem(roleClaimKey(role), claim); return claim; }
     function clearRole() { sessionStorage.removeItem('chatgpt_agent_role'); return ''; }
     function updateConfig() {}
     function cleanNavigationUrl(value) { return String(value || '/'); }
     function domSnapshot() { return { messages: { messages: [], counts: {}, last_user: null, last_assistant: null } }; }
     async function request(method, url, payload) { requests.push({ url, payload }); if (url.endsWith('/api/reserve-role-claim')) throw new Error('reservation failed'); if (url.endsWith('/api/release-role')) releases.push(payload); return { status: 'OK' }; }
     ${extractFunction('setRole')}; ${extractFunction('assignRole')}; ${extractFunction('requestRole')}; ${extractFunction('report')}; ${extractFunction('roleFromPayload')}; ${extractFunction('handleSetOrTakeoverRole')};
     globalThis.run = async () => { await handleSetOrTakeoverRole({ command_id: 'cmd-role', payload: { role: 'B' } }, false); return { role: nextRole(), a: roleClaimId('A'), b: roleClaimId('B'), pending: roleClaimPending, releases, requests }; };`,
    commandReservationFailureContext,
);
const commandReservationFailure = await commandReservationFailureContext.run();
const commandFailureReports = commandReservationFailure.requests.filter((item) => item.url.endsWith('/api/report') && item.payload.state === 'ROLE_TAKEOVER_FAILED');
assert.equal(commandReservationFailure.role, 'A'); assert.equal(commandReservationFailure.a, 'g-0-a'); assert.equal(commandReservationFailure.b, ''); assert.equal(commandReservationFailure.pending, false); assert.equal(commandReservationFailure.releases.length, 0);
assert.equal(commandFailureReports.length, 1, 'command reservation failure must emit exactly one terminal failure report');
assert.equal(commandFailureReports[0].payload.role, 'A'); assert.equal(commandFailureReports[0].payload.role_claim_id, 'g-0-a'); assert.equal(commandFailureReports[0].payload.command_id, 'cmd-role'); assert.equal(commandFailureReports[0].payload.result.reason, 'claim_reservation_failed');
assert.equal(commandReservationFailure.requests.filter((item) => item.url.endsWith('/api/status') && item.payload?.role === 'B' && item.payload?.claim_role === true).length, 0);

const assignmentPollRaceContext = {};
vm.runInNewContext(
    `let stopped = false;
     let visibleRole = 'A';
     let roleAssignmentGeneration = 0;
     let roleAssignmentIntentGeneration = 0;
     let roleClaimPending = false;
     let pollTimer = null;
     let nextTimerId = 0;
     const timers = new Map();
     const PAGE_INSTANCE_ID = 'page-a';
     const ROLE_OWNER_ID = 'tab-a';
     const SERVER_URL = 'http://127.0.0.1:8500';
     const config = { poll_ms: 800 };
     let flowStatus = null;
     const storage = new Map([['chatgpt_agent_role', 'A']]);
     const sessionStorage = { getItem(key) { return storage.get(key) || ''; }, setItem(key, value) { storage.set(key, String(value)); }, removeItem(key) { storage.delete(key); } };
     const localStorage = { removeItem() {} };
     const window = { location: { pathname: '/c/a', origin: 'https://chatgpt.com' } };
     const console = { warn() {} };
     let resolveStatus;
     const requests = [];
     function setTimeout(callback) { const id = ++nextTimerId; timers.set(id, callback); return id; }
     function clearTimeout(id) { timers.delete(id); }
     function nextRole() { return sessionStorage.getItem('chatgpt_agent_role') || 'NONE'; }
     function roleClaimKey(role) { return 'mauto_role_claim_id:' + String(role || '').trim().toUpperCase(); }
     function roleClaimId(role = nextRole()) { return sessionStorage.getItem(roleClaimKey(role)); }
     function beginRoleClaim(role, claimId) { storage.set(roleClaimKey(role), claimId); return claimId; }
     function ensureUIAttached() {}
     function updateUI() {}
     function updateConfig() {}
     function domSnapshot() { return { messages: { messages: [], counts: {}, last_user: null, last_assistant: null } }; }
     async function claimQueuedRole() { return 'NONE'; }
     async function request(method, url, payload) { requests.push({ url, payload }); if (url.endsWith('/api/status')) return new Promise((resolve) => { resolveStatus = resolve; }); if (url.endsWith('/api/reserve-role-claim')) return { role_claim_id: 'g-2-opaque', config: {} }; return { status: 'OK' }; }
     ${extractFunction('clearRole')};
     ${extractFunction('setRole')};
     ${extractFunction('releaseRoleClaim')};
     ${extractFunction('assignRole')};
     ${extractFunction('handleRoleAssignmentMessage')};
     ${extractFunction('pollOnce')};
     ${extractFunction('schedulePoll')};
     globalThis.run = async () => {
         const oldPoll = pollOnce();
         await handleRoleAssignmentMessage({ origin: 'https://chatgpt.com', data: { type: 'MAUTO_SET_ROLE', role: 'B' } });
         const pendingAfterAssignment = timers.size;
         resolveStatus({ command: { action: 'WAIT' }, config: {}, flow_status: null });
         await oldPoll;
         const assignedPoll = pollOnce();
         resolveStatus({ command: { action: 'WAIT' }, config: {}, flow_status: null });
         await assignedPoll;
         return { visibleRole: nextRole(), pendingAfterAssignment, pendingAfterOldResponse: timers.size, roleClaimPending, requests };
     };`,
    assignmentPollRaceContext,
);
const assignmentPollRace = await assignmentPollRaceContext.run();
assert.equal(assignmentPollRace.visibleRole, 'B', 'the real assignment handler must switch to B');
assert.equal(assignmentPollRace.pendingAfterAssignment, 1, 'the real assignment callback must schedule one poll');
assert.equal(assignmentPollRace.pendingAfterOldResponse, 1, 'the stale in-flight poll must leave exactly one pending next poll');
const assignmentReserveIndex = assignmentPollRace.requests.findIndex((item) => item.url.endsWith('/api/reserve-role-claim'));
const assignmentClaimIndex = assignmentPollRace.requests.findIndex((item) => item.url.endsWith('/api/status') && item.payload.role === 'B');
assert.ok(assignmentReserveIndex >= 0 && assignmentClaimIndex > assignmentReserveIndex, 'new assignment must reserve then persist and publish its claimed status poll');

const heartbeatFailurePollContext = {};
vm.runInNewContext(
    `let stopped = false;
     let clockMs = 0;
     let activeCommandId = '';
     let activeCommandAction = '';
     let activeCommandRole = '';
     let flowStatus = null;
     let lastAcceptedTurnContext = {
         before_user_count: 1,
         before_assistant_count: 0,
         before_last_assistant_text: '',
         accepted_at: 1
     };
     let statusCalls = 0;
     let scheduledPolls = 0;
     let firstHeartbeatFailed = false;
     const reportAttempts = [];
     const warnings = [];
     const command = {
         command_id: 'cmd-heartbeat',
         action: 'WAIT_ASSISTANT_DONE',
         payload: { timeout_ms: 30000 }
     };
     const config = {
         poll_ms: 800,
         report_wait_every_ms: 20000,
         assistant_quiet_ms: 1000,
         assistant_force_sync_quiet_ms: 5000,
         assistant_post_stop_timeout_ms: 15000,
         auto_reload_on_assistant_timeout: false,
         reload_after_timeout_ms: 0
     };
     const Date = { now: () => clockMs };
     const PAGE_INSTANCE_ID = 'page-a';
     const SERVER_URL = 'http://127.0.0.1:8500';
     const window = { location: { pathname: '/c/a', reload() {} } };
     const console = { warn(...args) { warnings.push(args); } };
     function setTimeout() {}
     function nextRole() { return 'A'; }
     function ensureUIAttached() {}
     function updateUI() {}
     function updateConfig() {}
     function schedulePoll() { scheduledPolls += 1; }
     function domSnapshot() {
         return {
             stop_visible: true,
             composer_text_len: 0,
             composer_attachments: [],
             messages: {
                 counts: { user: 1, assistant: 0, images: 0 },
                 messages: [],
                 last_user: { text: 'prompt' },
                 last_assistant: { text: '' }
             }
         };
     }
     function hasManualComposerInput() { return false; }
     function looksIncompleteAssistantText(text) { return !String(text || '').trim(); }
     async function sleep(ms) { clockMs += ms; }
     async function request(method, url, payload) {
         if (url.endsWith('/api/status')) {
             statusCalls += 1;
             return { command, config: {}, flow_status: null };
         }
         if (url.endsWith('/api/report')) {
             const attempt = { payload, at: clockMs, failed: false };
             reportAttempts.push(attempt);
             if (payload.result && payload.result.heartbeat === true && !firstHeartbeatFailed) {
                 firstHeartbeatFailed = true;
                 attempt.failed = true;
                 throw new Error('temporary bridge transport failure');
             }
             return { status: 'OK' };
         }
         throw new Error('unexpected request ' + url);
     }
     ${extractFunction('requestRole')};
     ${extractFunction('report')};
     ${extractFunction('handleWaitAssistantDone')};
     ${extractFunction('executeCommand')};
     ${extractFunction('pollOnce')};
     globalThis.run = async () => {
         await pollOnce();
         const afterFirstPoll = { activeCommandId, activeCommandRole, activeCommandAction };
         await pollOnce();
         return {
             activeCommandId,
             activeCommandRole,
             activeCommandAction,
             afterFirstPoll,
             statusCalls,
             scheduledPolls,
             reportAttempts,
             warnings,
             clockMs
         };
     };`,
    heartbeatFailurePollContext,
);
const heartbeatFailurePoll = await heartbeatFailurePollContext.run();
const failedHeartbeatAttempts = heartbeatFailurePoll.reportAttempts.filter((item) => item.failed);
const successfulHeartbeatAttempts = heartbeatFailurePoll.reportAttempts.filter((item) => item.payload.result?.heartbeat === true && !item.failed);
const heartbeatFailureTerminals = heartbeatFailurePoll.reportAttempts.filter((item) => item.payload.state === 'ASSISTANT_TIMEOUT' || item.payload.state === 'ASSISTANT_DONE');
assert.equal(failedHeartbeatAttempts.length, 1, 'the first heartbeat transport attempt must fail exactly once');
assert.equal(failedHeartbeatAttempts[0].at, 5000, 'the first capped heartbeat attempt must occur at five seconds');
assert.equal(failedHeartbeatAttempts[0].payload.role, 'A', 'failed heartbeat must retain immutable leased role A');
assert.equal(heartbeatFailurePoll.warnings.length, 1, 'transient heartbeat failure must be logged locally exactly once');
assert.match(String(heartbeatFailurePoll.warnings[0][0]), /heartbeat report failed/);
assert.ok(successfulHeartbeatAttempts.length >= 1, 'the same command loop must later deliver a successful heartbeat');
assert.equal(successfulHeartbeatAttempts[0].at, 10000, 'failed heartbeat retry must remain rate-limited by the existing interval');
assert.ok(successfulHeartbeatAttempts.every((item) => item.payload.command_id === 'cmd-heartbeat'));
assert.ok(successfulHeartbeatAttempts.every((item) => item.payload.role === 'A'), 'later heartbeat must retain immutable leased role A');
assert.equal(heartbeatFailureTerminals.length, 1, 'heartbeat failure recovery must still emit exactly one terminal result');
assert.equal(heartbeatFailureTerminals[0].payload.state, 'ASSISTANT_TIMEOUT');
assert.equal(heartbeatFailureTerminals[0].at, 30000, 'heartbeat transport failure must not move the original deadline');
assert.equal(heartbeatFailurePoll.reportAttempts.at(-1).payload.state, 'ASSISTANT_TIMEOUT', 'no heartbeat may occur after terminal timeout');
assert.equal(heartbeatFailurePoll.statusCalls, 2, 'the duplicate-ID guard must be exercised by a second status poll');
assert.equal(heartbeatFailurePoll.scheduledPolls, 2, 'both poll executions must retain normal scheduling');
assert.equal(heartbeatFailurePoll.afterFirstPoll.activeCommandId, 'cmd-heartbeat');
assert.equal(heartbeatFailurePoll.activeCommandId, 'cmd-heartbeat', 'handled failure must not clear the command ID to force re-execution');
assert.equal(heartbeatFailurePoll.activeCommandRole, '');
assert.equal(heartbeatFailurePoll.activeCommandAction, '');
assert.equal(heartbeatFailurePoll.clockMs, 30000, 'duplicate delivery must not execute the completed browser command twice');
