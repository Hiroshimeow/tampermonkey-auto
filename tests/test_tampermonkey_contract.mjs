import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const scriptPath = path.resolve('tampermonkey.js');
const source = fs.readFileSync(scriptPath, 'utf8');
const metadataVersion = source.match(/\/\/ @version\s+([^\s]+)/)?.[1];
const bridgeVersion = source.match(/const BRIDGE_VERSION = 'standalone-([^']+)'/)?.[1];
assert.equal(bridgeVersion, metadataVersion, 'userscript metadata and bridge runtime versions must stay in sync');
assert.equal(metadataVersion, '1.0.2', 'flow-status UI release must identify itself as version 1.0.2');
assert.match(source, /bridge_version:\s*BRIDGE_VERSION/, 'domSnapshot must expose the active userscript version');
assert.match(source, /let flowStatus = null;/, 'browser poll state must retain only this tab flow status');
assert.match(source, /flowStatus = response\.flow_status \|\| null;/, 'status poll must update flow UI state from backend');
assert.match(source, /function flowStatusMarkup\(status\)/, 'flow status rendering must be isolated in a small helper');
assert.doesNotMatch(source, />QUEUED</, 'flow UI must never render a QUEUED state');
assert.match(source, /composer_watchdog_ms:\s*60000/, 'stale composer watchdog must use a 60 second window');
assert.match(source, /let composerWatchdogTimer = null;/, 'watchdog must run independently from command polling');
assert.match(source, /setInterval\(checkComposerWatchdog,\s*1000\)/, 'watchdog must sample independently once per second');
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
    detail_label: 'From',
    detail_role: 'User',
});
assert.match(runningMarkup, /id="mauto-flow-state"/, 'running state must use a dedicated compact UI element');
assert.match(runningMarkup, /color:#ff5c5c/, 'RUNNING must render red');
assert.match(runningMarkup, />RUNNING</, 'running state label must be visible');
assert.match(runningMarkup, /From: User/, 'turn 1 must show From: User');

const waitingMarkup = flowStatusMarkup({ state: 'WAITING' });
assert.match(waitingMarkup, /color:#d6a84b/, 'WAITING must render amber');
assert.match(waitingMarkup, />WAITING</, 'waiting state label must be visible');
assert.doesNotMatch(waitingMarkup, /mauto-flow-detail/, 'unreached waiting roles must not show predictive detail');
const doneMarkup = flowStatusMarkup({ state: 'DONE', detail_label: 'From', detail_role: 'A' });
assert.match(doneMarkup, /color:#10a37f/, 'DONE must render green');
assert.match(doneMarkup, />DONE</, 'completed role must render DONE');
assert.match(doneMarkup, /From: A/, 'completed role must identify itself');
assert.equal(flowStatusMarkup(null), '', 'nonparticipant role must retain the old UI without a flow block');
assert.equal(flowStatusMarkup({ state: 'QUEUED' }), '', 'unknown states must not render');
const hostileMarkup = flowStatusMarkup({
    state: 'RUNNING',
    detail_label: 'From',
    detail_role: '</div><style>textarea{display:none}</style><div>',
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
