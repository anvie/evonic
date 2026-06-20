// Dashboard lazy loading — fetches data from /api/dashboard/data and populates UI

$(function() {
    loadDashboard();
});

function loadDashboard() {
    $.getJSON('/api/dashboard/data', function(data) {
        renderStats(data.stats);
        renderFeatureCards(data.skill_stats, data.plugin_stats, data.schedule_stats, data.stats, data.plugin_cards);
        renderAgents(data.recent_agents);
        renderLeaderboard(data.leaderboard);
        renderRecentRuns(data.recent_runs);
        renderModelUsage(data.model_usage);
        renderPluginCards(data.plugin_cards);
        // Remove skeleton shimmer animation after data is loaded
        $('.skeleton-value').removeClass('skeleton-value');
    }).fail(function() {
        // Hide all empty/loading placeholders and show error
        $('#agent-empty, #leaderboard-empty, #recent-runs-empty, #model-usage-empty').html('<p class="text-red-500 dark:text-red-400">Failed to load data. Please try refreshing the page.</p>');
    });
}

function emptyState(message) {
    return '<div class="p-6 text-center text-sm text-gray-400 dark:text-gray-500">' + message + '</div>';
}

function renderStats(stats) {
    $('#stat-agent-count').text(stats.agent_count);
    $('#stat-active-channel-count').text(stats.active_channel_count);
    $('#stat-channel-count').text(stats.channel_count);
    $('#stat-session-count').text(stats.session_count);
    $('#stat-eval-run-count').text(stats.eval_run_count);
    if (stats.latest_eval_score !== null && stats.latest_eval_score !== undefined) {
        $('#stat-latest-eval-score').text('Latest: ' + stats.latest_eval_score + '%');
    } else {
        $('#stat-latest-eval-score').text('No evaluations yet');
    }
}

function renderFeatureCards(skillStats, pluginStats, scheduleStats, stats, pluginCards) {
    // Skills
    $('#stat-skill-enabled').text(skillStats.enabled);
    $('#stat-skill-total').text(skillStats.total);
    var skillsetWord = skillStats.skillset_count === 1 ? 'skillset' : 'skillsets';
    $('#stat-skill-skillsets').text(skillStats.skillset_count + ' ' + skillsetWord);

    // Plugins
    $('#stat-plugin-enabled').text(pluginStats.enabled);
    $('#stat-plugin-total').text(pluginStats.total);

    // Tools
    $('#stat-tool-count').text(stats.tool_count);

    // Scheduler
    $('#stat-schedule-active').text(scheduleStats.active);
    $('#stat-schedule-total').text(scheduleStats.total);

    // Plugin feature cards (Row 2 stat cards from plugins)
    renderPluginFeatureCards(pluginCards);
}

function renderPluginFeatureCards(pluginCards) {
    var $container = $('#plugin-feature-cards');
    $container.empty();
    if (!pluginCards || pluginCards.length === 0) {
        return;
    }
    for (var i = 0; i < pluginCards.length; i++) {
        var card = pluginCards[i];
        if (!card.feature_card) {
            continue;
        }
        var fc = card.feature_card;
        var html = '<a href="' + (card.link || '#') + '" class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-5 hover:shadow-md hover:border-' + (fc.border_color || 'rose') + '-300 transition-all no-underline">';
        html += '  <div class="flex items-center gap-3 mb-2">';
        html += '    <div class="w-10 h-10 rounded-full bg-' + (fc.bg_color || 'rose') + '-100 dark:bg-' + (fc.bg_color || 'rose') + '-900/40 flex items-center justify-center flex-shrink-0">';
        html += '      <svg class="w-5 h-5 text-' + (fc.icon_color || 'rose') + '-600 dark:text-' + (fc.icon_color || 'rose') + '-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2"/></svg>';
        html += '    </div>';
        html += '    <div>';
        html += '      <div class="text-2xl font-bold text-gray-800 dark:text-gray-100">' + (fc.count || 0) + '</div>';
        html += '      <div class="text-xs text-gray-500 dark:text-gray-400">' + (fc.detail || '') + '</div>';
        html += '    </div>';
        html += '  </div>';
        html += '</a>';
        $container.append(html);
    }
}

function renderAgents(agents) {
    var $container = $('#agent-list');
    var $empty = $('#agent-empty');

    if (!agents || agents.length === 0) {
        $container.hide();
        $empty.html(emptyState('No agents yet')).show();
        return;
    }

    $empty.hide();
    $container.show();

    var html = '';
    for (var i = 0; i < agents.length; i++) {
        var a = agents[i];
        var name = a.name || a.id;
        var initial = name.charAt(0).toUpperCase();
        var desc = a.description || 'No description';
        if (desc.length > 50) {
            desc = desc.substring(0, 50) + '...';
        }
        var modelBadge = a.model_id ? '<span class="text-xs bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 px-2 py-0.5 rounded font-mono hidden sm:inline">' + a.model_id.substring(0, 20) + '</span>' : '';

        html += '<a href="/agents/' + a.id + '" class="flex items-center justify-between p-4 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors no-underline text-inherit">\n';
        html += '    <div class="flex items-center gap-3 min-w-0">\n';
        var avatarHtml = a.avatar_path
            ? '<img src="/api/agents/' + a.id + '/avatar?size=small" class="w-8 h-8 rounded-full object-cover flex-shrink-0" alt="">'
            : '<div class="w-8 h-8 rounded-full bg-indigo-100 dark:bg-indigo-900/40 flex items-center justify-center flex-shrink-0 text-xs font-bold text-indigo-600 dark:text-indigo-400">' + initial + '</div>';
        html += '        ' + avatarHtml + '\n';
        html += '        <div class="min-w-0">\n';
        html += '            <div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">' + name + '</div>\n';
        html += '            <div class="text-xs text-gray-400 dark:text-gray-500">' + desc + '</div>\n';
        html += '        </div>\n';
        html += '    </div>\n';
        html += '    <div class="flex items-center gap-3 flex-shrink-0 ml-3">\n';
        html += modelBadge;
        html += '        <div class="flex gap-2 text-xs text-gray-400 dark:text-gray-500">\n';
        html += '            <span title="Tools">' + a.tool_count + ' tools</span>\n';
        html += '            <span title="Channels">' + a.channel_count + ' ch</span>\n';
        html += '        </div>\n';
        html += '    </div>\n';
        html += '</a>';
    }

    $container.html(html);
}

function renderLeaderboard(leaderboard) {
    var $container = $('#leaderboard-list');
    var $empty = $('#leaderboard-empty');

    if (!leaderboard || leaderboard.length === 0) {
        $container.hide();
        $empty.html(emptyState('No model scores yet')).show();
        return;
    }

    $empty.hide();
    $container.show();

    var html = '';
    for (var i = 0; i < leaderboard.length; i++) {
        var m = leaderboard[i];
        var idx = i + 1;
        var medalClass = idx === 1 ? 'bg-yellow-100 text-yellow-700' :
                         idx === 2 ? 'bg-gray-200 text-gray-600' :
                         idx === 3 ? 'bg-amber-100 text-amber-700' :
                         'bg-gray-100 text-gray-500';
        var score = m.best_score * 100;
        var scoreClass = score >= 80 ? 'bg-green-100 text-green-700' :
                         score >= 60 ? 'bg-amber-100 text-amber-700' :
                         'bg-red-100 text-red-700';
        var modelName = m.model_name;
        if (modelName.length > 25) modelName = modelName.substring(0, 25) + '...';
        var runWord = m.run_count === 1 ? 'run' : 'runs';
        var href = m.best_run_id ? '/history/' + m.best_run_id : '/history';

        html += '<a href="' + href + '" class="flex items-center justify-between p-4 no-underline hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors cursor-pointer">\n';
        html += '    <div class="flex items-center gap-3 min-w-0">\n';
        html += '        <div class="w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0 text-xs font-bold ' + medalClass + '">#' + idx + '</div>\n';
        html += '        <div class="min-w-0">\n';
        html += '            <div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate" title="' + m.model_name + '">' + modelName + '</div>\n';
        html += '            <div class="text-xs text-gray-400 dark:text-gray-500">' + m.run_count + ' ' + runWord + '</div>\n';
        html += '        </div>\n';
        html += '    </div>\n';
        html += '    <span class="text-xs font-semibold px-2.5 py-1 rounded-full flex-shrink-0 ' + scoreClass + '">' + score.toFixed(1) + '%</span>\n';
        html += '</a>';
    }

    $container.html(html);
}

function renderRecentRuns(runs) {
    var $container = $('#recent-runs-list');
    var $empty = $('#recent-runs-empty');

    if (!runs || runs.length === 0) {
        $container.hide();
        $empty.html(emptyState('No evaluation runs yet')).show();
        return;
    }

    $empty.hide();
    $container.show();

    var html = '';
    for (var i = 0; i < runs.length; i++) {
        var r = runs[i];
        var modelName = r.model_name || 'Unknown model';
        var runIdStr = String(r.run_id);

        var scoreHtml = '';
        if (r.overall_score !== null && r.overall_score !== undefined) {
            var score = r.overall_score * 100;
            var scoreClass = score >= 80 ? 'bg-green-100 text-green-700' :
                             score >= 60 ? 'bg-amber-100 text-amber-700' :
                             'bg-red-100 text-red-700';
            scoreHtml = '<span class="text-xs font-semibold px-2.5 py-1 rounded-full ' + scoreClass + '">' + score.toFixed(1) + '%</span>';
        } else {
            scoreHtml = '<span class="text-xs bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 px-2.5 py-1 rounded-full">--</span>';
        }

        html += '<a href="/history/' + r.run_id + '" class="flex items-center justify-between p-4 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors no-underline text-inherit">\n';
        html += '    <div class="min-w-0">\n';
        html += '        <div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate" title="' + r.model_name + '">' + modelName + '</div>\n';
        html += '        <div class="text-xs text-gray-400 dark:text-gray-500"><span class="font-mono">#' + runIdStr + '</span> · ' + r.passed_count + '/' + r.test_count + ' passed</div>\n';
        html += '    </div>\n';
        html += '    <div class="flex items-center gap-2 flex-shrink-0 ml-3">\n';
        html += scoreHtml;
        html += '    </div>\n';
        html += '</a>';
    }

    $container.html(html);
}

function renderModelUsage(modelUsage) {
    var $container = $('#model-usage-list');
    var $empty = $('#model-usage-empty');

    if (!modelUsage || modelUsage.length === 0) {
        $container.hide();
        $empty.html(emptyState('No model usage data yet')).show();
        return;
    }

    $empty.hide();
    $container.show();

    var maxCount = modelUsage[0].agent_count;
    var html = '';

    for (var i = 0; i < modelUsage.length; i++) {
        var item = modelUsage[i];
        var model = item.model;
        if (model.length > 30) model = model.substring(0, 30) + '...';
        var agentWord = item.agent_count === 1 ? 'agent' : 'agents';
        var pct = Math.round(item.agent_count / maxCount * 100);

        html += '<div>\n';
        html += '    <div class="flex justify-between items-center mb-1">\n';
        html += '        <span class="text-sm text-gray-700 dark:text-gray-300 font-medium truncate" title="' + item.model + '">' + model + '</span>\n';
        html += '        <span class="text-xs text-gray-400 dark:text-gray-500 flex-shrink-0 ml-2">' + item.agent_count + ' ' + agentWord + '</span>\n';
        html += '    </div>\n';
        html += '    <div class="w-full bg-gray-100 dark:bg-gray-700 rounded-full h-2">\n';
        html += '        <div class="bg-indigo-500 h-2 rounded-full" style="width: ' + pct + '%"></div>\n';
        html += '    </div>\n';
        html += '</div>';
    }

    $container.html(html);
}

function renderPluginCards(pluginCards) {
    var $container = $('#plugin-cards-container');
    $container.empty();

    if (!pluginCards || pluginCards.length === 0) {
        return;
    }

    var $quickActions = $('#quick-actions-card');
    var hasVisibleCards = false;

    for (var i = 0; i < pluginCards.length; i++) {
        var card = pluginCards[i];
        var items = card.items || [];

        var cardHtml = '<div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">';
        cardHtml += '  <div class="p-5 border-b border-gray-100 dark:border-gray-700 flex justify-between items-center">';
        cardHtml += '    <h3 class="text-base font-semibold text-gray-800 dark:text-gray-100 m-0">' + (card.title || 'Plugin Card') + '</h3>';
        if (card.link) {
            cardHtml += '    <a href="' + card.link + '" class="text-xs text-indigo-600 dark:text-indigo-400 hover:underline">View all</a>';
        }
        cardHtml += '  </div>';

        hasVisibleCards = true;
        if (items.length === 0) {
            // Empty state
            cardHtml += '  <div class="p-8 text-center text-gray-400 dark:text-gray-500 text-sm">';
            cardHtml += '    <svg class="w-8 h-8 mx-auto mb-2 text-gray-300 dark:text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>';
            cardHtml += '    <p>No items</p>';
            cardHtml += '  </div>';
        } else {
            cardHtml += '  <div class="divide-y divide-gray-50 dark:divide-gray-700">';

            var maxShow = Math.min(items.length, 5);
            for (var j = 0; j < maxShow; j++) {
                var item = items[j];
                var title = item.title || 'Untitled';
                var desc = item.description || '';
                if (desc.length > 60) {
                    desc = desc.substring(0, 60) + '...';
                }
                var created = '';
                if (item.created_at) {
                    try {
                        var d = new Date(item.created_at);
                        created = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
                    } catch(e) {}
                }

                cardHtml += '    <a href="' + (card.link || '#') + '" class="block p-4 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors no-underline text-inherit">';
                cardHtml += '      <div class="flex items-start gap-3">';
                cardHtml += '        <div class="w-2 h-2 rounded-full bg-amber-400 dark:bg-amber-500 mt-2 flex-shrink-0"></div>';
                cardHtml += '        <div class="min-w-0">';
                cardHtml += '          <div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">' + title + '</div>';
                if (desc) {
                    cardHtml += '          <div class="text-xs text-gray-400 dark:text-gray-500 mt-0.5">' + desc + '</div>';
                }
                if (created) {
                    cardHtml += '          <div class="text-xs text-gray-400 dark:text-gray-500 mt-1">Created ' + created + '</div>';
                }
                cardHtml += '        </div>';
                cardHtml += '      </div>';
                cardHtml += '    </a>';
            }

            cardHtml += '  </div>';
        }

        cardHtml += '</div>';
        $container.append(cardHtml);
    }

    // Adjust quick actions column span based on whether plugin cards are visible
    if (hasVisibleCards) {
        $quickActions.removeClass('lg:col-span-2').addClass('lg:col-span-1');
    }
}
