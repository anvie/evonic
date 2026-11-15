/**
 * Training Data Generator - Shared Component
 * Generates Gemma 4 format training data from test results
 */

// Store current test for training generation
let _currentTrainingTest = null;

/**
 * Show the training data modal with generated JSONL
 * @param {Object} test - Test object with prompt, details, etc.
 */
function showTrainingDataModal(test) {
    if (!test) return;
    
    _currentTrainingTest = test;
    
    const trainingData = generateTrainingData(test);
    const prettyJson = JSON.stringify(trainingData, null, 2);
    
    const modal = document.getElementById('training-modal');
    const textarea = document.getElementById('training-textarea');
    
    if (modal && textarea) {
        textarea.value = prettyJson;
        modal.style.display = 'flex';
    }
}

/**
 * Handle training data button click
 * @param {number} testIndex - Index of selected test
 */
function onGenerateTrainingDataClick(testIndex) {
    console.log('[TRAINING] Button clicked, testIndex=', testIndex);
    console.log('[TRAINING] currentModalTests=', currentModalTests);
    // currentModalTests is in global scope from test-modal.js
    if (typeof currentModalTests !== 'undefined' && currentModalTests[testIndex]) {
        console.log('[TRAINING] Showing modal for test:', currentModalTests[testIndex]);
        showTrainingDataModal(currentModalTests[testIndex]);
    } else {
        console.error('[TRAINING ERROR] no test found at index', testIndex, 'currentModalTests=', currentModalTests);
    }
}

/**
 * Generate Gemma 4 format training data from test result
 * @param {Object} test - Test object
 * @returns {Object} Training data in Gemma 4 JSONL format
 */
function generateTrainingData(test) {
    const details = test.details || {};
    const conversationLog = details.conversation_log || [];
    const toolsAvailable = details.tools_available || [];
    
    // Use test's system_prompt if available, otherwise empty
    let systemContent = test.system_prompt || '';
    
    // If system_prompt exists, wrap in Gemma 4 think tag
    if (systemContent && systemContent.trim()) {
        systemContent = '<|think|>' + systemContent.trim() + '\n\n';
    } else {
        systemContent = '<|think|>You are a helpful assistant.\n\n';
    }
    
    // Add tool definitions in Gemma 4 format
    toolsAvailable.forEach(tool => {
        const toolDef = {
            name: tool.name,
            description: tool.description,
            parameters: tool.parameters || { type: 'object', properties: {}, required: [] }
        };
        systemContent += '<|tool>\n' + JSON.stringify(toolDef) + '\n<tool|>\n';
    });
    
    const messages = [
        { role: 'system', content: systemContent.trim() },
        { role: 'user', content: test.prompt || '' }
    ];
    
    // Handle single-turn tests (no conversation_log)
    if (conversationLog.length === 0) {
        let assistantContent = '';
        if (details.thinking) {
            assistantContent += '<|channel>thought\n' + details.thinking.trim() + '\n<channel|>';
        }
        assistantContent += test.response || '';
        if (assistantContent) {
            messages.push({ role: 'assistant', content: assistantContent });
        }
    }

    // Process each turn in conversation_log
    conversationLog.forEach((turn, idx) => {
        let assistantContent = '';
        
        // Add thinking
        if (turn.thinking) {
            assistantContent += '<|channel>thought\n' + turn.thinking.trim() + '\n<channel|>';
        }
        
        // Add tool calls
        if (turn.tool_calls && turn.tool_calls.length > 0) {
            turn.tool_calls.forEach(tc => {
                const argsStr = _formatToolArgs(tc.arguments || {});
                assistantContent += '<|tool_call>call:' + tc.name + '{' + argsStr + '}<tool_call|>';
            });
        }
        
        // Add tool responses
        if (turn.tool_results && turn.tool_results.length > 0) {
            turn.tool_results.forEach(tr => {
                const resultStr = _formatToolArgs(tr.result || {});
                assistantContent += '<|tool_response>response:' + tr.function_name + '{' + resultStr + '}<tool_response|>';
            });
        }
        
        // Add final response
        if (turn.response) {
            if (!assistantContent.includes('<|channel>thought')) {
                assistantContent += '<|channel>thought\n<channel|>';
            }
            assistantContent += turn.response;
        }
        
        if (assistantContent) {
            messages.push({ role: 'assistant', content: assistantContent });
        }
    });
    
    return {
        messages: messages,
        category: _mapDomainToCategory(test.domain)
    };
}

/**
 * Format tool arguments in Gemma 4 style
 * @param {Object} obj - Arguments object
 * @returns {string} Formatted string
 */
function _formatToolArgs(obj) {
    if (!obj || Object.keys(obj).length === 0) return '';
    
    const parts = [];
    for (const [key, value] of Object.entries(obj)) {
        if (typeof value === 'string') {
            parts.push(key + ':<|"|>' + value + '<|"|>');
        } else if (typeof value === 'number') {
            parts.push(key + ':' + value);
        } else if (Array.isArray(value)) {
            parts.push(key + ':[' + value.map(v => typeof v === 'string' ? '"' + v + '"' : v).join(',') + ']');
        } else {
            parts.push(key + ':' + JSON.stringify(value));
        }
    }
    return parts.join(',');
}

/**
 * Map domain ID to training category
 * @param {string} domain - Domain ID
 * @returns {string} Category name
 */
function _mapDomainToCategory(domain) {
    const mapping = {
        'villa_demo': 'availability',
        'booking': 'booking',
        'pricing': 'price',
        'tool_calling': 'tool_calling'
    };
    return mapping[domain] || 'general';
}

/**
 * Copy training data as compact JSONL to clipboard
 */
function copyTrainingDataAsJsonl() {
    if (!_currentTrainingTest) return;
    
    const trainingData = generateTrainingData(_currentTrainingTest);
    const compactJson = JSON.stringify(trainingData);
    
    navigator.clipboard.writeText(compactJson).then(() => {
        const btn = document.getElementById('copy-training-btn');
        if (btn) {
            const originalText = btn.innerHTML;
            btn.innerHTML = '✓ Copied!';
            btn.style.background = '#10b981';
            setTimeout(() => {
                btn.innerHTML = originalText;
                btn.style.background = '#667eea';
            }, 2000);
        }
    }).catch(err => {
        console.error('Failed to copy:', err);
        alert('Failed to copy to clipboard');
    });
}

/**
 * Close the training data modal
 */
function closeTrainingModal() {
    const modal = document.getElementById('training-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

/**
 * Render the "Generate Training Data" button HTML
 * @param {Object} test - Test object to check for conversation_log
 * @param {number|string} testIndex - Index or identifier for onclick
 * @returns {string} HTML string or empty if no conversation_log
 */
function renderTrainingDataButton(test, testIndex) {
    const details = test.details || {};
    if (!details.conversation_log || details.conversation_log.length === 0) {
        return '';
    }
    
    return `
        <div class="training-data-section" style="margin-top: 1.5rem; padding-top: 1rem; border-top: 2px dashed #e5e7eb;">
            <button onclick="onGenerateTrainingDataClick(${testIndex})" 
                    class="training-data-btn"
                    style="width: 100%; padding: 0.6rem 1rem; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                           color: white; border: none; border-radius: 6px; font-size: 0.9rem; font-weight: 600; 
                           cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 0.5rem;
                           transition: transform 0.2s, box-shadow 0.2s;"
                    onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 4px 12px rgba(102, 126, 234, 0.4)';"
                    onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='none';">
                📋 Generate Training Data
            </button>
        </div>
    `;
}

// Handle click-outside and escape key for training modal
document.addEventListener('DOMContentLoaded', function() {
    document.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
            const trainingModal = document.getElementById('training-modal');
            if (trainingModal && trainingModal.style.display === 'flex') {
                closeTrainingModal();
                event.stopPropagation();
            }
        }
    });
    
    window.addEventListener('click', function(event) {
        const trainingModal = document.getElementById('training-modal');
        if (event.target === trainingModal) {
            closeTrainingModal();
        }
    });
});
