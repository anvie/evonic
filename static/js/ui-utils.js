/**
 * ui-utils.js — UI Utility Bundle (Toast Notification + Confirm Dialog)
 *
 * jQuery + Tailwind CSS. Provides a unified `window.ui` namespace with
 * toast notifications and confirm dialogs that look consistent across
 * every page. Backward-compatible: `window.toast` and `window.showConfirm`
 * aliases are preserved.
 *
 * Usage:
 *   // Toast
 *   ui.toast.success('Saved!');
 *   ui.toast.error('Failed to save');
 *   ui.toast.info('Syncing…', 6000);
 *
 *   // Confirm (promise-based)
 *   const ok = await ui.confirm({ title: 'Delete?', message: 'Are you sure?' });
 *   // or with legacy alias
 *   const ok = await showConfirm({ title: 'Delete?', message: 'Sure?' });
 */
(function($) {
    'use strict';

    /* ====================================================================
     *  TOAST NOTIFICATION
     * ==================================================================== */
    var toastConfig = {
        position: 'top-center',
        maxToasts: 5,
        duration: 4000,
        animationDuration: 300,
        gap: 12
    };

    var $toastContainer = null;

    function toastInit() {
        if ($toastContainer) return;
        var posMap = {
            'top-right':    'fixed top-4 right-4 z-[9999]',
            'top-left':     'fixed top-4 left-4 z-[9999]',
            'top-center':   'fixed top-4 left-1/2 -translate-x-1/2 z-[9999]',
            'bottom-right': 'fixed bottom-4 right-4 z-[9999]',
            'bottom-left':  'fixed bottom-4 left-4 z-[9999]'
        };
        $toastContainer = $('<div id="toastContainer">')
            .addClass(posMap[toastConfig.position] || posMap['top-right'])
            .css({ display: 'flex', 'flex-direction': 'column', gap: toastConfig.gap + 'px' });
        $('body').append($toastContainer);
    }

    function toastIcon(type) {
        var icons = {
            success: '<svg class="w-5 h-5 text-white flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>',
            error:   '<svg class="w-5 h-5 text-white flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>',
            warning: '<svg class="w-5 h-5 text-white flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>',
            info:    '<svg class="w-5 h-5 text-white flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>'
        };
        return icons[type] || icons.info;
    }

    function toastStyle(type) {
        var styles = {
            success: { bg: 'bg-green-500' },
            error:   { bg: 'bg-red-500' },
            warning: { bg: 'bg-yellow-500' },
            info:    { bg: 'bg-blue-500' }
        };
        return styles[type] || styles.info;
    }

    function toastShow(message, type, duration) {
        type = type || 'info';
        duration = duration || toastConfig.duration;
        toastInit();

        var $toasts = $toastContainer.children();
        if ($toasts.length >= toastConfig.maxToasts) {
            $toasts.first().fadeOut(toastConfig.animationDuration, function() { $(this).remove(); });
        }

        var style = toastStyle(type);
        var initTransform = toastConfig.position === 'top-center' ? 'translateY(-100%)' : 'translateX(100%)';

        var $toast = $('<div>')
            .addClass('flex items-center gap-3 px-4 py-3 rounded-lg shadow-lg ' + style.bg)
            .css({ 'max-width': '380px', 'min-width': '250px', cursor: 'pointer',
                   opacity: '0', transform: initTransform,
                   transition: 'opacity ' + toastConfig.animationDuration + 'ms ease, transform ' + toastConfig.animationDuration + 'ms ease' });

        $toast.append(toastIcon(type));

        $toast.append($('<div>').addClass('text-white text-sm font-medium flex-1').text(message));

        var $close = $('<button>')
            .addClass('text-white hover:text-gray-200 ml-2 flex-shrink-0')
            .html('&times;').css({ 'font-size': '18px', 'line-height': '1', padding: '0 4px' });
        $toast.append($close);

        $toast.on('click', function(e) {
            if (!$(e.target).closest($close).length) toastDismiss($toast);
        });
        $close.on('click', function(e) { e.stopPropagation(); toastDismiss($toast); });

        $toastContainer.append($toast);

        setTimeout(function() {
            $toast.css({ opacity: '1', transform: toastConfig.position === 'top-center' ? 'translateY(0)' : 'translateX(0)' });
        }, 10);

        var timer = setTimeout(function() { toastDismiss($toast); }, duration);

        $toast.on('mouseenter', function() { clearTimeout(timer); });
        $toast.on('mouseleave', function() {
            timer = setTimeout(function() { toastDismiss($toast); }, duration);
        });

        return $toast;
    }

    function toastDismiss($toast) {
        if (!$toast || $toast.data('dismissed')) return;
        $toast.data('dismissed', true);
        $toast.css({ opacity: '0', transform: toastConfig.position === 'top-center' ? 'translateY(-100%)' : 'translateX(100%)' });
        setTimeout(function() { $toast.remove(); }, toastConfig.animationDuration);
    }

    function toastClear() {
        if ($toastContainer) $toastContainer.children().each(function() { toastDismiss($(this)); });
    }

    var toastApi = {
        show: toastShow,
        success: function(msg, dur) { return toastShow(msg, 'success', dur); },
        error:   function(msg, dur) { return toastShow(msg, 'error', dur); },
        warning: function(msg, dur) { return toastShow(msg, 'warning', dur); },
        info:    function(msg, dur) { return toastShow(msg, 'info', dur); },
        clear: toastClear,
        configure: function(cfg) { $.extend(toastConfig, cfg); }
    };

    /* ====================================================================
     *  CONFIRM DIALOG
     * ==================================================================== */
    var confirmModalHtml =
        '<div id="confirmModal" class="hidden fixed inset-0 z-[250] flex items-center justify-center">' +
            '<div id="confirmOverlay" class="absolute inset-0 bg-black/50 transition-opacity"></div>' +
            '<div id="confirmBox" class="relative bg-white dark:bg-gray-800 rounded-xl w-[90%] max-w-[400px] shadow-xl transform transition-all scale-95 opacity-0">' +
                '<div class="p-6">' +
                    '<div class="flex items-start gap-3">' +
                        '<div id="confirmIconDanger" class="hidden flex-shrink-0 w-10 h-10 rounded-full bg-red-100 dark:bg-red-900/30 items-center justify-center">' +
                            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="w-5 h-5 text-red-600 dark:text-red-400"><path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 9a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z" clip-rule="evenodd"/></svg>' +
                        '</div>' +
                        '<div id="confirmIconInfo" class="hidden flex-shrink-0 w-10 h-10 rounded-full bg-indigo-100 dark:bg-indigo-900/30 items-center justify-center">' +
                            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="w-5 h-5 text-indigo-600 dark:text-indigo-400"><path fill-rule="evenodd" d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-7-4a1 1 0 1 1-2 0 1 1 0 0 1 2 0ZM9 9a.75.75 0 0 0 0 1.5h.253a.25.25 0 0 1 .244.304l-.459 2.066A1.75 1.75 0 0 0 10.747 15H11a.75.75 0 0 0 0-1.5h-.253a.25.25 0 0 1-.244-.304l.459-2.066A1.75 1.75 0 0 0 9.253 9H9Z" clip-rule="evenodd"/></svg>' +
                        '</div>' +
                        '<div class="flex-1 min-w-0">' +
                            '<h3 id="confirmTitle" class="text-base font-semibold text-gray-900 dark:text-gray-100"></h3>' +
                            '<p id="confirmMessage" class="mt-1 text-sm text-gray-500 dark:text-gray-400"></p>' +
                        '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="flex gap-3 justify-end px-6 pb-5">' +
                    '<button id="confirmCancelBtn" class="px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors">Cancel</button>' +
                    '<button id="confirmOkBtn" class="px-4 py-2 rounded-md text-sm font-medium text-white transition-colors"></button>' +
                '</div>' +
            '</div>' +
        '</div>';

    var $confirmModal = null;
    var _confirmResolve = null;

    function confirmInit() {
        if ($confirmModal) return;
        $confirmModal = $(confirmModalHtml);
        $('body').append($confirmModal);

        $('#confirmCancelBtn').on('click', function() { confirmClose(false); });
        $('#confirmOverlay').on('click', function() { confirmClose(false); });
        $(document).on('keydown.confirmModal', function(e) {
            if (e.key === 'Escape' && _confirmResolve) confirmClose(false);
        });
    }

    function confirmOpen(opts) {
        confirmInit();
        var danger = opts.danger !== false;

        $('#confirmTitle').text(opts.title || 'Confirm');
        $('#confirmMessage').text(opts.message || '');
        $('#confirmOkBtn').text(opts.confirmText || 'Confirm');
        $('#confirmCancelBtn').text(opts.cancelText || 'Cancel');

        if (danger) {
            $('#confirmOkBtn').attr('class', 'px-4 py-2 rounded-md text-sm font-medium text-white bg-red-600 hover:bg-red-700 transition-colors');
            $('#confirmIconDanger').removeClass('hidden').addClass('flex');
            $('#confirmIconInfo').removeClass('flex').addClass('hidden');
        } else {
            $('#confirmOkBtn').attr('class', 'px-4 py-2 rounded-md text-sm font-medium text-white bg-indigo-500 hover:bg-indigo-600 transition-colors');
            $('#confirmIconInfo').removeClass('hidden').addClass('flex');
            $('#confirmIconDanger').removeClass('flex').addClass('hidden');
        }

        $confirmModal.removeClass('hidden');
        requestAnimationFrame(function() {
            $('#confirmBox').removeClass('scale-95 opacity-0').addClass('scale-100 opacity-100');
        });

        return new Promise(function(resolve) {
            _confirmResolve = resolve;
            $('#confirmOkBtn').off('click.confirmAction').on('click.confirmAction', function() { confirmClose(true); });
        });
    }

    function confirmClose(result) {
        if (!_confirmResolve) return;
        $('#confirmBox').removeClass('scale-100 opacity-100').addClass('scale-95 opacity-0');
        setTimeout(function() { $confirmModal.addClass('hidden'); }, 150);
        var resolve = _confirmResolve;
        _confirmResolve = null;
        resolve(result);
    }

    var confirmApi = function(opts) {
        return confirmOpen(opts);
    };

    /* ====================================================================
     *  COPY CODE BLOCK BUTTONS
     * ==================================================================== */

    var _COPY_ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
    var _CHECK_ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

    function _copyText(text, onDone) {
        var fallback = function() {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); onDone(); } catch (_) {}
            document.body.removeChild(ta);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(onDone).catch(fallback);
        } else {
            fallback();
        }
    }

    // Adds hover-revealed copy buttons to <pre> and <blockquote> elements
    // inside the given container. Reuses pre-existing data attribute to
    // avoid attaching duplicate buttons to the same element.
    function addCopyButtons(container) {
        var $container = $(container);
        if (!$container.length) return;

        $container.find('pre, blockquote').each(function () {
            var $el = $(this);
            if ($el.data('copyAttached')) return;
            $el.data('copyAttached', true);

            // Wrap so the button stays pinned even when a <pre> scrolls horizontally.
            $el.wrap($('<div>').css({ position: 'relative' }));
            var $wrapper = $el.parent();

            var $btn = $('<button type="button">')
                .attr('title', 'Copy')
                .attr('aria-label', 'Copy to clipboard')
                .css({
                    position: 'absolute',
                    bottom: '6px',
                    right: '6px',
                    zIndex: 5,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    width: '28px',
                    height: '28px',
                    padding: '0',
                    borderRadius: '6px',
                    border: 'none',
                    cursor: 'pointer',
                    color: 'currentColor',
                    backgroundColor: 'rgba(128,128,128,0.25)',
                    opacity: 0,
                    transition: 'opacity 150ms ease',
                })
                .html(_COPY_ICON)
                .on('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    var text = $el.is('pre') ? ($el.find('code').text() || $el.text()) : $el.text();
                    _copyText(text, function () {
                        $btn.html(_CHECK_ICON).css('color', '#22c55e');
                        setTimeout(function () { $btn.html(_COPY_ICON).css('color', 'currentColor'); }, 1500);
                    });
                });
            $wrapper.append($btn);

            $wrapper.on('mouseenter', function () { $btn.css('opacity', 1); });
            $wrapper.on('mouseleave', function () { if (!$btn.is(':focus')) $btn.css('opacity', 0); });
            $btn.on('mouseenter', function () { $(this).css('backgroundColor', 'rgba(128,128,128,0.4)'); });
            $btn.on('mouseleave', function () { $(this).css('backgroundColor', 'rgba(128,128,128,0.25)'); });
            $btn.on('focus', function () { $(this).css({ opacity: 1, outline: '2px solid rgba(128,128,128,0.7)', outlineOffset: '1px' }); });
            $btn.on('blur', function () {
                $(this).css({ outline: 'none', outlineOffset: '0' });
                if (!$wrapper.is(':hover')) $btn.css('opacity', 0);
            });
        });
    }

    /* ====================================================================
     *  PUBLIC API
     * ==================================================================== */
    window.ui = {
        toast: toastApi,
        confirm: confirmApi,
        addCopyButtons: addCopyButtons
    };

    // Backward compatibility
    window.toast = toastApi;
    window.showConfirm = confirmApi;

})(jQuery);
