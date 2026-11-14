# Lightpanda Browser

Lightpanda is a headless browser written from scratch in Zig — not a Chromium fork. It uses
roughly 16x less memory and runs about 9x faster than headless Chrome, which makes it the default
choice for reading pages, crawling, and scripted extraction. It is **not** pixel-identical to
Chrome: WebGL and canvas are unavailable and screenshots reflect its computed text layout.

This skill drives a **local binary** (no Docker, no Cloud, no API key). Interactive browsing goes
through Lightpanda's own MCP server, which the skill starts on demand.

## Which tool to use

| Goal | Tool |
|------|------|
| Read one page, no session needed | `lightpanda_fetch` |
| Read the page you are already on | `lightpanda_read` |
| Click, type, submit, scroll, wait | `lightpanda_act` |
| Pull specific fields with CSS selectors | `lightpanda_extract` |
| Save a visual snapshot to a file | `lightpanda_screenshot` |
| Replay a deterministic multi-step script | `lightpanda_run` |
| Anything else in the MCP surface | `lightpanda_mcp` |
| Something is broken | `lightpanda_status`, then `./evonic doctor` |
| Free the browser's memory | `lightpanda_stop` |

`lightpanda_fetch` shells out to `lightpanda fetch` and needs no server — prefer it for one-off
reads. Everything else uses the MCP server, which is started automatically on first use and keeps
one browsing session (page, cookies, storage) per agent.

## Typical flows

**Read a documentation page**

```
lightpanda_fetch(url="https://example.com/docs", dump="markdown")
```

**Navigate, inspect, interact, re-read**

```
lightpanda_act(action="goto", url="https://shop.example.com")
lightpanda_read(format="tree", max_depth=4)          # cheap overview, gives node ids
lightpanda_read(format="links", limit=50)            # discover targets
lightpanda_act(action="fill", selector="#q", value="zig")
lightpanda_act(action="press", key="Enter")
lightpanda_read(format="markdown")
```

**Extract fields without reading the whole page**

```
lightpanda_extract(url="https://news.example.com", schema={
  "headline": "h1",
  "author": ".byline",
  "links": ["article a@href"]
})
```

**Escape hatch: evaluate JavaScript in the page**

```
lightpanda_mcp(tool="evaluate",
               arguments={"script": "return document.querySelectorAll('tr').length"})
```

## Reading strategy (token efficiency)

1. `tree` with `max_depth` first — role/name outline with `backendNodeId`s.
2. `markdown` on a scoped `selector` instead of the whole document.
3. `links` / `structuredData` / `detectForms` when you need one specific dimension.
4. `html` last: it is the most expensive representation.

Output is capped at `max_bytes` (default 200,000). Truncation is always marked with
`[truncated at N bytes]`; narrow the scope rather than raising the cap.

## MCP tool catalog (`lightpanda_mcp`)

Pass the tool name plus its `arguments` object. Selectors are **plain CSS only** —
Playwright-style prefixes (`text=`, `role=`, `xpath=`) fail with `InvalidSelector`; use
`findElement` for role/name lookups.

**Navigation**
- `goto` `{url, waitUntil?, timeout?}`
- `download` `{url, path}` — fetch with session cookies; only 200–203 bodies are written
- `search` `{query}` — web search as markdown (needs `BRAVE_API_KEY` / `TAVILY_API_KEY` /
  `EXA_API_KEY`, or Keenable's public endpoint)

**Reading**
- `markdown` `{selector?, backendNodeId?, maxBytes?, strip?}`
- `html` `{selector?, backendNodeId?, maxBytes?, strip?}`
- `tree` `{url?, backendNodeId?, maxDepth?, maxBytes?}`
- `links` `{limit?, url?}`
- `structuredData` `{url?}` — JSON-LD, OpenGraph, Twitter, meta, link tags
- `nodeDetails` `{backendNodeId}` — attributes, state, ready-to-use selector
- `findElement` `{role?, name?}` · `interactiveElements` `{}` · `detectForms` `{}`

**Acting**
- `click` `{selector|backendNodeId}`
- `fill` `{value, selector|backendNodeId}`
- `press` `{key, selector?}`
- `selectOption` `{value, selector|backendNodeId}`
- `setChecked` `{checked, selector|backendNodeId}`
- `hover` `{selector|backendNodeId}` · `scroll` `{x?, y?, selector?}`
- `waitForSelector` `{selector, timeout?}` · `waitForScript` `{script, timeout?}` ·
  `waitForState` `{state, timeout?}`

**Data & scripting**
- `extract` `{schema, save?}` — schema is a JSON object literal string
- `evaluate` `{script, url?, save?}` — top-level `await`/`return` supported; `save` exposes
  the value as `lp.<name>` to later evaluates
- `run` `{script|path, timeout?}` — PandaScript
- `save` `{}` — export the session as a reusable PandaScript
- `classify` / `acceptCookies` / `login` — need `TYPESAFE_API_KEY` unless you pass the
  selectors an earlier call returned

**State & debugging**
- `getUrl` `{}` · `getCookies` `{url?, all?}` · `getEnv` `{name?}`
- `consoleLogs` `{}` · `popups` `{follow?}`
- `pageTools` `{}` · `callPageTool` `{name, arguments?}` — WebMCP tools the page exposes

**Sessions**
- `session_new` `{name?}` · `session_list` `{}` · `session_close` `{id}`
- Each agent already gets its own session; use these only for extra isolation or sharing.

## Safety and etiquette

- Only `http` and `https` URLs are accepted. `file:`, `data:`, `javascript:` and similar schemes
  are rejected before anything runs.
- **Block private networks** is on by default: localhost, private ranges and `.local`/`.internal`
  names are refused, and the server passes `--block-private-networks` so DNS rebinding is blocked
  after resolution. Disable it only for a trusted intranet host.
- **Obey robots.txt** is off by default so pages you are authorized to read work immediately.
  Turn it on before any crawl or bulk fetch.
- The browser sends `Lightpanda/<version>` as its User-Agent. Do not use this skill to bypass
  logins, paywalls, CAPTCHAs, or rate limits.
- Keep fetches polite: one page at a time, no tight loops, honor `Retry-After`.

## Limits worth remembering

- No WebGL, no canvas rendering, no pixel-accurate screenshots.
- Subframe (iframe) loading is off in fetch by default; MCP reads report frames as their URL.
- One browser context per process: 16 parallel pages maximum on a CDP connection.
- V8 coverage is broad but not complete; heavy SPA frameworks may render partially. When the
  content is missing, check `goto`'s notes (bot challenge, 403/429, WebGL) before retrying.
- Pages behind a JS challenge usually cannot be read; report that instead of retrying.

## Configuration

Skill settings (Agent → Skills → Lightpanda):

| Setting | Default | Purpose |
|---------|---------|---------|
| Binary path | auto | Explicit `lightpanda` path; auto-detects `$LIGHTPANDA_BIN`, `skills/lightpanda/bin/`, then `PATH` |
| MCP host | `127.0.0.1` | Interface for the auto-started server |
| MCP port | `9223` | Server port; change it if something else owns 9223 |
| Obey robots.txt | off | Politeness default for every fetch |
| Block private networks | on | SSRF guard |

Telemetry is disabled for every child process (`LIGHTPANDA_DISABLE_TELEMETRY=true`) and core
dumps are suppressed (`LIGHTPANDA_DISABLE_CORE_DUMP=1`).

Runtime state (server pid, session ids, logs, screenshots, `download` output and saved
PandaScripts) lives in `~/.cache/evonic/lightpanda/` and never inside the repository; the MCP
server runs from that directory, so relative paths passed to `download`/`run` resolve there.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `lightpanda binary not found` | Run `./evonic doctor --fix` (or `bash skills/lightpanda/scripts/install.sh`). |
| `cannot execute: required file not found` / GLIBC error | musl/Alpine host; release binaries need glibc ≥ 2.35 (x86_64) / 2.38 (arm64). Use a glibc distro or build from source. |
| Port 9223 already in use | Another process owns it: change **MCP port** in the skill settings, or `lightpanda_stop`. |
| Session state looks wrong / page won't advance | `lightpanda_stop` then retry; a fresh server and session are created automatically. |
| `InvalidSelector` | Selectors must be plain CSS. Use `findElement` with `role`/`name` for text-based lookup. |
| Empty markdown, HTTP 403/429 | Bot detection or rate limit. Slow down, honor robots, or use the site's API. |
| Screenshot looks unstyled or sparse | Lightpanda renders computed text layout; CSS/WebGL-dependent visuals are approximate. |

`lightpanda_status` reports the binary version, server pid/port/log, active sessions and the
safety settings — attach that output when reporting a problem.
