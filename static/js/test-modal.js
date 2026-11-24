/**
 * Test Modal Component - Shared between index.html and history_detail.html
 * Two-column layout: test list on left, test details on right
 */

// Global state for modal
let currentModalTests = [];
let currentSelectedTestIndex = 0;
let currentModalDomain = '';
let currentModalLevel = 0;
let currentModalRunId = '';

/**
 * Show modal with multiple tests (two-column layout)
 * @param {string} domain - Domain name
 * @param {number} level - Test level
 * @param {Array} tests - Array of test results
 * @param {Object} summaryData - Summary data with overall status
 */
function showModalWithTests(domain, level, tests, summaryData) {
    const modal = document.getElementById('test-modal');
    const header = document.getElementById('modal-header');
    const title = document.getElementById('modal-title');
    const body = document.getElementById('modal-body');

    currentModalTests = tests;
    currentSelectedTestIndex = 0;
    currentModalDomain = domain;
    currentModalLevel = level;
    currentModalRunId = (typeof currentRunId !== 'undefined' && currentRunId) || (typeof RUN_ID !== 'undefined' && RUN_ID) || '';

    // DEBUG: Log system prompt fields
    console.log(`[MODAL DEBUG][${domain}][L${level}] Received ${tests.length} tests`);
    tests.forEach((t, i) => {
        if (t.system_prompt || t.test_system_prompt || t.domain_system_prompt ||
            t.details?.system_prompt || t.details?.test_system_prompt || t.details?.domain_system_prompt) {
            console.log(`[MODAL DEBUG][${domain}][L${level}][#${i}] ${t.test_id || t.name}: system_prompt=`, {
                test_sp: !!t.system_prompt,
                test_sp_mode: t.system_prompt_mode,
                test_backend_sp: !!t.test_system_prompt,
                domain_sp: !!t.domain_system_prompt,
                details_sp: !!t.details?.system_prompt,
                details_test_sp: !!t.details?.test_system_prompt,
                details_domain_sp: !!t.details?.domain_system_prompt
            });
        } else {
            console.log(`[MODAL DEBUG][${domain}][L${level}][#${i}] ${t.test_id || t.name}: NO SYSTEM PROMPT FIELDS`);
        }
    });

    // Calculate summary stats
    const passed = tests.filter(t => t.status === 'passed').length;
    const total = tests.length;
    const overallStatus = summaryData?.status || (passed === total ? 'passed' : 'failed');

    const headerClass = overallStatus === 'passed' ? 'modal-header-success' : 'modal-header-error';
    header.className = 'modal-header ' + headerClass;
    title.innerHTML = `${domain.replace(/_/g, ' ').toUpperCase()} Level ${level} — ${passed}/${total} Passed`;

    // Two-column layout with mobile dropdown
    body.innerHTML = `
        <select class="modal-test-select-mobile" id="modal-test-select-mobile" onchange="selectTest(parseInt(this.value))">
            ${tests.map((t, i) => `
                <option value="${i}">${t.status === 'passed' ? '\u2713' : '\u2717'} ${escapeHtml(t.test_id || t.name || 'Test ' + (i+1))}</option>
            `).join('')}
        </select>
        <div class="modal-two-col">
            <div class="modal-test-list">
                <div class="test-list-header">Tests <span class="pass-count">${passed}/${total}</span></div>
                <div class="test-list-items" id="test-list-items">
                    ${tests.map((t, i) => `
                        <div class="test-list-item ${i === 0 ? 'active' : ''} ${t.status}"
                             data-index="${i}" onclick="selectTest(${i})">
                            <span class="test-status-icon">${t.status === 'passed' ? '✓' : '✗'}</span>
                            <span class="test-name">${escapeHtml(t.test_id || t.name || 'Test ' + (i+1))}</span>
                        </div>
                    `).join('')}
                </div>
            </div>
            <div class="modal-test-detail" id="modal-test-detail">
                ${renderTestDetail(tests[0], domain)}
            </div>
        </div>
    `;

    modal.style.display = 'flex';
}

/**
 * Select a test from the list
 * @param {number} index - Test index
 */
function selectTest(index) {
    currentSelectedTestIndex = index;

    // Update active state in list
    document.querySelectorAll('.test-list-item').forEach((el, i) => {
        el.classList.toggle('active', i === index);
    });

    // Sync mobile dropdown
    const mobileSelect = document.getElementById('modal-test-select-mobile');
    if (mobileSelect) mobileSelect.value = index;

    // Update detail view
    const detailDiv = document.getElementById('modal-test-detail');
    const test = currentModalTests[index];
    detailDiv.innerHTML = renderTestDetail(test, test.domain);
}

/**
 * Render test detail panel
 * @param {Object} test - Test result object
 * @param {string} domain - Domain name (for training data)
 * @returns {string} HTML string
 */
function renderTestDetail(test, domain) {
    if (!test) return '<div class="no-test">No test data</div>';

    const details = test.details || {};
    const scoreDisplay = test.points !== null && test.points !== undefined
        ? `${Math.round(test.points * 10) / 10} / ${Math.round(test.max_points * 10) / 10} pts` : 'N/A';
    const scoreClass = test.status === 'passed' ? 'score-passed' : 'score-failed';

    let html = `
        <div class="test-detail-header">
            <h3>${escapeHtml(test.test_id || test.name || 'Test')}</h3>
            <div class="test-meta">
                <span class="score ${scoreClass}">${scoreDisplay}</span>
                <span class="status-badge status-badge-${test.status}">${test.status.toUpperCase()}</span>
                ${test.duration_ms ? `<span class="duration">${test.duration_ms}ms</span>` : ''}
            </div>
        </div>
    `;

    // Tools Available section - compact inline format
    if (details.tools_available && details.tools_available.length > 0) {
        const toolsText = details.tools_available.map(t => {
            const params = t.parameters?.properties
                ? Object.keys(t.parameters.properties).join(', ')
                : '';
            return `<span class="tool-name">${escapeHtml(t.name)}</span>(<span class="tool-params-inline">${params}</span>)`;
        }).join(' • ');

        html += `
            <div class="test-detail-section">
                <div class="section-header">🔧 AVAILABLE TOOLS (${details.tools_available.length})</div>
                <div class="section-content tools-box">${toolsText}</div>
            </div>
        `;
    }

    // System Prompt (collapsible, only if present)
    // The resolved/compiled system prompt is saved by the engine to individual_test_results.system_prompt
    // This is the exact prompt that was sent to the LLM during evaluation
    const systemPrompt = test.system_prompt || details.system_prompt || null;
    const systemPromptMode = test.system_prompt_mode || details.system_prompt_mode || null;
    if (systemPrompt) {
        const modeBadge = systemPromptMode === 'append'
            ? '<span class="mode-badge mode-append">APPEND</span>'
            : '<span class="mode-badge mode-overwrite">OVERWRITE</span>';

        html += `
            <div class="test-detail-section">
                <div class="section-header" style="cursor: pointer; user-select: none;" onclick="toggleSystemPrompt()">
                    🎭 SYSTEM PROMPT ${modeBadge}
                    <span class="toggle-icon" id="system-prompt-toggle">▶</span>
                </div>
                <div class="section-content system-prompt-box" id="system-prompt-content" style="display: none;">
                    <pre style="white-space: pre-wrap; word-wrap: break-word; margin: 0; font-size: 0.85rem; line-height: 1.5;">${escapeHtml(systemPrompt)}</pre>
                </div>
            </div>
        `;
    }

    // Prompt section
    html += `
        <div class="test-detail-section">
            <div class="section-header">📥 PROMPT</div>
            <div class="section-content prompt-box"><pre style="white-space: pre-wrap; word-wrap: break-word; margin: 0; font-size: 0.9rem; line-height: 1.5;">${escapeHtml(test.prompt || '')}</pre></div>
        </div>
    `;

    // Thinking section (collapsible) — right after prompt
    if (details.conversation_log && details.conversation_log.length > 0) {
        html += `
            <div class="test-detail-section">
                <div class="section-header collapsible-header" onclick="toggleThinking()">
                    💭 CONVERSATION LOG (${details.conversation_log.length} turns)
                    <span class="toggle-icon" id="thinking-toggle">▶</span>
                </div>
                <div class="section-content thinking-box" id="thinking-content" style="display: none;">
                    <div class="space-y-2">
                        ${details.conversation_log.map((turn, i) => `
                            <div class="border border-gray-200 dark:border-gray-700 rounded text-xs">
                                <div class="bg-gray-100 dark:bg-gray-700 px-2 py-1 font-semibold text-gray-600 dark:text-gray-300 border-b border-gray-200 dark:border-gray-600">Turn ${turn.turn || i+1}</div>
                                ${turn.thinking ? `
                                    <div class="px-2 py-1 bg-purple-50 dark:bg-purple-900/20 border-b border-gray-100 dark:border-gray-700">
                                        <span class="text-purple-600 dark:text-purple-400 font-medium">💭 [thinking]</span><br />
                                        <pre class="text-gray-700 dark:text-gray-300 ml-1 whitespace-pre-wrap break-words overflow-hidden" style="max-height: 16rem;">${escapeHtml(turn.thinking)}</pre>
                                    </div>
                                ` : ''}
                                ${turn.tool_calls && turn.tool_calls.length > 0 ? `
                                    <div class="px-2 py-1 bg-blue-50 dark:bg-blue-900/20 border-b border-gray-100 dark:border-gray-700 font-mono">
                                        ${turn.tool_calls.map((tc, tcIdx) => {
                                            const argsJson = JSON.stringify(tc.arguments || {}, null, 2);
                                            const argsShort = JSON.stringify(tc.arguments || {});
                                            const truncated = argsShort.length > 60;
                                            const elId = `tc-args-${test.id}-${i}-${tcIdx}`;
                                            return `<div class="flex items-start gap-1 flex-wrap">
                                                <span class="text-blue-600 dark:text-blue-400">🔧</span>
                                                <span class="text-indigo-600 dark:text-indigo-400 font-semibold">${escapeHtml(tc.name)}</span>
                                                <span class="text-gray-500 dark:text-gray-400">(${escapeHtml(argsShort.substring(0, 60))}${truncated ? '…' : ''})</span>
                                                ${truncated ? `<button onclick="toggleToolDetail('${elId}')" class="text-blue-500 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 underline text-xs ml-1" data-expanded="false">▼ args</button>
                                                <div id="${elId}" class="hidden w-full mt-1">
                                                    <pre class="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded p-2 text-xs overflow-auto max-h-48 text-gray-700 dark:text-gray-300 whitespace-pre-wrap">${escapeHtml(argsJson)}</pre>
                                                </div>` : ''}
                                            </div>`;
                                        }).join('')}
                                    </div>
                                ` : ''}
                                ${turn.tool_results && turn.tool_results.length > 0 ? `
                                    <div class="px-2 py-1 bg-green-50 dark:bg-green-900/20 border-b border-gray-100 dark:border-gray-700 font-mono text-gray-600 dark:text-gray-300">
                                        ${turn.tool_results.map((tr, trIdx) => {
                                            const resultJson = JSON.stringify(tr.result || {}, null, 0);
                                            const truncated = resultJson.length > 120;
                                            const elId = `tr-result-${test.id}-${i}-${trIdx}`;
                                            return `<div class="flex items-start gap-1 flex-wrap">
                                                <span class="text-green-600 dark:text-green-400">📥</span>
                                                <span class="text-gray-500 dark:text-gray-400 font-semibold">${escapeHtml(tr.function_name || '')}</span>
                                                <span class="ml-1">${escapeHtml(resultJson.substring(0, 120))}${truncated ? '…' : ''}</span>
                                                ${truncated ? `<button onclick="expandToolResult('${elId}', ${test.id}, ${i}, ${trIdx})" class="text-green-600 dark:text-green-400 hover:text-green-800 dark:hover:text-green-200 underline text-xs ml-1 whitespace-nowrap" data-expanded="false">▼ expand</button>
                                                <div id="${elId}" class="hidden w-full mt-1">
                                                    <div class="text-gray-400 dark:text-gray-500 text-xs italic p-1">Loading…</div>
                                                </div>` : ''}
                                            </div>`;
                                        }).join('')}
                                    </div>
                                ` : ''}
                                ${turn.response ? `
                                    <div class="px-2 py-1 bg-amber-50 dark:bg-amber-900/20 font-semibold text-md">
                                        <p class="text-amber-600 dark:text-amber-400">💬 [response]</p>
                                        <pre class="text-gray-700 dark:text-gray-300 ml-1 whitespace-pre-wrap break-words overflow-hidden">${escapeHtml(turn.response)}</pre>
                                    </div>
                                ` : ''}
                            </div>
                        `).join('')}
                    </div>
                </div>
            </div>
        `;
    } else if (details.thinking) {
        html += `
            <div class="test-detail-section">
                <div class="section-header collapsible-header" onclick="toggleThinking()">
                    🧠 THINKING
                    <span class="toggle-icon" id="thinking-toggle">▶</span>
                </div>
                <div class="section-content thinking-box" id="thinking-content" style="display: none;">
                    <pre class="whitespace-pre-wrap break-words overflow-hidden">${escapeHtml(details.thinking)}</pre>
                </div>
            </div>
        `;
    }

    // Side-by-side: Expected vs Final Response
    // Use details.response (the text sent to evaluator) when available, else test.response
    const finalResponseText = details.response || test.response;
    const hasResponse = !!finalResponseText;
    html += `
        <div class="compare-row">
            <div class="compare-col">
                <div class="section-header">EXPECTED</div>
                <div class="section-content expected-box">${formatExpected(test.expected)}</div>
            </div>
            <div class="compare-col">
                <div class="section-header">RESULT</div>
                <div class="section-content response-box" style="background: #fefce8;">
                    ${hasResponse
                        ? `<pre style="white-space: pre-wrap; word-wrap: break-word; margin: 0; font-size: 0.9rem; line-height: 1.5;">${escapeHtml(stripCodeFences(finalResponseText))}</pre>`
                        : `<span style="color: #9ca3af; font-style: italic;">No response</span>`
                    }
                </div>
            </div>
        </div>
    `;

    // Evaluation Details
    if (details.evaluator || details.called_tools || details.missing_tools) {
        html += `
            <div class="test-detail-section">
                <div class="section-header">🔍 EVALUATION</div>
                <div class="section-content eval-box">
                    ${details.evaluator ? `<div><strong>Evaluator:</strong> ${escapeHtml(details.evaluator)}</div>` : ''}
                    ${details.method ? `<div><strong>Method:</strong> ${escapeHtml(details.method)}</div>` : ''}
                    ${details.called_tools ? `<div><strong>Called:</strong> ${details.called_tools.map(t => `<code>${escapeHtml(t)}</code>`).join(', ')}</div>` : ''}
                    ${details.missing_tools && details.missing_tools.length > 0 ? `<div style="color: #dc2626;"><strong>Missing:</strong> ${details.missing_tools.map(t => `<code>${escapeHtml(t)}</code>`).join(', ')}</div>` : ''}
                    ${renderEvalProcessDetails(details)}
                </div>
            </div>
        `;
    }

    // Action buttons (always visible)
    const replayTestId = escapeHtml(test.test_id || '');
    html += `
        <div class="test-detail-section" style="margin-top: 1.5rem; padding-top: 1rem; border-top: 2px dashed #e5e7eb; display: flex; gap: 0.5rem; flex-wrap: wrap;">
            <button onclick="console.log('[TEST-MODAL] Button clicked, idx=', currentSelectedTestIndex); onGenerateTrainingDataClick(currentSelectedTestIndex)"
                    class="flex-1 px-4 py-2 rounded font-medium text-sm bg-blue-500 hover:bg-blue-600 text-white transition-colors">
                Generate Training Data
            </button>
            <button onclick="replayTest('${replayTestId}', currentModalRunId)"
                    id="btn-replay-test"
                    class="px-4 py-2 rounded font-medium text-sm bg-amber-500 hover:bg-amber-600 text-white transition-colors whitespace-nowrap">
                Play
            </button>
            <button onclick="copyTestResultLink()"
                    id="btn-copy-link"
                    class="px-4 py-2 rounded font-medium text-sm bg-emerald-500 hover:bg-emerald-600 text-white transition-colors whitespace-nowrap">
                Copy Link
            </button>
        </div>
    `;

    return html;
}

/**
 * Show modal with no data (pending state)
 * @param {string} domain - Domain name
 * @param {number} level - Test level
 */
function showModalNoData(domain, level) {
    const modal = document.getElementById('test-modal');
    const header = document.getElementById('modal-header');
    const title = document.getElementById('modal-title');
    const body = document.getElementById('modal-body');

    header.className = 'modal-header';
    title.innerHTML = `${domain.toUpperCase()} Level ${level}`;

    body.innerHTML = `
        <div style="text-align: center; padding: 2rem; color: #666;">
            <div style="font-size: 3rem; margin-bottom: 1rem;">⏳</div>
            <p>No test data available yet.</p>
            <p style="font-size: 0.9rem;">This test has not been executed or is still pending.</p>
        </div>
    `;

    modal.style.display = 'flex';
}

/**
 * Close the test modal
 */
function closeModal() {
    document.getElementById('test-modal').style.display = 'none';
}

/**
 * Escape HTML special characters
 * @param {string} text - Text to escape
 * @returns {string} Escaped text
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Render collapsible evaluator process details
 */
function renderEvalProcessDetails(details) {
    if (!details) return '';
    try {
        var skipKeys = ['evaluator', 'method', 'called_tools', 'missing_tools',
            'tools_available', 'conversation_log', 'thinking', 'response',
            'duration_ms', 'system_prompt', 'system_prompt_mode', 'uses_pass2'];

        var evalFields = {};
        var keys = Object.keys(details);
        for (var i = 0; i < keys.length; i++) {
            var key = keys[i];
            var value = details[key];
            if (skipKeys.indexOf(key) === -1 && value !== null && value !== undefined && value !== '') {
                evalFields[key] = value;
            }
        }

        if (details.pass2 && typeof details.pass2 === 'object') {
            evalFields['pass2'] = details.pass2;
        }

        var fieldKeys = Object.keys(evalFields);
        if (fieldKeys.length === 0) return '';

        var detailId = 'eval-process-' + Math.random().toString(36).substr(2, 9);

        var contentHtml = '';
        for (var j = 0; j < fieldKeys.length; j++) {
            var fkey = fieldKeys[j];
            var fvalue = evalFields[fkey];
            var label = fkey.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
            var valueHtml;

            if (typeof fvalue === 'object') {
                valueHtml = '<pre style="background:var(--bg-secondary,#f1f5f9);padding:0.5rem;border-radius:4px;font-size:0.8rem;white-space:pre-wrap;word-break:break-word;overflow-x:auto;margin:0.25rem 0 0 0;">' + escapeHtml(JSON.stringify(fvalue, null, 2)) + '</pre>';
            } else if (typeof fvalue === 'string' && fvalue.length > 100) {
                valueHtml = '<pre style="background:var(--bg-secondary,#f1f5f9);padding:0.5rem;border-radius:4px;font-size:0.8rem;white-space:pre-wrap;word-break:break-word;overflow-x:auto;margin:0.25rem 0 0 0;">' + escapeHtml(fvalue) + '</pre>';
            } else {
                valueHtml = '<code style="background:var(--bg-secondary,#f1f5f9);padding:0.15rem 0.4rem;border-radius:3px;font-size:0.85rem;">' + escapeHtml(String(fvalue)) + '</code>';
            }

            contentHtml += '<div style="margin-bottom:0.5rem;"><strong style="font-size:0.8rem;color:var(--text-muted,#6b7280);">' + escapeHtml(label) + ':</strong><br>' + valueHtml + '</div>';
        }

        return '<div style="margin-top:0.75rem;border-top:1px solid var(--border-color,#e5e7eb);padding-top:0.5rem;">' +
            '<div style="cursor:pointer;user-select:none;font-size:0.85rem;color:var(--text-muted,#6b7280);font-weight:600;" onclick="var el=document.getElementById(\'' + detailId + '\');var icon=this.querySelector(\'.toggle-icon\');if(el.style.display===\'none\'){el.style.display=\'block\';icon.textContent=\'▼\';}else{el.style.display=\'none\';icon.textContent=\'▶\';}">' +
            '<span class="toggle-icon">▶</span> Evaluator Process Details</div>' +
            '<div id="' + detailId + '" style="display:none;margin-top:0.5rem;padding:0.75rem;background:var(--bg-secondary,#fafafa);border-radius:6px;border:1px solid var(--border-color,#e5e7eb);">' +
            contentHtml + '</div></div>';
    } catch(e) {
        console.error('renderEvalProcessDetails error:', e);
        return '';
    }
}

/**
 * Strip markdown code fences (e.g. ```json ... ```) from LLM responses
 * @param {string} text
 * @returns {string}
 */
function stripCodeFences(text) {
    if (!text) return text;
    return text.replace(/^```[a-zA-Z]*\n?/gm, '').replace(/^```$/gm, '').trim();
}

/**
 * Format expected output for display
 * @param {*} expected - Expected value (can be string, object, etc.)
 * @returns {string} HTML string
 */
function formatExpected(expected) {
    if (!expected) {
        return '<em style="color: #999;">No expected output defined</em>';
    }

    try {
        var parsed = typeof expected === 'string' ? JSON.parse(expected) : expected;

        if (typeof parsed === 'object') {
            // Tool calling format
            if (parsed.tools && Array.isArray(parsed.tools)) {
                return '<strong>Expected Tools:</strong> ' + parsed.tools.map(function(t) { return '<code style="background:#e0e7ff;padding:0.25rem 0.5rem;border-radius:4px;">' + escapeHtml(t) + '</code>'; }).join(' → ');
            }

            if (parsed.tool) {
                var html = '<strong>Expected Tool:</strong> <code style="background:#e0e7ff;padding:0.25rem 0.5rem;border-radius:4px;">' + escapeHtml(parsed.tool) + '</code>';
                if (parsed.result !== undefined) {
                    html += '<br><strong>Expected Result:</strong> ' + escapeHtml(String(parsed.result));
                }
                return html;
            }

            // Show raw JSON for all types
            return '<pre style="background:var(--bg-secondary,#f1f5f9);padding:0.75rem;border-radius:4px;overflow-x:auto;font-size:0.85rem;white-space:pre-wrap;word-break:break-word;margin:0;">' + escapeHtml(JSON.stringify(parsed, null, 2)) + '</pre>';
        }

        return escapeHtml(String(parsed));
    } catch (e) {
        return escapeHtml(String(expected));
    }
}

// Handle escape key and click-outside for test modal
document.addEventListener('DOMContentLoaded', function() {
    document.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
            const trainingModal = document.getElementById('training-modal');
            // Close training modal first if visible, otherwise close test modal
            if (trainingModal && trainingModal.style.display === 'flex') {
                // Training modal escape is handled by training-data.js
                return;
            }
            closeModal();
        }
    });

    window.addEventListener('click', function(event) {
        const testModal = document.getElementById('test-modal');
        if (event.target === testModal) {
            closeModal();
        }
    });
});

/**
 * Toggle thinking/conversation log visibility (collapsed/expanded)
 */
/**
 * Toggle expanded inline view for tool call arguments (already in memory).
 */
function toggleToolDetail(elId) {
    const el = document.getElementById(elId);
    const btn = document.querySelector(`button[onclick="toggleToolDetail('${elId}')"]`);
    if (!el || !btn) return;
    const expanded = btn.dataset.expanded === 'true';
    el.classList.toggle('hidden', expanded);
    btn.dataset.expanded = String(!expanded);
    btn.textContent = expanded ? '▼ args' : '▲ args';
}

/**
 * Expand tool result panel with lazy fetch from server.
 * Fetches /api/v1/result/<resultId> and extracts conversation_log[turnIdx].tool_results[trIdx].
 */
async function expandToolResult(elId, resultId, turnIdx, trIdx) {
    const el = document.getElementById(elId);
    const btn = document.querySelector(`button[onclick="expandToolResult('${elId}', ${resultId}, ${turnIdx}, ${trIdx})"]`);
    if (!el || !btn) return;

    const expanded = btn.dataset.expanded === 'true';

    // Collapse
    if (expanded) {
        el.classList.add('hidden');
        btn.dataset.expanded = 'false';
        btn.textContent = '▼ expand';
        return;
    }

    // Expand — show panel immediately, then fetch
    el.classList.remove('hidden');
    btn.dataset.expanded = 'true';
    btn.textContent = '▲ collapse';

    // Only fetch if not already loaded
    if (el.dataset.loaded === 'true') return;

    el.innerHTML = '<div class="text-gray-400 text-xs italic p-1">Loading…</div>';

    try {
        const resp = await fetch(`/api/v1/result/${resultId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const log = data.details?.conversation_log;
        const result = log?.[turnIdx]?.tool_results?.[trIdx]?.result;
        if (result === undefined) throw new Error('Result not found in response');
        el.innerHTML = `<pre class="bg-green-50 border border-green-200 rounded p-2 text-xs overflow-auto max-h-96 text-gray-700 whitespace-pre-wrap">${escapeHtml(JSON.stringify(result, null, 2))}</pre>`;
        el.dataset.loaded = 'true';
    } catch (err) {
        el.innerHTML = `<div class="text-red-500 text-xs p-1">Error loading result: ${escapeHtml(String(err))}</div>`;
        // Allow retry next time
        btn.dataset.expanded = 'false';
        btn.textContent = '▼ expand';
        el.classList.add('hidden');
    }
}

function toggleThinking() {
    const content = document.getElementById('thinking-content');
    const toggle = document.getElementById('thinking-toggle');
    if (content && toggle) {
        if (content.style.display === 'none') {
            content.style.display = 'block';
            toggle.textContent = '▼';
        } else {
            content.style.display = 'none';
            toggle.textContent = '▶';
        }
    }
}

/**
 * Toggle system prompt visibility (collapsed/expanded)
 */
function toggleSystemPrompt() {
    const content = document.getElementById('system-prompt-content');
    const toggle = document.getElementById('system-prompt-toggle');

    if (content && toggle) {
        if (content.style.display === 'none') {
            content.style.display = 'block';
            toggle.textContent = '▼';
        } else {
            content.style.display = 'none';
            toggle.textContent = '▶';
        }
    }
}

/**
 * Replay a single test and update the modal with the new result
 * @param {string} testId - Test definition ID
 * @param {string|number} runId - Original run ID
 */
async function replayTest(testId, runId) {
    const btn = document.getElementById('btn-replay-test');
    if (!btn) return;

    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '⏳ Running...';
    btn.style.opacity = '0.7';
    btn.style.cursor = 'not-allowed';

    try {
        const resp = await fetch('/api/replay-test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ test_id: testId, run_id: runId })
        });
        const data = await resp.json();

        if (!data.success) {
            if (window.toast) window.toast.error(data.error || 'Replay failed');
            else alert(data.error || 'Replay failed');
            return;
        }

        // Update the test in the modal's in-memory list
        const newResult = data.result;
        newResult.domain = newResult.domain || currentModalDomain;
        currentModalTests[currentSelectedTestIndex] = newResult;

        // Update the test list item status icon and class
        const listItems = document.querySelectorAll('.test-list-item');
        if (listItems[currentSelectedTestIndex]) {
            const item = listItems[currentSelectedTestIndex];
            const icon = item.querySelector('.test-status-icon');
            item.classList.remove('passed', 'failed');
            item.classList.add(newResult.status);
            if (icon) icon.textContent = newResult.status === 'passed' ? '✓' : '✗';
        }

        // Re-render the detail panel
        const detailDiv = document.getElementById('modal-test-detail');
        if (detailDiv) detailDiv.innerHTML = renderTestDetail(newResult, newResult.domain);

        // Update header pass count
        const passed = currentModalTests.filter(t => t.status === 'passed').length;
        const total = currentModalTests.length;
        const titleEl = document.getElementById('modal-title');
        if (titleEl) {
            titleEl.innerHTML = titleEl.innerHTML.replace(/\d+\/\d+ Passed/, `${passed}/${total} Passed`);
        }

        const statusLabel = newResult.status === 'passed' ? 'PASSED ✓' : 'FAILED ✗';
        if (window.toast) window.toast.success(`Replay done — ${statusLabel}`);

    } catch (err) {
        if (window.toast) window.toast.error('Replay error: ' + err.message);
        else alert('Replay error: ' + err.message);
        // Restore button on error
        btn.disabled = false;
        btn.innerHTML = originalHtml;
        btn.style.opacity = '';
        btn.style.cursor = '';
    }
}

/**
 * Copy the API link for the current test result detail to clipboard
 */
function copyTestResultLink() {
    var url = window.location.origin + '/api/v1/history/' + currentModalRunId + '/' + currentModalDomain + '/' + currentModalLevel;
    var btn = document.getElementById('btn-copy-link');
    navigator.clipboard.writeText(url).then(function() {
        var original = btn.innerHTML;
        btn.innerHTML = '✅ Copied!';
        setTimeout(function() { btn.innerHTML = original; }, 2000);
    }).catch(function() {
        var textarea = document.createElement('textarea');
        textarea.value = url;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        var original = btn.innerHTML;
        btn.innerHTML = '✅ Copied!';
        setTimeout(function() { btn.innerHTML = original; }, 2000);
    });
}
