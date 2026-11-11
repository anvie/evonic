# MCP Client

Lets Evonic agents call tools on external [MCP](https://modelcontextprotocol.io) servers, local ones over stdio and remote ones over Streamable HTTP. Ships with a **Claude Code** preset (`claude mcp serve`), which exposes Claude Code's tools (Bash, Read, Write, Edit, Glob, Grep, ...) to your agents.

## How it works

The plugin is fully self-contained: tool definitions live in `tools.json` (declared via `tools_file` in `plugin.json`) and their implementations under `backend/tools/`. It exposes three agent tools:

- **`mcp_list_servers()`** — list the configured, enabled servers (name and transport) so an agent knows which ones are available. Reads config only; nothing is started.
- **`mcp_list_tools(server?, names_only?, filter?, include_schema?)`** — list the tools exposed by one or all configured servers. Compact by default (see below).
- **`mcp_call(server, tool, arguments, timeout_sec?)`** — invoke a tool on a server and return its text output.

Server subprocesses are spawned lazily on first use, shared across agents, kept alive between calls, restarted automatically if they die, and terminated when Evonic exits. HTTP servers work the same way: one session per server, opened on first use and re-opened if the server forgets it.

## Compact tool listings

A server with 30+ tools returns tens of thousands of characters when every raw JSON
`input_schema` is included, and the runtime truncates oversized tool output before
the model sees it — the tail of the tool list then becomes invisible and the agent
starts guessing tool names. `mcp_list_tools` therefore lists compactly by default:

```json
{"server": "nuwaira-deck", "tool_count": 34, "shown": 34,
 "tools": [{"name": "slide_create",
            "description": "Create a slide from a layout",
            "args": "deck_id:string!, layout_id:string!, title:string?"}],
 "hint": "…"}
```

- `args` is a signature: `name:type!` is required, `name:type?` optional,
  `mode[a|b]` lists the allowed values.
- `names_only=true` returns just the tool names — the cheapest way to see what exists.
- `filter="illustration"` returns only the matching tools **with** their full
  `input_schema`. A plain word is matched as a case-insensitive substring of the
  name and description; anything else is tried as a regex.
- `include_schema=true` returns every tool's full schema. This is only sensible for a
  small toolset: when the rendered listing would exceed ~20,000 characters the
  schemas are dropped automatically and a `note` explains how to narrow it.

## Setup

1. Enable the **MCP Client** plugin in the Plugins UI.
2. Review `MCP_SERVERS` in the plugin settings. Format:

   ```json
   {
     "claude-code": {
       "command": "claude",
       "args": ["mcp", "serve"],
       "env": {},
       "cwd": "/path/to/project",
       "enabled": true
     }
   }
   ```

   - `command` (required): executable to spawn.
   - `args`, `env`: extra arguments / environment overrides.
   - `cwd`: working directory for the server. For `claude-code` this is the directory Claude Code operates in — **setting it is strongly recommended**.
   - `enabled`: set `false` to disable a server without deleting its entry.

   A remote server takes a `url` instead of a `command`, plus any `headers` to send (such as its token):

   ```json
   {
     "nuwaira-deck": {
       "url": "https://deck.example/mcp",
       "headers": {"Authorization": "Bearer nwd_..."}
     }
   }
   ```

   Replies may be plain JSON or an SSE stream. Text content only: images in tool results show as `[image content omitted]`.

3. Assign the `mcp_list_servers`, `mcp_list_tools` and `mcp_call` tools to an agent in the agent's tool settings.

## Security

- Agents can only reference servers by *name*; commands, args, env, URLs and headers are admin-configured here and never come from the agent.
- `mcp_call` against `claude-code` executes commands on the host with the Evonic process's user and **bypasses Evonic's bash safety pipeline**. Only assign these tools to trusted agents.
