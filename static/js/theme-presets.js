/**
 * theme-presets.js — VS Code-style colour theme presets (dark + light families).
 *
 * The stylesheets hard-code a slate/indigo palette. Rather than rewriting every rule, a preset
 * re-tints that palette at runtime: neutral colours (slate/gray/white) take the preset's hue,
 * saturation and lightness shift, and the indigo/violet accent family (hue 225-300) is rotated to the preset's accent hue.
 * Status colours (red/green/amber/…) are left alone. Inline `style=""` attributes are untouched.
 *
 *   EvTheme.boot()                       head script: sets <html class="dark"> + background, hides the page until re-tinted
 *   EvTheme.set({mode, dark, light})     change mode ('light'|'system'|'dark') and/or the preset used for each scheme
 *   EvTheme.get()                        {mode, dark, light, isDark, active}
 *   EvTheme.PRESETS                      catalogue (with swatch colours) for the settings picker
 *
 * Stored in localStorage: evonic-theme (mode), evonic-theme-dark, evonic-theme-light (preset ids).
 */
(function (global) {
    'use strict';

    var KEYS = { mode: 'evonic-theme', dark: 'evonic-theme-dark', light: 'evonic-theme-light' };
    var DEFAULTS = { dark: 'midnight', light: 'daylight' };

    // hue/sat: tint for neutrals · shift: lightness move for backgrounds (dark: lighten, light: darken) · accent: hue the indigo family rotates to
    var PRESETS = [
        { id: 'midnight',    name: 'Midnight',        mode: 'dark',  original: true },
        { id: 'dracula',     name: 'Dracula',         mode: 'dark',  hue: 231, sat: 0.15, shift: 0.12,  accent: 265 },
        { id: 'one-dark',    name: 'One Dark',        mode: 'dark',  hue: 220, sat: 0.13, shift: 0.11,  accent: 207 },
        { id: 'tokyo-night', name: 'Tokyo Night',     mode: 'dark',  hue: 235, sat: 0.23, shift: 0.06,  accent: 222 },
        { id: 'github-dark', name: 'GitHub Dark',     mode: 'dark',  hue: 216, sat: 0.27, shift: 0.0,   accent: 212 },
        { id: 'nord',        name: 'Nord',            mode: 'dark',  hue: 220, sat: 0.16, shift: 0.16,  accent: 213 },
        { id: 'catppuccin',  name: 'Catppuccin Mocha',mode: 'dark',  hue: 240, sat: 0.21, shift: 0.08,  accent: 267 },
        { id: 'monokai',     name: 'Monokai',         mode: 'dark',  hue: 70,  sat: 0.08, shift: 0.09,  accent: 335 },
        { id: 'gruvbox-dark',name: 'Gruvbox Dark',    mode: 'dark',  hue: 25,  sat: 0.10, shift: 0.10,  accent: 38 },
        { id: 'solarized-dark', name: 'Solarized Dark', mode: 'dark', hue: 195, sat: 0.55, shift: 0.05,  accent: 205 },
        { id: 'pure-black',  name: 'Pure Black',      mode: 'dark',  hue: 0,   sat: 0.0,  shift: -0.08, accent: null },

        { id: 'daylight',    name: 'Daylight',        mode: 'light', original: true },
        { id: 'github-light',name: 'GitHub Light',    mode: 'light', hue: 210, sat: 0.29, shift: 0.0,   accent: 212 },
        { id: 'one-light',   name: 'One Light',       mode: 'light', hue: 230, sat: 0.08, shift: 0.02,  accent: 222 },
        { id: 'latte',       name: 'Catppuccin Latte',mode: 'light', hue: 228, sat: 0.20, shift: 0.05,  accent: 266 },
        { id: 'nord-light',  name: 'Nord Light',      mode: 'light', hue: 218, sat: 0.20, shift: 0.06,  accent: 213 },
        { id: 'rose-pine-dawn', name: 'Rosé Pine Dawn', mode: 'light', hue: 32, sat: 0.40, shift: 0.035, accent: 267 },
        { id: 'paper',       name: 'Paper',           mode: 'light', hue: 38,  sat: 0.14, shift: 0.035, accent: 25 },
        { id: 'solarized-light', name: 'Solarized Light', mode: 'light', hue: 44, sat: 0.45, shift: 0.06, accent: 205 },
        { id: 'gruvbox-light', name: 'Gruvbox Light', mode: 'light', hue: 46,  sat: 0.50, shift: 0.10,  accent: 30 }
    ];

    // ---------- colour maths ----------
    function rgbToHsl(r, g, b) {
        r /= 255; g /= 255; b /= 255;
        var max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, h = 0, s = 0, d = max - min;
        if (d) {
            s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
            if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
            else if (max === g) h = (b - r) / d + 2;
            else h = (r - g) / d + 4;
            h *= 60;
        }
        return [h, s, l];
    }
    function hslToRgb(h, s, l) {
        h = ((h % 360) + 360) % 360 / 360;
        if (!s) { var v = Math.round(l * 255); return [v, v, v]; }
        var q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
        function f(t) {
            if (t < 0) t += 1; if (t > 1) t -= 1;
            if (t < 1 / 6) return p + (q - p) * 6 * t;
            if (t < 1 / 2) return q;
            if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
            return p;
        }
        return [Math.round(f(h + 1 / 3) * 255), Math.round(f(h) * 255), Math.round(f(h - 1 / 3) * 255)];
    }
    function clamp(x, a, b) { return Math.min(b, Math.max(a, x)); }

    /** Map one rgb colour through a preset. Returns [r, g, b]. */
    function mapRgb(r, g, b, p) {
        if (!p || p.original) return [r, g, b];
        if (r === 0 && g === 0 && b === 0 && p.shift >= 0) return [r, g, b];   // shadows / overlays stay black
        var hsl = rgbToHsl(r, g, b), h = hsl[0], s = hsl[1], l = hsl[2];
        var neutral = s < 0.12 || (h >= 190 && h <= 262 && s < 0.55);
        if (neutral) {
            var nl = l;
            if (p.mode === 'dark') { if (l < 0.5) nl = l + p.shift * (1 - l / 0.5); }
            else if (l > 0.5) nl = l - p.shift * ((l - 0.5) / 0.5);
            var ns = p.sat * (p.mode === 'dark' ? (l > 0.5 ? 0.5 : 1) : (l < 0.5 ? 0.6 : 1));
            return hslToRgb(p.hue, clamp(ns, 0, 1), clamp(nl, 0, 1));
        }
        if (p.accent != null && s >= 0.45 && h >= 225 && h <= 300) return hslToRgb(h + (p.accent - 239), s, l);
        return [r, g, b];
    }

    /** oklch → sRGB (0-255, clamped). Tailwind v4 defines its gray/slate palette in oklch. */
    function oklchToRgb(L, C, H) {
        var a = C * Math.cos(H * Math.PI / 180), b = C * Math.sin(H * Math.PI / 180);
        var l = Math.pow(L + 0.3963377774 * a + 0.2158037573 * b, 3);
        var m = Math.pow(L - 0.1055613458 * a - 0.0638541728 * b, 3);
        var s = Math.pow(L - 0.0894841775 * a - 1.2914855480 * b, 3);
        var lin = [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                   -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                   -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s];
        return lin.map(function (v) {
            v = clamp(v, 0, 1);
            v = v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
            return Math.round(clamp(v, 0, 1) * 255);
        });
    }

    var HEX_RE = /#([0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{3})(?![0-9a-z_-])/gi;
    var RGB_RE = /(rgba?\(\s*)(\d+\.?\d*)(\s*,\s*|\s+)(\d+\.?\d*)(\s*,\s*|\s+)(\d+\.?\d*)/gi;
    var OKLCH_RE = /oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)(?:deg)?(\s*\/\s*[^)]+)?\)/gi;
    var ANY_COLOR_RE = /#[0-9a-f]{3,8}(?![0-9a-z_-])|rgba?\(\s*\d|oklch\(/i;

    function hex2(n) { var s = n.toString(16); return s.length < 2 ? '0' + s : s; }

    /** Re-tint every colour token inside a CSS value string. */
    function mapValue(value, p) {
        value = value.replace(OKLCH_RE, function (m, l, pct, c, h, alpha) {
            var L = pct ? +l / 100 : +l, rgb = oklchToRgb(L, +c, +h), out = mapRgb(rgb[0], rgb[1], rgb[2], p);
            if (out[0] === rgb[0] && out[1] === rgb[1] && out[2] === rgb[2]) return m;       // untouched (status/accent colours)
            return 'rgb(' + out[0] + ' ' + out[1] + ' ' + out[2] + (alpha || '') + ')';
        });
        value = value.replace(RGB_RE, function (m, fn, r, s1, g, s2, b) {
            var c = mapRgb(+r, +g, +b, p);
            return fn + c[0] + s1 + c[1] + s2 + c[2];
        });
        return value.replace(HEX_RE, function (m, d) {
            var a = '';
            if (d.length === 3 || d.length === 4) { a = d.length === 4 ? d.charAt(3) + d.charAt(3) : ''; d = d.charAt(0) + d.charAt(0) + d.charAt(1) + d.charAt(1) + d.charAt(2) + d.charAt(2); }
            else if (d.length === 8) { a = d.slice(6); d = d.slice(0, 6); }
            var c = mapRgb(parseInt(d.slice(0, 2), 16), parseInt(d.slice(2, 4), 16), parseInt(d.slice(4, 6), 16), p);
            return '#' + hex2(c[0]) + hex2(c[1]) + hex2(c[2]) + a;
        });
    }

    function find(id) { for (var i = 0; i < PRESETS.length; i++) if (PRESETS[i].id === id) return PRESETS[i]; return null; }
    function swatch(p) {
        var m = function (hex) { return mapValue(hex, p); };
        var dark = p.mode === 'dark';
        return dark
            ? { bg: m('#0a0f1c'), surface: m('#1f2937'), border: m('#374151'), text: m('#e5e7eb'), accent: m('#6366f1') }
            : { bg: m('#f1f2f4'), surface: m('#ffffff'), border: m('#e5e7eb'), text: m('#111827'), accent: m('#6366f1') };
    }
    PRESETS.forEach(function (p) { p.swatch = swatch(p); });

    // ---------- state ----------
    function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
    function write(key, val) { try { localStorage.setItem(key, val); } catch (e) { /* private mode */ } }

    function get() {
        var mode = read(KEYS.mode) || 'system';
        var dark = find(read(KEYS.dark)) && find(read(KEYS.dark)).mode === 'dark' ? read(KEYS.dark) : DEFAULTS.dark;
        var light = find(read(KEYS.light)) && find(read(KEYS.light)).mode === 'light' ? read(KEYS.light) : DEFAULTS.light;
        var sysDark = !!(global.matchMedia && global.matchMedia('(prefers-color-scheme: dark)').matches);
        var isDark = mode === 'dark' || (mode === 'system' && sysDark);
        return { mode: mode, dark: dark, light: light, isDark: isDark, active: find(isDark ? dark : light) };
    }

    // ---------- stylesheet re-tinting ----------
    var tracked = [];            // [{style, entries: [[prop, originalValue, priority]]}]
    var seen = typeof WeakSet === 'function' ? new WeakSet() : null;
    var current = null;          // preset currently painted into the CSSOM
    var cache = {};              // preset id -> {orig value -> mapped value}

    function collect(rule) {
        var st = rule.style;
        if (st && st.length) {
            var entries = [];
            for (var i = 0; i < st.length; i++) {
                var name = st[i], val = st.getPropertyValue(name);
                if (val && val.indexOf('url(') < 0 && ANY_COLOR_RE.test(val)) entries.push([name, val, st.getPropertyPriority(name)]);
            }
            if (entries.length) tracked.push({ style: st, entries: entries });
        }
        if (rule.cssRules) walk(rule.cssRules);
    }
    function walk(rules) { for (var i = 0; i < rules.length; i++) collect(rules[i]); }

    function scanNewSheets() {
        var added = false;
        for (var i = 0; i < document.styleSheets.length; i++) {
            var sheet = document.styleSheets[i];
            if (seen && seen.has(sheet)) continue;
            var rules;
            try { rules = sheet.cssRules; } catch (e) { rules = null; }      // cross-origin sheet
            if (seen) seen.add(sheet);
            if (rules) { walk(rules); added = true; }
        }
        return added;
    }

    function paint(preset) {
        var key = preset && !preset.original ? preset.id : '';
        var memo = cache[key] || (cache[key] = {});
        for (var i = 0; i < tracked.length; i++) {
            var t = tracked[i];
            for (var j = 0; j < t.entries.length; j++) {
                var e = t.entries[j];
                var v = key ? (memo[e[1]] || (memo[e[1]] = mapValue(e[1], preset))) : e[1];
                if (t.style.getPropertyValue(e[0]) !== v) t.style.setProperty(e[0], v, e[2]);
            }
        }
        current = key;
    }

    function retint() {
        var st = get(), key = st.active && !st.active.original ? st.active.id : '';
        // Nothing painted yet and the default preset is active → skip the (expensive) scan entirely.
        if (!key && current === null) return;
        scanNewSheets();
        paint(key ? st.active : null);
    }

    // ---------- DOM state ----------
    function pageBackground(st) { return mapValue(st.isDark ? '#0a0f1c' : '#f1f2f4', st.active); }
    function applyClasses() {
        var st = get(), html = document.documentElement;
        html.classList.toggle('dark', st.isDark);
        html.setAttribute('data-theme-preset', st.active ? st.active.id : '');
        html.style.backgroundColor = pageBackground(st);
        return st;
    }

    var hideEl = null;
    function reveal() { if (hideEl && hideEl.parentNode) hideEl.parentNode.removeChild(hideEl); hideEl = null; }

    function boot() {
        var st = applyClasses();
        var custom = st.active && !st.active.original;
        if (custom) {
            // Hide until the stylesheets are re-tinted, otherwise the stock palette flashes on every navigation.
            hideEl = document.createElement('style');
            hideEl.textContent = 'html{visibility:hidden}';
            document.head.appendChild(hideEl);
            setTimeout(reveal, 2000);                       // failsafe
        }
        var done = false;
        function run() { if (done) return; done = true; try { retint(); } finally { reveal(); } }
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
        global.addEventListener('load', function () { try { retint(); } catch (e) { /* ignore */ } });
        if (global.MutationObserver) {
            var t = null;
            new MutationObserver(function () { clearTimeout(t); t = setTimeout(function () { if (current !== null || custom) retint(); }, 50); })
                .observe(document.head, { childList: true });
        }
        if (global.matchMedia) {
            var mq = global.matchMedia('(prefers-color-scheme: dark)');
            var onSys = function () { if (get().mode === 'system') { applyClasses(); retint(); } };
            if (mq.addEventListener) mq.addEventListener('change', onSys); else if (mq.addListener) mq.addListener(onSys);
        }
    }

    function set(opts) {
        opts = opts || {};
        if (opts.mode) write(KEYS.mode, opts.mode);
        if (opts.dark && find(opts.dark) && find(opts.dark).mode === 'dark') write(KEYS.dark, opts.dark);
        if (opts.light && find(opts.light) && find(opts.light).mode === 'light') write(KEYS.light, opts.light);
        applyClasses();
        retint();
        try { document.dispatchEvent(new CustomEvent('evonic:theme-changed', { detail: get() })); } catch (e) { /* old browser */ }
        return get();
    }

    global.EvTheme = { PRESETS: PRESETS, DEFAULTS: DEFAULTS, boot: boot, set: set, get: get, mapValue: mapValue, mapRgb: mapRgb };
})(window);
