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
| Read a page behind a login, using your own cookies | `lightpanda_cookies` + `lightpanda_fetch` |
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

## Authenticated pages (browser cookie dump)

Cookies come from **one textarea** in Settings → Skills → Lightpanda → *Cookies*: paste a
Netscape `cookies.txt` export from the browser exactly as it comes. There is nothing else to
configure — the domain column on each line says which site the cookie belongs to, so several
sites each keep their own session. `lightpanda_cookies` reads and writes that same value, so
the UI and the agent never disagree. Values are never echoed back by a tool result, and the
jar materialized on disk is `0600` outside the repository.

Format: 7 tab-separated columns per line — domain, includeSubdomains, path, secure, expiry,
name, value. `#HttpOnly_` lines and `#` comments are accepted.

```
# Netscape HTTP Cookie File
.x.com	TRUE	/	TRUE	1790000000	auth_token	...
.x.com	TRUE	/	TRUE	1790000000	ct0	...
```

Lightpanda filters the jar per host itself (checked: a `.example.com` cookie is never offered
to httpbin.org), so unrelated sites cannot mix while same-site subdomains and redirects keep
their session. With an empty textarea the legacy single jar (`Cookie jar path`) is used when
it exists.

```
lightpanda_cookies(action=“set“, cookies=“<the whole cookies.txt>“, merge=true)
lightpanda_cookies(action=“set“, site=“x.com“, cookies=“auth_token=...; ct0=...“)
lightpanda_cookies(action=“status“)     # domains + cookie NAMES only, never values
lightpanda_cookies(action=“verify“, url=“https://x.com/i/lists/123“, expect=“@handle“)
lightpanda_cookies(action=“delete“, site=“x.com“)
lightpanda_cookies(action=“clear“, confirm=true)
```

`set` replaces the dump unless `merge=true`; the same domain+path+name is overwritten. A bare
`name=value; name2=value2` header carries no domain, so pair it with `site`.

**Then fetch** — the dump is attached automatically (`use_cookies` defaults to true):

```
lightpanda_fetch(url=“https://example.com/private“, wait_until=“networkidle“, wait_ms=8000)
lightpanda_fetch(url=“...“, use_cookies=False)    # anonymous
```

The result reports `cookies: {source, domains, names}`, so always check which session was used.

**Hard limits in Lightpanda 1.0.4 — check these before promising a workflow:**

- `lightpanda fetch` **does** apply the jar (HttpOnly included). This is the supported path.
- `lightpanda run` (PandaScript) accepts `--cookie` but **ignores it** — a `new Page()` sees no
  cookies. `lightpanda_run(mode=“cli“)` therefore reports `cookies.applied: false`.
- The MCP server (`lightpanda_read` / `act` / `mcp` on the interactive session) **cannot be
  seeded**: it loads the jar but never applies it to browsing sessions, and there is no
  `setCookies` MCP tool. `evaluate` + `document.cookie` works only for non-HttpOnly cookies.
- So: authenticated reading = `lightpanda_fetch`. Authenticated *interaction* is not available;
  say so instead of retrying.

**Etiquette:** cookies are the user’s credentials for their own account. Never paste them into
chat, commits, or a report. Do not use them to bypass paywalls, CAPTCHAs, or rate limits, and
not to scrape a site in violation of its terms.

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
- Logged-in pages need the cookie dump (or the legacy jar) plus `lightpanda_fetch`; interactive
  MCP sessions are always anonymous in 1.0.4 (see *Authenticated pages*).

## Configuration

Skill settings (Agent → Skills → Lightpanda):

| Setting | Default | Purpose |
|---------|---------|---------|
| Binary path | auto | Explicit `lightpanda` path; auto-detects `$LIGHTPANDA_BIN`, `skills/lightpanda/bin/`, then `PATH` |
| MCP host | `127.0.0.1` | Interface for the auto-started server |
| MCP port | `9223` | Server port; change it if something else owns 9223 |
| Obey robots.txt | off | Politeness default for every fetch |
| Block private networks | on | SSRF guard |
| Cookies (cookies.txt dump) | empty | Pasted Netscape dump; `lightpanda_fetch` sends it, filtered per host |
| Cookie jar path (legacy) | `<state>/cookies.json` | Single jar used only when the dump is empty |

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
