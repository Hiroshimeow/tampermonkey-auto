import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const scriptPath = path.resolve('tampermonkey.js');
const source = fs.readFileSync(scriptPath, 'utf8');
const metadataVersion = source.match(/\/\/ @version\s+([^\s]+)/)?.[1];
const bridgeVersion = source.match(/const BRIDGE_VERSION = 'standalone-([^']+)'/)?.[1];
assert.equal(bridgeVersion, metadataVersion, 'userscript metadata and bridge runtime versions must stay in sync');
assert.match(source, /bridge_version:\s*BRIDGE_VERSION/, 'domSnapshot must expose the active userscript version');

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
