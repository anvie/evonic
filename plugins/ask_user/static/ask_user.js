/*
 * Ask User Question plugin — web UI for the agent chat page.
 *
 * Injected on /agents/<id> by plugins/ask_user/routes.py (inject_assets), so it
 * needs no Evonic core changes. It polls /api/ask_user/pending for the page's
 * chat session, docks a pending question above the chat input (locking the
 * composer), and posts answers or cancels back. It only relies on the chat
 * page's element ids: #chat-input, #chat-send-btn, #chat-attach-btn,
 * #chat-file-preview, #chat-messages.
 */
(function () {
    'use strict';

    const $ = window.jQuery;
    const composer = document.getElementById('chat-input');
    if (!$ || !composer || window.__askUserLoaded) return;
    window.__askUserLoaded = true;

    const agentId = decodeURIComponent(location.pathname.split('/')[2] || '');
    const POLL_ACTIVE_MS = 1000;   // agent busy or a card is open
    const POLL_IDLE_MS = 4000;
    const SESSION_REFRESH_MS = 15000;
    const LOCKED_PLACEHOLDER = 'Answer the question above, or cancel it';

    const cards = new Map();  // question_id -> $card (docked, unresolved)
    let sessionId = null;
    let sessionFetchedAt = 0;

    // ── Dock + composer lock ────────────────────────────────────────────────

    const dock = document.createElement('div');
    dock.id = 'ask-user-dock';
    dock.className = 'ask-dock';
    dock.hidden = true;
    dock.setAttribute('aria-live', 'polite');
    const preview = document.getElementById('chat-file-preview');
    const anchor = preview || composer.closest('.flex') || composer;
    anchor.parentNode.insertBefore(dock, anchor);

    // While a question is docked the agent is blocked on it, so the composer is
    // locked: answering or cancelling the card is the only way forward.
    function lockComposer(locked) {
        if (locked && !composer.dataset.askLocked) {
            composer.dataset.askLocked = '1';
            composer.dataset.askPlaceholder = composer.placeholder;
            composer.placeholder = LOCKED_PLACEHOLDER;
        } else if (!locked && composer.dataset.askLocked) {
            composer.placeholder = composer.dataset.askPlaceholder || 'Type a message...';
            delete composer.dataset.askLocked;
            delete composer.dataset.askPlaceholder;
        }
        composer.disabled = locked;
        ['chat-send-btn', 'chat-attach-btn'].forEach(id => {
            const btn = document.getElementById(id);
            if (btn) btn.disabled = locked;
        });
    }

    // The thinking bubble says "Waiting for your answer" while a card is open.
    function setWaitingLabel(waiting) {
        const labels = document.querySelectorAll('#chat-messages .thinking-bubble > .font-medium');
        const label = labels[labels.length - 1];
        if (!label) return;
        if (waiting) label.textContent = 'Waiting for your answer';
        else if (label.textContent === 'Waiting for your answer') label.textContent = 'Thinking';
    }

    function syncDock() {
        const open = dock.children.length > 0;
        dock.hidden = !open;
        lockComposer(open);
        setWaitingLabel(open);
    }

    // ── Card ────────────────────────────────────────────────────────────────

    // Keycap showing that Enter triggers the button (lucide "corner-down-left").
    function enterKeyHint() {
        return $('<kbd class="ask-kbd" aria-hidden="true">').html(
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round">' +
            '<polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>');
    }

    function readAnswer($fs) {
        const selected = $fs.find('input:checked').not('.ask-other-toggle')
            .map(function () { return this.value; }).get();
        const otherOn = $fs.find('.ask-other-toggle').prop('checked');
        const other = otherOn ? $fs.find('.ask-other-text').val().trim() : '';
        const complete = (selected.length > 0 || otherOn) && !(otherOn && !other);
        return { selected, other: other || null, complete };
    }

    // Returns the answers array for the API, or null while any question is unanswered.
    function collectAnswers($card) {
        const answers = [];
        let complete = true;
        $card.find('fieldset.ask-question').each((i, fs) => {
            const { selected, other, complete: done } = readAnswer($(fs));
            if (!done) complete = false;
            answers.push({ selected, other });
        });
        return complete ? answers : null;
    }

    function buildCard(pending) {
        const questionId = pending.question_id;
        const questions = pending.questions;
        const $card = $('<form class="ask-card" novalidate>')
            .attr('data-question-id', questionId)
            .data('questions', questions);
        $card.append($('<div class="ask-card-title">').append(
            $('<span class="ask-card-icon" aria-hidden="true">').text('?'),
            $('<span>').text('The agent needs your input')
        ));

        questions.forEach((q, qi) => {
            const multi = !!q.multiSelect;
            const inputType = multi ? 'checkbox' : 'radio';
            const name = `ask-${questionId}-${qi}`;
            const $fs = $('<fieldset class="ask-question">');
            $fs.append($('<legend class="ask-question-text">').append(
                $('<span class="ask-chip">').text(q.header || `Q${qi + 1}`),
                $('<span>').text(q.question || '')
            ));
            $fs.append($('<div class="ask-hint">').text(multi ? 'Choose one or more' : 'Choose one'));

            (q.options || []).forEach(opt => {
                const $label = $('<span class="ask-option-label">').text(opt.label);
                if (opt.recommended) $label.append($('<span class="ask-badge">').text('Recommended'));
                const $text = $('<span class="ask-option-text">').append($label);
                if (opt.description) $text.append($('<span class="ask-option-desc">').text(opt.description));
                $fs.append($('<label class="ask-option">').append(
                    $('<input>').attr({ type: inputType, name, value: opt.label }), $text));
            });

            const $otherToggle = $('<input class="ask-other-toggle">').attr({ type: inputType, name, value: '' });
            const $otherText = $('<input type="text" class="ask-other-text" maxlength="2000" placeholder="Other: type your own answer">')
                .attr('aria-label', `Other answer for ${q.header || 'question ' + (qi + 1)}`);
            $otherText.on('focus input', () => { if ($otherText.val().trim()) $otherToggle.prop('checked', true); });
            $otherToggle.on('change', () => { if ($otherToggle.prop('checked')) $otherText.trigger('focus'); });
            $fs.append($('<label class="ask-option ask-option-other">').append($otherToggle, $otherText));
            $card.append($fs);
        });

        const $submit = $('<button type="submit" class="ask-submit" disabled aria-keyshortcuts="Enter">')
            .append($('<span>').text('Submit answer'), enterKeyHint());
        const $cancel = $('<button type="button" class="ask-nav ask-cancel" aria-keyshortcuts="Escape">')
            .text('Cancel').on('click', () => cancelQuestion(questionId, $card));
        const $footer = $('<div class="ask-footer">').append($('<span class="ask-status" role="status">'), $cancel, $submit);
        $card.append($footer);

        const wizard = questions.length > 1 ? setupWizard($card, $footer) : null;

        $card.on('input change', () => {
            $submit.prop('disabled', !collectAnswers($card));
            if (wizard) wizard.refresh();
        });
        $card.on('submit', e => {
            e.preventDefault();
            submitAnswer(questionId, $card);
        });

        // Enter advances / submits, Escape cancels — while focus is on the card (or
        // nowhere), never while the user is typing elsewhere.
        const onKey = e => {
            if (e.isComposing || e.defaultPrevented) return;
            const inCard = $card[0].contains(e.target);
            if (!inCard && e.target !== document.body) return;
            if (e.key === 'Escape') {
                e.preventDefault();
                cancelQuestion(questionId, $card);
                return;
            }
            if (e.key !== 'Enter' || e.shiftKey) return;
            if (e.target.tagName === 'BUTTON') return;  // let buttons activate natively
            e.preventDefault();
            if (wizard && !wizard.isLast()) wizard.next();
            else if (!$submit.prop('disabled')) $card.trigger('submit');
        };
        document.addEventListener('keydown', onKey);
        $card.data('keyHandler', onKey);
        return $card;
    }

    // Multi-question cards show one question per step: header chips to jump
    // around, Back/Next, and Submit on the last step once everything is answered.
    function setupWizard($card, $footer) {
        const $fieldsets = $card.find('fieldset.ask-question');
        const total = $fieldsets.length;
        let step = 0;

        const $counter = $('<span class="ask-step-count">');
        $card.find('.ask-card-title').append($counter);
        const $steps = $('<div class="ask-steps" role="tablist">');
        $fieldsets.each((i, fs) => {
            const header = $(fs).find('.ask-chip').text();
            $steps.append($('<button type="button" class="ask-step" role="tab">')
                .text(header).on('click', () => go(i)));
        });
        $card.find('.ask-card-title').after($steps);
        $card.addClass('ask-wizard');

        const $back = $('<button type="button" class="ask-nav ask-back">').text('Back').on('click', () => go(step - 1));
        const $next = $('<button type="button" class="ask-nav ask-next" aria-keyshortcuts="Enter">')
            .append($('<span>').text('Next'), enterKeyHint()).on('click', () => go(step + 1));
        $footer.prepend($back, $next);

        const answered = i => readAnswer($fieldsets.eq(i)).complete;
        const refresh = () => {
            const last = step === total - 1;
            $counter.text(`${step + 1} of ${total}`);
            $steps.children().each((i, btn) => $(btn)
                .toggleClass('is-current', i === step)
                .toggleClass('is-done', answered(i))
                .attr('aria-selected', i === step ? 'true' : 'false'));
            $fieldsets.each((i, fs) => { fs.hidden = i !== step; });
            $back.prop('hidden', step === 0);
            $next.prop('hidden', last).prop('disabled', !answered(step));
            $footer.find('.ask-submit').prop('hidden', !last);
        };
        const go = i => {
            if (i < 0 || i >= total || i === step) return;
            step = i;
            refresh();
            const $fs = $fieldsets.eq(step);
            const $checked = $fs.find('input:checked');
            ($checked.length ? $checked : $fs.find('input')).first().trigger('focus');
        };

        // Clicking a single-select option (not "Other") answers the step; move on.
        // Pointer only: arrow keys also fire change, and must just browse options.
        let pointerAt = 0;
        $card.on('pointerdown', '.ask-option', () => { pointerAt = Date.now(); });
        $fieldsets.each((i, fs) => {
            $(fs).on('change', 'input[type="radio"]:not(.ask-other-toggle)', () => {
                if (Date.now() - pointerAt > 1000) return;
                if (i === step && step < total - 1) setTimeout(() => go(step + 1), 180);
            });
        });

        refresh();
        return { refresh, next: () => answered(step) && go(step + 1), isLast: () => step === total - 1 };
    }

    function addCard(pending) {
        if (!pending.question_id || !Array.isArray(pending.questions) || !pending.questions.length) return;
        if (cards.has(pending.question_id)) return;
        const $card = buildCard(pending);
        cards.set(pending.question_id, $card);
        dock.appendChild($card[0]);
        syncDock();
    }

    // ── Resolution ──────────────────────────────────────────────────────────

    // Once answered, closed, stopped or expired the card simply leaves the dock;
    // the answer stays visible in the thinking timeline as the tool's result.
    function resolveCard(questionId) {
        const $card = cards.get(questionId);
        if (!$card) return;
        cards.delete(questionId);
        const handler = $card.data('keyHandler');
        if (handler) document.removeEventListener('keydown', handler);
        $card.remove();
        syncDock();
    }

    function expireAll() {
        [...cards.keys()].forEach(resolveCard);
    }

    async function post(url, body) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        return { res, body: await res.json().catch(() => ({})) };
    }

    async function submitAnswer(questionId, $card) {
        const answers = collectAnswers($card);
        if (!answers || $card.attr('data-resolved') || $card.data('busy')) return;
        $card.data('busy', true);
        const $status = $card.find('.ask-status').removeClass('ask-status-error');
        const $controls = $card.find('input, button').prop('disabled', true);
        $status.text('Sending…');
        try {
            const { res, body } = await post('/api/ask_user/answer', { question_id: questionId, answers });
            if (res.ok || res.status === 404) return resolveCard(questionId);  // 404: no longer waiting
            $status.text(body.error || 'Could not send the answer.').addClass('ask-status-error');
        } catch (e) {
            $status.text('Network error. Try again.').addClass('ask-status-error');
        }
        $controls.prop('disabled', false);
        $card.trigger('change');  // restore wizard / submit enabled states
        $card.removeData('busy');
    }

    async function cancelQuestion(questionId, $card) {
        if ($card.attr('data-resolved') || $card.data('busy')) return;
        $card.data('busy', true);
        const $status = $card.find('.ask-status').removeClass('ask-status-error');
        const $controls = $card.find('input, button').prop('disabled', true);
        $status.text('Closing…');
        try {
            const { res, body } = await post('/api/ask_user/cancel', { question_id: questionId });
            if (res.ok || res.status === 404) return resolveCard(questionId);  // 404: no longer waiting
            $status.text(body.error || 'Could not close the question.').addClass('ask-status-error');
        } catch (e) {
            $status.text('Network error. Try again.').addClass('ask-status-error');
        }
        $controls.prop('disabled', false);
        $card.trigger('change');
        $card.removeData('busy');
    }

    // ── Polling ─────────────────────────────────────────────────────────────

    async function refreshSession() {
        const res = await fetch(`/api/agents/${encodeURIComponent(agentId)}/chat/session?user_id=web_test`);
        if (!res.ok) return;
        const data = await res.json().catch(() => ({}));
        const next = data.session_id || null;
        if (sessionId && next !== sessionId) expireAll();  // chat was cleared / switched
        sessionId = next;
        sessionFetchedAt = Date.now();
    }

    function agentLooksBusy() {
        return cards.size > 0 || !!document.querySelector('#chat-messages .thinking-spinner');
    }

    async function poll() {
        try {
            if (!sessionId || Date.now() - sessionFetchedAt > SESSION_REFRESH_MS) await refreshSession();
            if (sessionId && !document.hidden) {
                const known = [...cards.keys()].join(',');
                const res = await fetch(`/api/ask_user/pending?session_id=${encodeURIComponent(sessionId)}` +
                    (known ? `&known=${encodeURIComponent(known)}` : ''));
                if (res.ok) {
                    const data = await res.json();
                    Object.keys(data.resolved || {}).forEach(resolveCard);
                    (data.questions || []).forEach(addCard);
                    // The chat UI may rebuild its thinking bubble after a reload; re-label it.
                    if (cards.size) setWaitingLabel(true);
                }
            }
        } catch (e) { /* transient: retry on the next tick */ }
        setTimeout(poll, agentLooksBusy() ? POLL_ACTIVE_MS : POLL_IDLE_MS);
    }

    poll();
})();
