# Run TS (`runts`)

Runs a JavaScript/TypeScript snippet with **bun** inside the agent's workplace —
the JS/TS counterpart of `runpy` for Python.

```
runts {"code": "const n: number = 2; console.log(n)"}
   ->  bun -e <code>     cwd = workplace root     {stdout, stderr, exit_code, execution_time}
```

## Why a plugin, and why this shape

These projects are Astro/TS. `bash` is a bad fit for a multi-line snippet:
heredocs and escaped quotes are the exact failure `runpy` exists to avoid for
Python. Bun runs TypeScript natively, so the snippet needs no build step.

`runts` is a plugin tool, not a core tool: it can be versioned, disabled and
iterated without touching the core tool registry or the agent prompt.

One tool, not two: `runts` and `runjs` would be the same invocation. The
description says JavaScript **and** TypeScript, so the model does not keep
asking for the other one.

## What runs where

The execution backend is resolved per session through
`backend/tools/lib/exec_backend.py` (`registry.get_backend`), exactly like
`bash.py` and `runpy.py`:

| Workplace            | Where the snippet runs                          |
|----------------------|-------------------------------------------------|
| Docker sandbox       | the session's sandbox container (`bash -s`)     |
| bwrap                | the bubblewrap sandbox                          |
| SSH / remote         | that machine                                    |
| tunnel (Evonet)      | that device                                     |
| none + sandboxing off| `LocalBackend` — the host, same as bash/runpy   |

The tool never spawns a process itself. A workplace that cannot be reached
raises from the backend registry and `runts` returns that as an error: **a
missing workplace fails loudly and nothing runs on the host.**

The one case that does reach the host is the case that already reaches it for
`bash` and `runpy`: an agent with `sandbox_enabled=0` **and** no workplace
assigned. The backend registry picks `LocalBackend` there by configuration, not
because a workplace went missing; refusing it only in `runts` would make it
behave differently from the two tools it sits beside, so it is left alone
deliberately.

The working directory is the backend's own — the workplace root in every
environment (Docker's container workdir, the local workspace dir, the remote
workspace dir). That is what makes `import { z } from "zod"` resolve against the
project's own `node_modules` instead of a global install.

## Stateless per call

`runpy` promises persistent state per session because it keeps a Docker
container alive. `bun -e` has none: each call is a fresh process, so variables,
module state and timers are gone by the next call.

To keep something, write it into the workspace and read it next call:

```ts
await Bun.write("out.json", JSON.stringify({ ok: true }))
```

`tools.json` says this to the model directly, so it does not plan around state
that will not be there.

## Guarding

The core injection guard (`backend/tools/injection_guard.py`) maps argument
fields per *built-in* tool name, so a plugin tool is outside it by
construction. Two layers close that:

1. `handler.py` registers a guard through the plugin API
   (`register_tool_guard`, as `plugins/workflow_guard/handler.py` does). It
   fails closed when `ENABLED` is off, rejects a missing/blank/non-string
   `code`, and caps the snippet at 120 000 bytes (one argv string is limited to
   `MAX_ARG_STRLEN` = 131 072 bytes by the kernel).
2. The handler runs the code through the same HMADS safety pipeline `bash` and
   `runpy` use, with the bash pattern set (TypeScript is not Python; a snippet
   that shells out or wipes a tree matches the shell rules) and the full agent
   context, so `dangerous` is refused and `requires_approval` goes to the normal
   approval flow. That scan lives in the handler rather than the guard because
   the guard hook only receives the agent id and a slim gate context.

## Getting it in front of an agent

`runts` appears on the Tools page like any plugin tool (namespaced id
`plugin:runts:runts`) and is toggled per agent through the normal tool
assignment. Enable the plugin in Settings → Plugins first; while it is disabled
the tool definition is not offered at all, and if `ENABLED` is turned off the
tool refuses to run.

An agent also needs `bun` on the workplace PATH. If it is installed somewhere
else (e.g. `~/.bun/bin/bun`), set the `BUN_BIN` variable to that absolute path
rather than relying on the shell profile.

## Variables

| Name               | Default | Meaning                                                      |
|--------------------|---------|--------------------------------------------------------------|
| `ENABLED`          | `true`  | Kill switch; off means fail closed, nothing executes.         |
| `TIMEOUT_SECONDS`  | `60`    | Wall-clock limit per call, clamped to 1..300.                 |
| `MAX_OUTPUT_BYTES` | `65536` | Per-stream cap on stdout/stderr returned to the model.        |
| `BUN_BIN`          | `bun`   | The bun executable invoked inside the workplace.              |

Output is also capped at 64 KB per stream by every execution backend; raising
`MAX_OUTPUT_BYTES` above that changes nothing until the backends do
(`ponytail:` lift when a real snippet needs more than 64 KB of output — write to
a file and read it back instead).
