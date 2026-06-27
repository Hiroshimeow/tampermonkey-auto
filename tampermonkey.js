// ==UserScript==
// @name         MAuto Diagnostic Bridge Standalone
// @namespace    http://tampermonkey.net/
// @version      1.0.0
// @description  Standalone MAuto bridge without unsafe-eval or hot reload.
// @match        https://chatgpt.com/*
// @match        https://*.chatgpt.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    const SERVER_URL = 'http://127.0.0.1:8500';
    const BRIDGE_VERSION = 'standalone-1.0.0';

    const DEFAULT_CONFIG = {
        poll_ms: 800,
        sync_debounce_ms: 1200,
        wait_loop_interval_ms: 500,
        action_delay_min_ms: 3000,
        action_delay_max_ms: 5000,
        send_delay_min_ms: 2000,
        send_delay_max_ms: 5000,
        composer_stable_samples: 6,
        composer_stable_sample_ms: 300,
        assistant_quiet_ms: 2500,
        report_wait_every_ms: 1500,
        max_button_dump: 80,
        send_accept_timeout_ms: 10000,
        send_accept_poll_ms: 400,
        assistant_force_sync_quiet_ms: 5000,
        assistant_post_stop_timeout_ms: 15000,
        auto_reload_on_assistant_timeout: true,
        reload_after_timeout_ms: 1500
    };

    let stopped = false;
    let pollTimer = null;
    let syncTimer = null;
    let observer = null;
    let uiContainer = null;
    let config = { ...DEFAULT_CONFIG };
    let activeCommandId = '';
    let lastSyncHash = '';
    let lastAcceptedTurnContext = null;

    function sleep(ms) {
        return new Promise((resolve) => {
            if (stopped) {
                resolve();
                return;
            }
            setTimeout(resolve, ms);
        });
    }

    function randomBetween(min, max) {
        const normalizedMin = Math.max(0, Number(min) || 0);
        const normalizedMax = Math.max(normalizedMin, Number(max) || normalizedMin);
        return Math.floor(Math.random() * (normalizedMax - normalizedMin + 1)) + normalizedMin;
    }

    function nextRole() {
        return (sessionStorage.getItem('chatgpt_agent_role') || 'None').trim().toUpperCase() || 'None';
    }

    function setRole(role) {
        sessionStorage.setItem('chatgpt_agent_role', role);
    }

    function isVisible(el) {
        if (!el) {
            return false;
        }
        if (typeof el.getAttribute === 'function') {
            if (el.getAttribute('aria-hidden') === 'true' || el.hidden) {
                return false;
            }
        }
        if (typeof window.getComputedStyle === 'function') {
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {
                return false;
            }
        }
        if (typeof el.getBoundingClientRect === 'function') {
            const rect = el.getBoundingClientRect();
            if (!rect || rect.width <= 0 || rect.height <= 0) {
                return false;
            }
        }
        return true;
    }

    function isDisabled(el) {
        if (!el) {
            return true;
        }
        if (el.disabled || el.readOnly) {
            return true;
        }
        if (typeof el.getAttribute === 'function') {
            return el.getAttribute('aria-disabled') === 'true' || el.getAttribute('disabled') !== null;
        }
        return false;
    }

    function selectFirst(selectors) {
        for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (el) {
                return el;
            }
        }
        return null;
    }

    function composerElement() {
        const selectors = [
            'div#prompt-textarea',
            '[data-testid="composer"] div[contenteditable="true"]',
            'form div[contenteditable="true"]',
            'textarea[data-id="root"]',
            'textarea',
            '.ProseMirror',
            'div[contenteditable="true"]'
        ];
        const candidates = [];
        for (const selector of selectors) {
            try {
                candidates.push(...Array.from(document.querySelectorAll(selector)));
            } catch (error) {
                console.warn('[MAuto Bridge] bad composer selector', selector, error);
            }
        }
        const uniqueCandidates = uniqueElements(candidates);
        return uniqueCandidates.find((el) => isVisible(el) && !isDisabled(el))
            || uniqueCandidates.find((el) => !isDisabled(el))
            || uniqueCandidates[0]
            || null;
    }

    function stopElement() {
        return selectFirst([
            'button[aria-label*="Stop"]',
            'button[aria-label*="generation"]',
            'button[data-testid*="stop"]'
        ]);
    }

    function chatRootElement() {
        return document.querySelector('main') || document.body;
    }

    function messageElements() {
        const root = chatRootElement();
        return Array.from(root.querySelectorAll('[data-message-author-role]')).filter((node) => {
            const role = node.getAttribute('data-message-author-role') || '';
            return ['user', 'assistant'].includes(role) && isVisible(node);
        });
    }

    function imageSummary(root) {
        if (!root) {
            return [];
        }
        return Array.from(root.querySelectorAll('img')).map((img) => {
            const rect = typeof img.getBoundingClientRect === 'function' ? img.getBoundingClientRect() : null;
            return {
                src: img.currentSrc || img.src || '',
                alt: img.getAttribute('alt') || '',
                title: img.getAttribute('title') || '',
                natural_width: Number(img.naturalWidth || 0),
                natural_height: Number(img.naturalHeight || 0),
                rendered_width: rect ? Math.round(rect.width) : 0,
                rendered_height: rect ? Math.round(rect.height) : 0,
                visible: isVisible(img),
                path: domPath(img)
            };
        }).filter((item) => item.src || item.alt || item.visible);
    }

    function textOf(el) {
        if (!el) {
            return '';
        }
        return String(el.innerText || el.textContent || el.value || '').trim();
    }

    function clearComposerText(el) {
        if (!el) {
            return;
        }

        try {
            if ('value' in el) {
                el.value = '';
            }
            el.textContent = '';
            el.innerText = '';
            if (typeof document.execCommand === 'function') {
                el.focus();
                document.execCommand('selectAll', false);
                document.execCommand('delete', false);
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        } catch (error) {
            console.warn('[MAuto Bridge] clearComposerText failed', error);
        }
    }

    function setComposerText(el, text, method) {
        if (!el) {
            return false;
        }

        const methods = method === 'auto'
            ? ['execCommand', 'nativeValue', 'directTextContent']
            : [method];

        for (const currentMethod of methods) {
            try {
                el.focus();
                clearComposerText(el);

                if (currentMethod === 'execCommand') {
                    const dt = new DataTransfer();
                    dt.setData('text/plain', text);
                    el.dispatchEvent(new ClipboardEvent('paste', {
                        bubbles: true,
                        cancelable: true,
                        clipboardData: dt
                    }));
                    if (!textOf(el) && typeof document.execCommand === 'function') {
                        document.execCommand('insertText', false, text);
                    }
                } else if (currentMethod === 'nativeValue') {
                    if ('value' in el) {
                        el.value = text;
                    } else {
                        el.textContent = text;
                        el.innerText = text;
                    }
                } else if (currentMethod === 'directTextContent') {
                    el.textContent = text;
                    el.innerText = text;
                    if ('value' in el) {
                        el.value = text;
                    }
                }

                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));

                if (textOf(el)) {
                    return true;
                }
            } catch (error) {
                console.warn('[MAuto Bridge] setComposerText failed', currentMethod, error);
            }
        }

        return false;
    }


    function normalizeBase64(data) {
        return String(data || '')
            .replace(/^data:[^;]+;base64,/i, '')
            .replace(/\s+/g, '');
    }

    function base64ToBytes(data) {
        const normalized = normalizeBase64(data);
        if (!normalized) {
            throw new Error('missing_base64_data');
        }
        const binary = atob(normalized);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) {
            bytes[i] = binary.charCodeAt(i);
        }
        return bytes;
    }

    function uploadPayloadFiles(payload) {
        const entries = Array.isArray(payload.files) && payload.files.length ? payload.files : [payload];
        return entries.map((entry, index) => {
            const item = entry || {};
            const name = String(item.filename || item.name || `upload-${index + 1}.png`);
            const type = String(item.mime_type || item.type || item.mime || 'image/png');
            const data = item.data_b64 || item.file_b64 || item.b64 || item.base64 || item.data || '';
            const bytes = base64ToBytes(data);
            return new File([bytes], name, { type });
        });
    }

    function buildFileDataTransfer(files, text = '') {
        const dt = new DataTransfer();
        for (const file of files) {
            dt.items.add(file);
        }
        if (text) {
            dt.setData('text/plain', text);
        }
        return dt;
    }

    function dispatchClipboardLikeEvent(target, eventName, dataTransfer) {
        const init = {
            bubbles: true,
            cancelable: true,
            composed: true
        };
        let event;
        if (eventName === 'paste') {
            event = new ClipboardEvent('paste', { ...init, clipboardData: dataTransfer });
            if (!event.clipboardData) {
                Object.defineProperty(event, 'clipboardData', { value: dataTransfer });
            }
        } else {
            event = new DragEvent(eventName, { ...init, dataTransfer });
            if (!event.dataTransfer) {
                Object.defineProperty(event, 'dataTransfer', { value: dataTransfer });
            }
        }
        return target.dispatchEvent(event);
    }

    function visibleFileInputs() {
        return Array.from(document.querySelectorAll('input[type="file"]')).map((input) => {
            const accept = input.getAttribute ? input.getAttribute('accept') : '';
            return {
                element: input,
                accept: accept || '',
                multiple: !!input.multiple,
                disabled: isDisabled(input),
                visible: isVisible(input),
                path: domPath(input)
            };
        });
    }

    function tryAssignFilesToInput(files) {
        const inputs = visibleFileInputs()
            .filter((item) => !item.disabled)
            .sort((a, b) => {
                const aImage = a.accept.toLowerCase().includes('image') ? 1 : 0;
                const bImage = b.accept.toLowerCase().includes('image') ? 1 : 0;
                if (aImage !== bImage) {
                    return bImage - aImage;
                }
                return Number(b.visible) - Number(a.visible);
            });

        for (const item of inputs) {
            try {
                const dt = buildFileDataTransfer(files);
                Object.defineProperty(item.element, 'files', {
                    value: dt.files,
                    configurable: true
                });
                item.element.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                item.element.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                return { ok: true, input: { accept: item.accept, path: item.path, visible: item.visible } };
            } catch (error) {
                console.warn('[MAuto Bridge] file input assignment failed', error);
            }
        }
        return { ok: false, input: null };
    }

    function composerAttachmentSummary() {
        const root = closestComposerRoot() || document;
        const buttons = root && root.querySelectorAll
            ? Array.from(root.querySelectorAll('button,[role="button"]'))
            : [];
        return buttons.map((button) => buttonMeta(button)).filter((meta) => {
            const label = `${meta.label || ''} ${meta.aria_label || ''} ${meta.data_testid || ''}`.toLowerCase();
            return label.includes('open image')
                || label.includes('remove file')
                || label.includes('attached')
                || label.includes('upload')
                || label.includes('file');
        });
    }

    function dismissUploadOverlays() {
        const bodyText = ((document.body && (document.body.innerText || document.body.textContent)) || '').toLowerCase();
        const matched = bodyText.includes("already uploaded this file")
            || (bodyText.includes('add anything') && bodyText.includes('drop any file here'));
        const clicked = [];
        if (!matched) {
            return { matched: false, clicked, escaped: false };
        }

        const candidates = Array.from(document.querySelectorAll('button,[role="button"]'));
        for (const button of candidates) {
            const meta = buttonMeta(button);
            const label = `${meta.label || ''} ${meta.aria_label || ''} ${meta.data_testid || ''}`.trim().toLowerCase();
            if (!label) {
                continue;
            }
            if (label === 'ok' || label.includes('close') || label.includes('dismiss') || label.includes('cancel')) {
                try {
                    button.click();
                    clicked.push(meta);
                    break;
                } catch (error) {
                    console.warn('[MAuto Bridge] overlay button click failed', error);
                }
            }
        }

        const eventInit = { key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true, cancelable: true, composed: true };
        for (const target of uniqueElements([document.activeElement, document.body, document])) {
            try {
                target.dispatchEvent(new KeyboardEvent('keydown', eventInit));
                target.dispatchEvent(new KeyboardEvent('keyup', eventInit));
            } catch (error) {
                console.warn('[MAuto Bridge] overlay escape dispatch failed', error);
            }
        }
        return { matched: true, clicked, escaped: true };
    }
    function uploadSucceeded(beforeAttachments, snapshot) {
        const afterAttachments = snapshot.composer_attachments || [];
        if (afterAttachments.length > beforeAttachments.length) {
            return true;
        }
        return afterAttachments.some((meta) => {
            const label = `${meta.label || ''} ${meta.aria_label || ''} ${meta.data_testid || ''}`.toLowerCase();
            return label.includes('remove file') || label.includes('open image');
        });
    }

    function buttonMeta(button) {
        return {
            label: textOf(button),
            aria_label: button.getAttribute ? button.getAttribute('aria-label') : null,
            data_testid: button.getAttribute ? button.getAttribute('data-testid') : null,
            disabled: isDisabled(button),
            visible: isVisible(button),
            tag: button && button.tagName ? String(button.tagName).toLowerCase() : null,
            type: button && button.type ? button.type : null
        };
    }

    function sendScore(button) {
        const meta = buttonMeta(button);
        const haystack = `${meta.label} ${meta.aria_label || ''} ${meta.data_testid || ''}`.toLowerCase();
        return {
            testid: haystack.includes('send') ? 4 : 0,
            aria: haystack.includes('send') ? 3 : 0,
            label: haystack.includes('send') ? 2 : 0,
            visible: meta.visible ? 1 : 0,
            enabled: !meta.disabled ? 1 : 0
        };
    }

    function sendButtonCandidates() {
        return Array.from(document.querySelectorAll('button,[role="button"]')).map((button) => {
            const scores = sendScore(button);
            const total = Object.values(scores).reduce((sum, value) => sum + value, 0);
            return {
                element: button,
                meta: buttonMeta(button),
                scores,
                total
            };
        }).sort((a, b) => b.total - a.total);
    }

    function isClickableSendButton(button) {
        return !!button && !isDisabled(button) && isVisible(button);
    }

    function uniqueElements(elements) {
        return Array.from(new Set(elements.filter(Boolean)));
    }

    function selectorButtonCandidates(selectors, root = document) {
        const elements = [];
        for (const selector of selectors) {
            try {
                elements.push(...Array.from(root.querySelectorAll(selector)));
            } catch (error) {
                console.warn('[MAuto Bridge] bad send selector', selector, error);
            }
        }

        return uniqueElements(elements).map((button) => {
            const scores = sendScore(button);
            const total = Object.values(scores).reduce((sum, value) => sum + value, 0);
            return {
                element: button,
                meta: buttonMeta(button),
                scores,
                total
            };
        }).sort((a, b) => {
            const aClickable = isClickableSendButton(a.element) ? 1 : 0;
            const bClickable = isClickableSendButton(b.element) ? 1 : 0;
            if (aClickable !== bClickable) {
                return bClickable - aClickable;
            }
            return b.total - a.total;
        });
    }

    function closestComposerRoot() {
        const composer = composerElement();
        if (!composer) {
            return null;
        }

        if (composer.closest) {
            return composer.closest('form')
                || composer.closest('[data-testid="composer"]')
                || composer.closest('[data-testid="composer-root"]')
                || composer.parentElement
                || composer;
        }

        return composer.parentElement || composer;
    }

    function scopedSendButtonCandidates() {
        const root = closestComposerRoot();
        if (!root || !root.querySelectorAll) {
            return [];
        }

        return Array.from(root.querySelectorAll('button,[role="button"]')).map((button) => {
            const meta = buttonMeta(button);
            const haystack = `${meta.label} ${meta.aria_label || ''} ${meta.data_testid || ''}`.toLowerCase();
            const scores = {
                exact_testid: meta.data_testid === 'send-button' ? 10 : 0,
                exact_aria: meta.aria_label === 'Send prompt' || meta.aria_label === 'Send' ? 8 : 0,
                contains_send: haystack.includes('send') ? 4 : 0,
                submit_type: meta.type === 'submit' ? 3 : 0,
                visible: meta.visible ? 1 : 0,
                enabled: !meta.disabled ? 1 : 0
            };
            const total = Object.values(scores).reduce((sum, value) => sum + value, 0);
            return {
                element: button,
                meta,
                scores,
                total
            };
        }).sort((a, b) => {
            const aClickable = isClickableSendButton(a.element) ? 1 : 0;
            const bClickable = isClickableSendButton(b.element) ? 1 : 0;
            if (aClickable !== bClickable) {
                return bClickable - aClickable;
            }
            return b.total - a.total;
        });
    }

    function domPath(node) {
        if (!node || !node.tagName) {
            return '';
        }
        const parts = [];
        let current = node;
        let steps = 0;
        while (current && current.tagName && steps < 5) {
            let part = String(current.tagName).toLowerCase();
            if (current.id) {
                part += `#${current.id}`;
            }
            const testid = current.getAttribute ? current.getAttribute('data-testid') : null;
            if (testid) {
                part += `[data-testid="${testid}"]`;
            }
            parts.unshift(part);
            current = current.parentElement;
            steps += 1;
        }
        return parts.join(' > ');
    }

    function findSendButton() {
        const selectors = [
            'button[data-testid="send-button"]',
            'button[aria-label="Send prompt"]',
            'button[aria-label="Send"]',
            '[data-testid="send-button"]'
        ];

        const scoped = scopedSendButtonCandidates();
        const scopedClickable = scoped.find((item) => isClickableSendButton(item.element) && item.total >= 4);
        if (scopedClickable) {
            return {
                element: scopedClickable.element,
                strategy: 'composer_scoped_clickable',
                matched_selector: null
            };
        }

        const directCandidates = selectorButtonCandidates(selectors);
        const directClickable = directCandidates.find((item) => isClickableSendButton(item.element));
        if (directClickable) {
            return {
                element: directClickable.element,
                strategy: 'direct_selector_clickable',
                matched_selector: selectors.find((selector) => {
                    try {
                        return Array.from(document.querySelectorAll(selector)).includes(directClickable.element);
                    } catch (error) {
                        return false;
                    }
                }) || 'unknown'
            };
        }

        if (scoped.length > 0 && scoped[0].total >= 4) {
            return {
                element: scoped[0].element,
                strategy: 'composer_scoped_disabled_or_hidden',
                matched_selector: null
            };
        }

        if (directCandidates.length > 0) {
            return {
                element: directCandidates[0].element,
                strategy: 'direct_selector_disabled_or_hidden',
                matched_selector: selectors.find((selector) => {
                    try {
                        return Array.from(document.querySelectorAll(selector)).includes(directCandidates[0].element);
                    } catch (error) {
                        return false;
                    }
                }) || 'unknown'
            };
        }

        return null;
    }

    function messageSummary() {
        const messages = messageElements().map((node) => {
            const role = node.getAttribute('data-message-author-role') || 'unknown';
            const text = textOf(node);
            const images = imageSummary(node);
            return {
                role,
                text,
                length: text.length,
                image_count: images.length,
                images,
                path: domPath(node),
                visible: isVisible(node)
            };
        });

        const counts = messages.reduce((acc, item) => {
            acc[item.role] = (acc[item.role] || 0) + 1;
            acc.images = (acc.images || 0) + item.image_count;
            return acc;
        }, {});

        const assistants = messages.filter((item) => item.role === 'assistant');
        const users = messages.filter((item) => item.role === 'user');

        return {
            messages,
            counts,
            last_assistant: assistants.length ? assistants[assistants.length - 1] : null,
            last_user: users.length ? users[users.length - 1] : null
        };
    }

    function domSnapshot() {
        const composer = composerElement();
        const sendButtonRef = findSendButton();
        const sendButton = sendButtonRef ? sendButtonRef.element : null;
        const stopButton = stopElement();
        const messages = messageSummary();
        const composerRoot = closestComposerRoot();
        const composerButtons = scopedSendButtonCandidates().slice(0, 12).map((item) => ({
            meta: item.meta,
            scores: item.scores,
            total: item.total
        }));
        const buttons = sendButtonCandidates().slice(0, config.max_button_dump).map((item) => ({
            meta: item.meta,
            scores: item.scores,
            total: item.total
        }));

        return {
            composer: !!composer,
            composer_text: textOf(composer),
            composer_text_len: textOf(composer).length,
            composer_path: composer ? domPath(composer) : '',
            composer_root_path: composerRoot ? domPath(composerRoot) : '',
            composer_buttons: composerButtons,
            composer_attachments: composerAttachmentSummary(),
            file_inputs: visibleFileInputs().map((item) => ({
                accept: item.accept,
                multiple: item.multiple,
                disabled: item.disabled,
                visible: item.visible,
                path: item.path
            })),
            send_enabled: sendButton ? !sendButton.disabled : null,
            selected_button: sendButton ? buttonMeta(sendButton) : null,
            selection_strategy: sendButtonRef ? sendButtonRef.strategy : null,
            selected_button_path: sendButton ? domPath(sendButton) : '',
            matched_selector: sendButtonRef ? sendButtonRef.matched_selector : null,
            stop_visible: isVisible(stopButton),
            voice_visible: !!selectFirst(['button[aria-label*="voice"]', 'button[aria-label*="Voice"]']),
            buttons,
            messages
        };
    }

    function stableHash(obj) {
        try {
            return JSON.stringify(obj);
        } catch (error) {
            return String(Date.now());
        }
    }

    function messageSignature(message) {
        if (!message) {
            return '';
        }
        return stableHash({
            role: message.role || '',
            length: message.length || 0,
            text: message.text || ''
        });
    }

    function buildTurnContext(snapshot, commandId) {
        return {
            command_id: commandId || '',
            before_user_count: snapshot.messages.counts.user || 0,
            before_assistant_count: snapshot.messages.counts.assistant || 0,
            before_last_user_sig: messageSignature(snapshot.messages.last_user),
            before_last_assistant_sig: messageSignature(snapshot.messages.last_assistant),
            before_last_user_text: snapshot.messages.last_user ? snapshot.messages.last_user.text : '',
            before_last_assistant_text: snapshot.messages.last_assistant ? snapshot.messages.last_assistant.text : '',
            accepted_at: 0,
            acceptance_snapshot: null,
            acceptance_reasons: null
        };
    }

    function sendAcceptanceReasons(turnContext, snapshot, beforeComposerLen) {
        const currentUserSig = messageSignature(snapshot.messages.last_user);
        const currentAssistantSig = messageSignature(snapshot.messages.last_assistant);
        return {
            composer_cleared: snapshot.composer_text_len < beforeComposerLen,
            user_count_increased: (snapshot.messages.counts.user || 0) > turnContext.before_user_count,
            assistant_count_increased: (snapshot.messages.counts.assistant || 0) > turnContext.before_assistant_count,
            last_user_changed: currentUserSig && currentUserSig !== turnContext.before_last_user_sig,
            last_assistant_changed: currentAssistantSig && currentAssistantSig !== turnContext.before_last_assistant_sig,
            stop_visible: snapshot.stop_visible
        };
    }

    function isSendAccepted(reasons) {
        return !!(
            reasons.user_count_increased ||
            reasons.last_user_changed ||
            reasons.stop_visible
        );
    }

    function updateConfig(data) {
        if (data && data.config && typeof data.config === 'object') {
            config = { ...config, ...data.config };
        }
    }

    function request(method, url, body) {
        return new Promise((resolve, reject) => {
            if (typeof GM_xmlhttpRequest !== 'function') {
                fetch(url, {
                    method,
                    headers: body ? { 'Content-Type': 'application/json' } : undefined,
                    body: body ? JSON.stringify(body) : undefined,
                    credentials: 'omit'
                }).then(async (res) => {
                    const text = await res.text();
                    resolve(text ? JSON.parse(text) : null);
                }).catch(reject);
                return;
            }
            GM_xmlhttpRequest({
                method,
                url,
                headers: body ? { 'Content-Type': 'application/json' } : undefined,
                data: body ? JSON.stringify(body) : undefined,
                onload: (res) => {
                    try {
                        const parsed = res.responseText ? JSON.parse(res.responseText) : null;
                        resolve(parsed);
                    } catch (error) {
                        resolve(null);
                    }
                },
                onerror: reject
            });
        });
    }

    async function report(state, commandId, extra = {}) {
        if (stopped) {
            return null;
        }
        const role = nextRole();
        if (role === 'NONE') {
            return null;
        }
        const payload = {
            role,
            session_id: window.location.pathname || '',
            command_id: commandId || '',
            state,
            text: extra.text || '',
            result: extra.result || {},
            dom_info: extra.dom_info || domSnapshot()
        };
        const response = await request('POST', `${SERVER_URL}/api/report`, payload);
        updateConfig(response);
        return response;
    }

    async function syncTranscript(reason) {
        if (stopped) {
            return null;
        }
        const role = nextRole();
        if (role === 'NONE') {
            return null;
        }
        const snapshot = domSnapshot();
        const transcript = {
            messages: snapshot.messages.messages,
            counts: snapshot.messages.counts,
            last_user: snapshot.messages.last_user,
            last_assistant: snapshot.messages.last_assistant
        };
        const response = await request('POST', `${SERVER_URL}/api/sync`, {
            role,
            session_id: window.location.pathname || '',
            reason,
            transcript,
            snapshot
        });
        updateConfig(response);
        return { snapshot, transcript, response };
    }

    function scheduleSync(reason) {
        if (syncTimer) {
            clearTimeout(syncTimer);
        }
        syncTimer = setTimeout(async () => {
            syncTimer = null;
            const snapshot = domSnapshot();
            const hash = stableHash({
                composer_text: snapshot.composer_text,
                send_enabled: snapshot.send_enabled,
                stop_visible: snapshot.stop_visible,
                counts: snapshot.messages.counts,
                last_assistant: snapshot.messages.last_assistant ? snapshot.messages.last_assistant.text : ''
            });
            if (hash !== lastSyncHash) {
                lastSyncHash = hash;
                try {
                    await syncTranscript(reason);
                } catch (error) {
                    console.warn('[MAuto Bridge] sync failed', error);
                }
            }
        }, config.sync_debounce_ms);
    }

    async function handleProbe(command) {
        const snapshot = domSnapshot();
        await report('PROBE_DONE', command.command_id, {
            result: snapshot,
            dom_info: snapshot
        });
    }

    async function handleDumpButtons(command) {
        const snapshot = domSnapshot();
        await report('DUMP_BUTTONS_DONE', command.command_id, {
            result: {
                buttons: snapshot.buttons,
                composer_path: snapshot.composer_path,
                composer_root_path: snapshot.composer_root_path,
                composer_buttons: snapshot.composer_buttons,
                selected_button: snapshot.selected_button,
                selection_strategy: snapshot.selection_strategy,
                selected_button_path: snapshot.selected_button_path,
                matched_selector: snapshot.matched_selector,
                send_enabled: snapshot.send_enabled
            },
            dom_info: snapshot
        });
    }

    async function handleWaitComposerStable(command) {
        const payload = command.payload || {};
        const samples = Number(payload.samples || config.composer_stable_samples);
        const sampleMs = Number(payload.sample_ms || config.composer_stable_sample_ms);
        const series = [];

        for (let i = 0; i < samples; i += 1) {
            if (stopped) {
                return;
            }
            const snapshot = domSnapshot();
            series.push({
                composer: snapshot.composer,
                composer_text_len: snapshot.composer_text_len,
                send_enabled: snapshot.send_enabled
            });
            await sleep(sampleMs);
        }

        const first = JSON.stringify(series[0] || {});
        const stable = series.every((entry) => JSON.stringify(entry) === first) && !!series[0]?.composer;
        const snapshot = domSnapshot();

        await report(stable ? 'COMPOSER_STABLE' : 'COMPOSER_UNSTABLE', command.command_id, {
            result: {
                samples: series,
                stable
            },
            dom_info: snapshot
        });
    }

    async function handleSetPrompt(command) {
        const payload = command.payload || {};
        const text = String(payload.text || '');
        const method = String(payload.method || 'auto');
        const snapshotBefore = domSnapshot();
        const composer = composerElement();
        await sleep(randomBetween(config.action_delay_min_ms, config.action_delay_max_ms));
        const ok = setComposerText(composer, text, method);
        const snapshotAfter = domSnapshot();

        await report(ok ? 'PASTE_CONFIRMED' : 'PASTE_FAILED', command.command_id, {
            text: snapshotAfter.composer_text,
            result: {
                method,
                requested_text_len: text.length,
                before_len: snapshotBefore.composer_text_len,
                after_len: snapshotAfter.composer_text_len,
                replaced_existing_text: snapshotBefore.composer_text_len > 0
            },
            dom_info: snapshotAfter
        });
    }

    async function handleUploadFiles(command) {
        const payload = command.payload || {};
        let files = [];
        let preparedText = '';
        let before = domSnapshot();
        let after = before;
        const tried = [];
        let succeededMethod = '';
        const waitMs = Math.max(0, Number(payload.upload_wait_ms || 15000));
        const pollMs = Math.max(100, Number(payload.upload_poll_ms || 500));
        const method = String(payload.method || 'auto');
        const text = String(payload.text || '');
        const textMethod = String(payload.text_method || 'auto');

        try {
            const overlay_before = dismissUploadOverlays();
            if (overlay_before.matched) {
                tried.push({ method: 'dismiss_overlay_before_upload', ok: true, overlay: overlay_before });
                await sleep(800);
            }
            files = uploadPayloadFiles(payload);
            const composer = composerElement();
            if (!composer) {
                throw new Error('composer_not_found');
            }

            await sleep(randomBetween(config.action_delay_min_ms, config.action_delay_max_ms));
            if (text) {
                setComposerText(composer, text, textMethod);
                preparedText = textOf(composer);
            } else {
                composer.focus();
            }

            before = domSnapshot();
            const beforeAttachments = before.composer_attachments || [];
            const methods = method === 'auto' ? ['input', 'paste', 'drop'] : [method];
            const targets = uniqueElements([
                composer,
                closestComposerRoot(),
                composer.closest ? composer.closest('form') : null,
                document.activeElement,
                document.body,
                document
            ]);

            const checkAfterAttempt = async (label) => {
                await sleep(pollMs);
                dismissUploadOverlays();
                after = domSnapshot();
                if (uploadSucceeded(beforeAttachments, after)) {
                    succeededMethod = label;
                    return true;
                }
                return false;
            };

            for (const currentMethod of methods) {
                if (currentMethod === 'input') {
                    const assigned = tryAssignFilesToInput(files);
                    tried.push({ method: currentMethod, ok: assigned.ok, input: assigned.input });
                    if (await checkAfterAttempt(currentMethod)) {
                        break;
                    }
                } else if (currentMethod === 'paste') {
                    for (const target of targets) {
                        const dt = buildFileDataTransfer(files, text);
                        try {
                            dispatchClipboardLikeEvent(target, 'paste', dt);
                            tried.push({ method: currentMethod, target: domPath(target) || target.nodeName || 'document', ok: true });
                        } catch (error) {
                            tried.push({ method: currentMethod, target: domPath(target) || target.nodeName || 'document', ok: false, error: String(error && error.message || error) });
                        }
                        if (await checkAfterAttempt(`${currentMethod}:${domPath(target) || target.nodeName || 'document'}`)) {
                            break;
                        }
                    }
                    if (succeededMethod) {
                        break;
                    }
                } else if (currentMethod === 'drop') {
                    for (const target of targets) {
                        const dt = buildFileDataTransfer(files, text);
                        let targetAttempted = false;
                        for (const eventName of ['dragenter', 'dragover', 'drop']) {
                            try {
                                dispatchClipboardLikeEvent(target, eventName, dt);
                                targetAttempted = true;
                                tried.push({ method: `${currentMethod}:${eventName}`, target: domPath(target) || target.nodeName || 'document', ok: true });
                            } catch (error) {
                                tried.push({ method: `${currentMethod}:${eventName}`, target: domPath(target) || target.nodeName || 'document', ok: false, error: String(error && error.message || error) });
                            }
                        }
                        if (targetAttempted && await checkAfterAttempt(`${currentMethod}:${domPath(target) || target.nodeName || 'document'}`)) {
                            break;
                        }
                    }
                    if (succeededMethod) {
                        break;
                    }
                } else {
                    tried.push({ method: currentMethod, ok: false, error: 'unsupported_upload_method' });
                    if (await checkAfterAttempt(currentMethod)) {
                        break;
                    }
                }
            }

            const deadline = Date.now() + waitMs;
            while (!succeededMethod && Date.now() < deadline && !stopped) {
                await sleep(pollMs);
                after = domSnapshot();
                if (uploadSucceeded(beforeAttachments, after)) {
                    succeededMethod = 'async';
                    break;
                }
            }

            const ok = !!succeededMethod;
            await report(ok ? 'UPLOAD_FILES_DONE' : 'UPLOAD_FILES_FAILED', command.command_id, {
                text: after.composer_text,
                result: {
                    method,
                    succeeded_method: succeededMethod,
                    file_count: files.length,
                    files: files.map((file) => ({ name: file.name, type: file.type, size: file.size })),
                    text_len: preparedText.length,
                    before_attachment_count: beforeAttachments.length,
                    after_attachment_count: (after.composer_attachments || []).length,
                    attachments: after.composer_attachments || [],
                    file_inputs: after.file_inputs || [],
                    overlay_after: dismissUploadOverlays(),
                    tried
                },
                dom_info: after
            });
        } catch (error) {
            after = domSnapshot();
            await report('UPLOAD_FILES_FAILED', command.command_id, {
                text: after.composer_text,
                result: {
                    reason: String(error && error.message || error),
                    file_count: files.length,
                    overlay_after: dismissUploadOverlays(),
                    tried
                },
                dom_info: after
            });
        }
    }

    async function handleFindSend(command) {
        const buttonRef = findSendButton();
        const button = buttonRef ? buttonRef.element : null;
        const snapshot = domSnapshot();
        await report(button && snapshot.send_enabled ? 'SEND_BUTTON_ENABLED_DONE' : 'FIND_SEND_DONE', command.command_id, {
            result: {
                found: !!button,
                send_enabled: snapshot.send_enabled,
                selected_button: snapshot.selected_button,
                selection_strategy: snapshot.selection_strategy,
                selected_button_path: snapshot.selected_button_path,
                matched_selector: snapshot.matched_selector,
                top_buttons: snapshot.buttons.slice(0, 12)
            },
            dom_info: snapshot
        });
    }

    async function handleClickSend(command) {
        lastAcceptedTurnContext = null;

        const clickWaitStartedAt = Date.now();
        let before = domSnapshot();
        let buttonRef = findSendButton();
        let button = buttonRef ? buttonRef.element : null;

        while (!stopped && !isClickableSendButton(button) && Date.now() - clickWaitStartedAt < config.send_accept_timeout_ms) {
            await sleep(config.send_accept_poll_ms);
            before = domSnapshot();
            buttonRef = findSendButton();
            button = buttonRef ? buttonRef.element : null;
        }

        if (!isClickableSendButton(button)) {
            await report('SEND_FAILED', command.command_id, {
                result: {
                    reason: 'send_button_not_clickable_after_wait',
                    waited_ms: Date.now() - clickWaitStartedAt,
                    selected_button: before.selected_button,
                    selection_strategy: before.selection_strategy,
                    selected_button_path: before.selected_button_path,
                    composer_text_len: before.composer_text_len,
                    composer_buttons: before.composer_buttons,
                    top_buttons: before.buttons.slice(0, 12)
                },
                dom_info: before
            });
            return;
        }

        const turnContext = buildTurnContext(before, command.command_id);
        await sleep(randomBetween(config.send_delay_min_ms, config.send_delay_max_ms));

        let click_method = 'button.click';
        try {
            button.focus();
            button.click();
        } catch (error) {
            const composer = composerElement();
            const form = composer && composer.closest ? composer.closest('form') : null;
            if (form && typeof form.requestSubmit === 'function') {
                form.requestSubmit(button);
                click_method = 'form.requestSubmit_after_click_error';
            } else {
                await report('SEND_FAILED', command.command_id, {
                    result: {
                        reason: 'send_click_threw_without_form_fallback',
                        error: String(error && error.message ? error.message : error),
                        selected_button: buttonMeta(button),
                        selection_strategy: buttonRef ? buttonRef.strategy : null,
                        selected_button_path: domPath(button)
                    },
                    dom_info: before
                });
                return;
            }
        }

        const startedAt = Date.now();
        let submitFallbackUsed = false;
        let after = domSnapshot();
        let reasons = sendAcceptanceReasons(turnContext, after, before.composer_text_len);

        while (!stopped && !isSendAccepted(reasons) && Date.now() - startedAt < config.send_accept_timeout_ms) {
            await sleep(config.send_accept_poll_ms);
            after = domSnapshot();
            reasons = sendAcceptanceReasons(turnContext, after, before.composer_text_len);

            if (!submitFallbackUsed && !isSendAccepted(reasons)) {
                const composer = composerElement();
                const form = composer && composer.closest ? composer.closest('form') : null;
                if (form && typeof form.requestSubmit === 'function' && isClickableSendButton(button)) {
                    try {
                        form.requestSubmit(button);
                        submitFallbackUsed = true;
                        click_method = `${click_method}+form.requestSubmit_retry`;
                    } catch (error) {
                        submitFallbackUsed = true;
                        console.warn('[MAuto Bridge] requestSubmit retry failed', error);
                    }
                }
            }
        }

        const accepted = isSendAccepted(reasons);
        if (accepted) {
            turnContext.accepted_at = Date.now();
            turnContext.acceptance_snapshot = {
                user_count: after.messages.counts.user || 0,
                assistant_count: after.messages.counts.assistant || 0,
                last_user_sig: messageSignature(after.messages.last_user),
                last_assistant_sig: messageSignature(after.messages.last_assistant),
                stop_visible: after.stop_visible
            };
            turnContext.acceptance_reasons = reasons;
            lastAcceptedTurnContext = turnContext;
        }

        await report(accepted ? 'SEND_ACCEPTED' : 'SEND_FAILED', command.command_id, {
            result: {
                reasons,
                click_method,
                waited_for_clickable_ms: clickWaitStartedAt ? Math.max(0, startedAt - clickWaitStartedAt) : 0,
                before_counts: before.messages.counts,
                after_counts: after.messages.counts,
                selected_button: buttonMeta(button),
                selection_strategy: buttonRef ? buttonRef.strategy : null,
                selected_button_path: domPath(button),
                turn_context: {
                    before_user_count: turnContext.before_user_count,
                    before_assistant_count: turnContext.before_assistant_count,
                    accepted_at: turnContext.accepted_at,
                    acceptance_reasons: turnContext.acceptance_reasons
                },
                send_accept_timeout_ms: config.send_accept_timeout_ms
            },
            dom_info: after
        });
    }

    async function handleWaitAssistantDone(command) {
        const turnContext = lastAcceptedTurnContext;
        if (!turnContext) {
            const snapshot = domSnapshot();
            await report('ERROR_COMMAND', command.command_id, {
                result: {
                    reason: 'missing_send_accept_context'
                },
                dom_info: snapshot
            });
            return;
        }

        const currentSnapshot = domSnapshot();
        const initialAssistantText = turnContext.before_last_assistant_text || '';
        const initialAssistantCount = turnContext.before_assistant_count || 0;
        let lastText = currentSnapshot.messages.last_assistant ? currentSnapshot.messages.last_assistant.text : '';
        let lastAssistantCount = currentSnapshot.messages.counts.assistant || 0;
        let lastStopVisible = currentSnapshot.stop_visible;
        let quietSince = Date.now();
        let postStopSince = currentSnapshot.stop_visible ? 0 : Date.now();

        while (!stopped) {
            const snapshot = domSnapshot();
            const assistantText = snapshot.messages.last_assistant ? snapshot.messages.last_assistant.text : '';
            const assistantCount = snapshot.messages.counts.assistant || 0;
            const textChanged = assistantText !== lastText;
            const countChanged = assistantCount !== lastAssistantCount;
            const stopChanged = snapshot.stop_visible !== lastStopVisible;
            const hasNewAssistantTurn = assistantCount > initialAssistantCount;
            const hasFreshAssistantText = assistantText && assistantText !== initialAssistantText;
            const hasFreshAssistantOutput = hasNewAssistantTurn || hasFreshAssistantText;

            if (textChanged || countChanged || stopChanged) {
                lastText = assistantText;
                lastAssistantCount = assistantCount;
                lastStopVisible = snapshot.stop_visible;
                quietSince = Date.now();
                postStopSince = snapshot.stop_visible ? 0 : Date.now();
                await report(textChanged ? 'ASSISTANT_TEXT_CHANGED' : 'ASSISTANT_PROGRESS', command.command_id, {
                    text: assistantText,
                    result: {
                        assistant_len: assistantText.length,
                        assistant_count: assistantCount,
                        stop_visible: snapshot.stop_visible
                    },
                    dom_info: snapshot
                });
            }

            if (!snapshot.stop_visible && !postStopSince) {
                postStopSince = Date.now();
            }

            if (snapshot.stop_visible) {
                await sleep(config.report_wait_every_ms);
                continue;
            }

            if (Date.now() - quietSince >= config.assistant_quiet_ms && hasFreshAssistantOutput) {
                const synced = await syncTranscript('wait_assistant_done');
                const finalSnapshot = synced ? synced.snapshot : snapshot;
                const finalText = synced && synced.transcript.last_assistant ? synced.transcript.last_assistant.text : assistantText;
                const finalAssistantCount = (finalSnapshot.messages.counts.assistant || 0);
                const finalHasFreshText = finalText && finalText !== initialAssistantText;
                const finalHasFreshTurn = finalAssistantCount > initialAssistantCount;
                if (finalHasFreshText || finalHasFreshTurn) {
                    await report('ASSISTANT_DONE', command.command_id, {
                        text: finalText,
                        result: {
                            assistant_len: finalText.length,
                            counts: finalSnapshot.messages.counts,
                            turn_context: {
                                before_user_count: turnContext.before_user_count,
                                before_assistant_count: turnContext.before_assistant_count,
                                accepted_at: turnContext.accepted_at
                            }
                        },
                        dom_info: finalSnapshot
                    });
                    lastAcceptedTurnContext = null;
                    return;
                }
            }

            if (Date.now() - quietSince >= config.assistant_force_sync_quiet_ms && hasFreshAssistantOutput) {
                const synced = await syncTranscript('wait_assistant_done_force_sync');
                const finalSnapshot = synced ? synced.snapshot : snapshot;
                const finalText = synced && synced.transcript.last_assistant ? synced.transcript.last_assistant.text : assistantText;
                const finalAssistantCount = finalSnapshot.messages.counts.assistant || 0;
                const finalHasFreshText = finalText && finalText !== initialAssistantText;
                const finalHasFreshTurn = finalAssistantCount > initialAssistantCount;
                if (finalHasFreshText || finalHasFreshTurn) {
                    await report('ASSISTANT_DONE', command.command_id, {
                        text: finalText,
                        result: {
                            assistant_len: finalText.length,
                            counts: finalSnapshot.messages.counts,
                            force_sync: true,
                            turn_context: {
                                before_user_count: turnContext.before_user_count,
                                before_assistant_count: turnContext.before_assistant_count,
                                accepted_at: turnContext.accepted_at
                            }
                        },
                        dom_info: finalSnapshot
                    });
                    lastAcceptedTurnContext = null;
                    return;
                }
            }

            if (postStopSince && Date.now() - postStopSince >= config.assistant_post_stop_timeout_ms) {
                await report('ASSISTANT_TIMEOUT', command.command_id, {
                    text: assistantText,
                    result: {
                        post_stop_timeout_ms: config.assistant_post_stop_timeout_ms,
                        quiet_ms: Date.now() - quietSince
                    },
                    dom_info: snapshot
                });

                if (config.auto_reload_on_assistant_timeout) {
                    setTimeout(() => {
                        window.location.reload();
                    }, config.reload_after_timeout_ms);
                }
                lastAcceptedTurnContext = null;
                return;
            }

            await sleep(config.report_wait_every_ms);
        }
    }

    async function handleSyncTranscript(command) {
        const payload = command.payload || {};
        const reason = String(payload.reason || 'manual');
        const synced = await syncTranscript(reason);
        const snapshot = synced ? synced.snapshot : domSnapshot();
        await report('TRANSCRIPT_SAVED', command.command_id, {
            text: synced && synced.transcript.last_assistant ? synced.transcript.last_assistant.text : '',
            result: {
                reason,
                counts: snapshot.messages.counts
            },
            dom_info: snapshot
        });
    }

    async function handleReloadPage(command, hard = false) {
        const snapshot = domSnapshot();
        await report('PAGE_RELOADING', command.command_id, {
            result: {
                hard,
                href: window.location.href,
                path: window.location.pathname || ''
            },
            dom_info: snapshot
        });
        setTimeout(() => {
            if (hard) {
                window.location.reload();
                return;
            }
            window.location.reload();
        }, config.reload_after_timeout_ms);
    }

    async function handleNavigateNewChat(command) {
        const snapshot = domSnapshot();
        await report('NEW_CHAT_NAVIGATING', command.command_id, {
            result: {
                href: window.location.href,
                target_path: '/',
                reason: 'navigate_current_tab_to_new_chat'
            },
            dom_info: snapshot
        });
        lastAcceptedTurnContext = null;
        setTimeout(() => {
            window.location.assign('/');
        }, config.reload_after_timeout_ms);
    }

    async function handleCloseWindow(command) {
        const snapshot = domSnapshot();
        await report('WINDOW_CLOSE_REQUESTED', command.command_id, {
            result: {
                href: window.location.href,
                note: 'Browsers may block window.close() unless this tab was script-opened.'
            },
            dom_info: snapshot
        });
        setTimeout(() => {
            window.close();
            setTimeout(async () => {
                if (!document.hidden) {
                    await report('WINDOW_CLOSE_BLOCKED', command.command_id, {
                        result: {
                            href: window.location.href,
                            reason: 'tab_remained_visible_after_close_request'
                        },
                        dom_info: domSnapshot()
                    });
                }
            }, config.reload_after_timeout_ms);
        }, config.reload_after_timeout_ms);
    }

    async function executeCommand(command) {
        const action = String(command.action || 'WAIT');
        activeCommandId = command.command_id || '';

        if (action === 'WAIT') {
            return;
        }

        if (action === 'PROBE') {
            await handleProbe(command);
        } else if (action === 'DUMP_BUTTONS') {
            await handleDumpButtons(command);
        } else if (action === 'WAIT_COMPOSER_STABLE') {
            await handleWaitComposerStable(command);
        } else if (action === 'SET_PROMPT') {
            await handleSetPrompt(command);
        } else if (action === 'UPLOAD_FILE' || action === 'UPLOAD_FILES' || action === 'PASTE_IMAGE' || action === 'PASTE_FILES') {
            await handleUploadFiles(command);
        } else if (action === 'FIND_SEND') {
            await handleFindSend(command);
        } else if (action === 'CLICK_SEND') {
            await handleClickSend(command);
        } else if (action === 'WAIT_ASSISTANT_DONE') {
            await handleWaitAssistantDone(command);
        } else if (action === 'SYNC_TRANSCRIPT') {
            await handleSyncTranscript(command);
        } else if (action === 'NEW_CHAT' || action === 'NAVIGATE_NEW') {
            await handleNavigateNewChat(command);
        } else if (action === 'RESET_PAGE' || action === 'RELOAD_PAGE' || action === 'RELOAD') {
            await handleReloadPage(command, false);
        } else if (action === 'HARD_RELOAD') {
            await handleReloadPage(command, true);
        } else if (action === 'CLOSE_WINDOW' || action === 'CLOSE_TAB') {
            await handleCloseWindow(command);
        } else {
            await report('UNKNOWN_COMMAND', command.command_id, {
                result: {
                    action
                }
            });
        }
    }

    async function pollOnce() {
        if (stopped) {
            return;
        }
        ensureUIAttached();
        const role = nextRole();
        updateUI();
        if (role === 'NONE') {
            schedulePoll();
            return;
        }

        try {
            const response = await request('POST', `${SERVER_URL}/api/status`, {
                role,
                session_id: window.location.pathname || '',
                dom_info: domSnapshot()
            });
            updateConfig(response);
            if (response && response.command) {
                const command = response.command;
                if (command.command_id && command.command_id !== activeCommandId) {
                    await executeCommand(command);
                }
            }
        } catch (error) {
            console.warn('[MAuto Bridge] poll failed', error);
        } finally {
            schedulePoll();
        }
    }

    function schedulePoll() {
        if (stopped) {
            return;
        }
        pollTimer = setTimeout(() => {
            pollTimer = null;
            pollOnce();
        }, config.poll_ms);
    }

    function updateUI() {
        ensureUIAttached();
        if (!uiContainer) {
            return;
        }
        const role = nextRole();
        const hasRole = role !== 'NONE' && role !== 'None' && role !== '';
        const ver = BRIDGE_VERSION.replace('standalone-', '');
        uiContainer.innerHTML = hasRole ? `
            <div style="color:#888;line-height:1.5;">Ver: ${ver}</div>
            <div style="margin-bottom:4px;line-height:1.5;">Role: <span style="color:#10a37f;font-weight:bold;">${role}</span></div>
            <button id="mauto-clear-role-btn" style="width:100%;padding:2px 0;background:#333;color:#ccc;border:1px solid #555;border-radius:3px;cursor:pointer;font-size:10px;">Clear</button>
        ` : `
            <div style="color:#888;line-height:1.5;">Ver: ${ver}</div>
            <div style="margin-bottom:4px;line-height:1.5;">Role: <span style="color:#555;">NONE</span></div>
            <button id="mauto-set-role-btn" style="width:100%;padding:2px 0;background:#10a37f;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">Set Role</button>
        `;

        const setBtn = document.getElementById('mauto-set-role-btn');
        const clearBtn = document.getElementById('mauto-clear-role-btn');

        if (setBtn) {
            setBtn.onclick = () => {
                const current = nextRole();
                const value = prompt('Role:', current === 'NONE' ? '' : current);
                if (value !== null) {
                    const normalized = value.trim().toUpperCase() || 'None';
                    setRole(normalized);
                    updateUI();
                    scheduleSync('role_changed');
                }
            };
        }

        if (clearBtn) {
            clearBtn.onclick = () => {
                setRole('None');
                updateUI();
            };
        }
    }

    function createUI() {
        if (uiContainer && uiContainer.isConnected) {
            return;
        }
        if (uiContainer && !uiContainer.isConnected) {
            uiContainer = null;
        }
        uiContainer = document.createElement('div');
        uiContainer.id = 'mauto-diagnostic-ui';
        uiContainer.style = [
            'position: fixed',
            'top: 80px',
            'right: 20px',
            'z-index: 999999',
            'background: rgba(10,10,10,0.92)',
            'border: 1px solid #444',
            'padding: 5px 7px',
            'border-radius: 5px',
            'color: #fff',
            'font-family: monospace',
            'font-size: 10px',
            'width: 90px',
            'box-shadow: 0 2px 8px rgba(0,0,0,0.5)'
        ].join(';');
        document.body.appendChild(uiContainer);
        updateUI();
    }

    function ensureUIAttached() {
        if (!document.body) {
            return;
        }
        if (!uiContainer || !uiContainer.isConnected) {
            createUI();
        }
    }

    function attachObserver() {
        if (observer) {
            observer.disconnect();
        }
        observer = new MutationObserver(() => {
            ensureUIAttached();
            scheduleSync('mutation');
        });
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true
        });
    }

    function start() {
        stopped = false;
        activeCommandId = '';
        lastAcceptedTurnContext = null;
        createUI();
        attachObserver();
        scheduleSync('start');
        schedulePoll();
        console.log('[MAuto Bridge] start', BRIDGE_VERSION);
    }

    function stop() {
        stopped = true;
        activeCommandId = '';
        lastAcceptedTurnContext = null;
        if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
        if (syncTimer) {
            clearTimeout(syncTimer);
            syncTimer = null;
        }
        if (observer) {
            observer.disconnect();
            observer = null;
        }
        if (uiContainer) {
            uiContainer.remove();
            uiContainer = null;
        }
        console.log('[MAuto Bridge] stop', BRIDGE_VERSION);
    }

    stop();
    start();
})();
