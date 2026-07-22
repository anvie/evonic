# Evonic Platform Knowledge

This document explains how the Evonic agent platform works internally. Use this knowledge whenever you need to understand or modify the platform itself.

## Architecture Overview

Evonic is an agent orchestration platform. Here is how the key pieces fit together:

### Directory Structure

All agent data lives under the `agents/` directory at the project root. Each agent gets its own subdirectory:

```
agents/
  <agent_id>/
    SYSTEM.md    — the agent's system prompt (rules, persona, workflow)
    kb/          — knowledge base files the agent can read with `read_file` via `/_self/kb/`
    chat.db      — per-agent SQLite database (chat history, memory, summaries)
    sessions/    — JSONL chat logs for streaming/SSE
```

The super agent (first agent created during setup) additionally has access to tools for managing other agents.

### System Prompt

Every agent's system prompt lives in `agents/<agent_id>/SYSTEM.md`. This file is:

- Written once during agent creation (via `create_agent` or `apply_skillset`)
- Can be updated later with the `update_agent` tool
- Loaded at the start of every conversation turn and injected as the system message
- Also stored in the DB (`agents` table, `system_prompt` column)

The super agent's default system prompt lives in `defaults/super_agent_system_prompt.md` and is used as the base template during first-time setup.

### Key Backend Components

- `backend/agent_runtime/runtime.py` — main orchestrator: message queue, worker threads, session lifecycle
- `backend/agent_runtime/llm_loop.py` — the LLM interaction loop (tool calling, response handling)
- `backend/agent_runtime/context.py` — builds the system prompt + tool definitions for each turn
- `backend/agent_runtime/summarizer.py` — conversation summarization and context management
- `backend/agent_runtime/memory_manager.py` — long-term memory extraction and retrieval
- `backend/tools/` — all tool implementations (bash, read_file, write_file, patch, etc.)
- `backend/tools/registry.py` — tool registry; defines built-in tools and their factories
- `backend/channels/` — channel adapters (Telegram, etc.)
- `plugins/` — installable plugins (kanban, plugin_creator, auto_improver, etc.)
- `models/` — database layer: `db.py` (connection), `schema.py` (ORM models), `chat.py`, `chatlog.py` (per-agent SQLite)

### Agent State

Agents have a `set_mode` tool that controls their working mode:
- **plan mode** — write tools (write_file, patch, str_replace) are BLOCKED; agent can only read and plan
- **execute mode** — all tools available; agent can write code and make changes

State handlers (kanban, etc.) are registered via `backend/plugin_manager.py` and managed through the `state` tool using `namespace:action` labels (e.g. `kanban:pick`, `kanban:finish`).

---

## Knowledge Base (KB)

### What It Is

Each agent has a `kb/` directory under `agents/<agent_id>/kb/`. This directory holds markdown or text files that the agent can read at any time using `read_file` with `/_self/kb/` paths.

### How to Read KB Files

Use `read_file` with the `/_self/kb/` path prefix:

```
read_file(file_path="/_self/kb/architecture.md")
```

Rules:
- Always use the `/_self/kb/` prefix for KB files (e.g. `/_self/kb/notes.md`)
- The `/_self/` prefix resolves to the agent's own directory
- `read_file` supports pagination for large files via the `offset` parameter

### Managing KB Files

The super agent can create and update KB files using `write_file` and `read_file`:
- `write_file` — create or overwrite files via `/_self/kb/` paths
- `read_file` — read any file including KB files (supports pagination for large files)

### When to Use KB

KB is ideal for:
- Platform documentation (like this file!)
- Reference material that should persist across conversations
- Pre-loaded knowledge about the agent's role and environment
- Static data the agent needs to consult regularly

KB is NOT for:
- Dynamic conversation state (use the memory system instead)
- User-specific facts that change (use `remember` / `recall`)
- Temporary working data

---

## Built-in Memory System

### What It Does

The memory system stores durable facts about users across conversations. The default
engine is **evomem** — a local knowledge base with hybrid retrieval (lexical + vector)
**and a self-wiring knowledge graph** (entities and typed relationships). It transparently
falls back to SQLite FTS5 keyword search when evomem is unavailable.

It follows an Extract → Deduplicate → Store → Retrieve pipeline:

1. **Extract** — after conversation summarization, an LLM extracts salient facts (names, preferences, decisions, context) AND named entities + relationships (e.g. "Robin works_at Acme").
2. **Deduplicate** — new facts are compared against existing memories; duplicates are skipped, contradictions supersede the old fact (in both FTS5 and evomem).
3. **Store** — facts become structured markdown pages (notes linked to entities via `[[wiki-links]]`); entities and typed edges build the knowledge graph. A dual-write to FTS5 is kept as a safety net.
4. **Retrieve** — at the start of each turn, relevant memories are retrieved using the user's latest message and injected into the context under a `## Memory` heading.

### Tools

**Capture:**
- **`remember`** — pin an important fact for the current session. It is noted instantly (no delay) and stays available to you for the rest of the conversation; the summarizer then persists it to long-term memory and the knowledge graph in the background. You do NOT need to `remember()` routine details — the summarizer captures the conversation on its own.
  - `content`: the fact as a single clear sentence
  - `category`: one of `user_info`, `preference`, `decision`, `context`, `instruction`, `general`
- **`forget_memory`** — delete a stored fact by id (removed from both FTS5 and evomem).

**Retrieve:** one tool, `recall`, with three modes:
- **`recall(query="...")`** — fast keyword lookup of stored facts (default, `mode="fts"`).
- **`recall(query="...", mode="think")`** — brain-layer synthesis: reason over everything known about a topic and surface knowledge gaps. Prefer it for open questions ("what do I know about the user's project?").
- **`recall(query="...", mode="graph")`** — traverse the knowledge graph from an entity to its relationships (employer, companies founded, people advised, etc.); `query` is the entity name. Optional `edge_type` and `hops` steer the traversal.

### Memory Categories

| Category     | Purpose                                          |
|-------------|--------------------------------------------------|
| user_info   | Identity, contact info (name, phone, email)      |
| preference  | Likes/dislikes, communication style, language    |
| decision    | Commitments or choices made by the user          |
| context     | Background about the user's project or situation |
| instruction | Persistent behavioral instructions               |
| general     | Anything else worth remembering                  |

Facts in user-scoped categories (`user_info`, `preference`, `instruction`, `decision`, `context`) are automatically linked to the canonical `User` entity, so they become graph-adjacent and feed `think`.

### When to Use

- User shares their name, phone, or email → `remember(category="user_info")`
- User states a durable preference or interest → `remember(category="preference")` (this feeds the graph; reserve `notes.md` for always-apply communication/style rules)
- User gives persistent instructions ("Always use English") → `remember(category="instruction")`
- User mentions project context → `remember(category="context")`
- Need to reason about a topic across many facts → `recall(query="...", mode="think")`
- Need to follow relationships between entities → `recall(query="...", mode="graph")`
- Memories are auto-injected each turn, so use `recall` (any mode) only when you need MORE than what was already provided.

---

## Workplaces

### What a Workplace Is

A **workplace** is an execution environment where agents run their tools (bash scripts, Python code, file operations). Each agent is assigned to one workplace, which determines *where* the agent's commands actually execute.

### Workplace Types

Evonic supports three workplace types:

| Type     | Description                                                                 |
|----------|-----------------------------------------------------------------------------|
| **local**  | Runs commands directly on the Evonic server (optionally inside a Docker sandbox). |
| **remote** | Runs commands on a remote server via SSH. Requires host, username, and authentication (password or private key). |
| **tunnel**  | Runs commands on a remote device via the **Evonet** connector — a lightweight program that connects outbound to Evonic over WebSocket (JSON-RPC). No inbound firewall rules needed. |

### Key Points

- Multiple agents can share the same workplace (e.g., several agents all working on the same server).
- The `workspace_path` config sets the working directory for all tools executed in that workplace.
- Local workplaces can be sandboxed inside a Docker container for isolation (`sandbox_enabled`).
- Tunnel workplaces connect and disconnect dynamically as the Evonet program starts/stops on the target device.
- Workplace status (`connected` / `disconnected`) is tracked and surfaced via the `WorkplaceManager`.

---

## Agents

### What Is an Agent

An agent is an AI-powered persona with a **system prompt**, a set of **tools**, optional **skills**, and **channel connections** (e.g., Telegram). Each agent:

- Has its own `agents/<agent_id>/` directory with KB files, chat database, and session logs.
- Belongs to one workplace that defines where its tools execute.
- Maintains independent conversation history and long-term memory.
- Can be enabled/disabled, assigned tools, and linked to communication channels.

### Agent Settings

Every agent is configured through a set of properties stored in the `agents` database table:

| Setting                         | Purpose                                                                 |
|---------------------------------|-------------------------------------------------------------------------|
| `name`                          | Display name shown to users                                             |
| `description`                   | Short purpose summary                                                   |
| `system_prompt`                 | Core persona: rules, workflow, communication style                      |
| `model`                         | LLM model override (default: platform default)                          |
| `enabled`                       | Whether the agent processes messages                                    |
| `is_super`                      | Whether this is the super admin agent                                   |
| `vision_enabled`                | Whether the agent can process images                                   |
| `sandbox_enabled`               | Execute tools inside a Docker container (local workplaces only)          |
| `safety_checker_enabled`        | Enable HMADS safety checking on bash/runpy tools                        |
| `agent_messaging_enabled`       | Allow this agent to send messages to other agents                       |
| `disable_parallel_tool_execution` | Force tools to run one at a time instead of in parallel              |
| `disable_turn_prefetch`         | Disable background context prefetching optimizations                    |
| `workspace`                     | Custom workspace directory path                                         |
| `summarize_threshold` / `summarize_tail` / `summarize_prompt` | Conversation summarization tuning           |
| `message_buffer_seconds` / `outbound_buffer_seconds` | Message batching timing                        |
| `send_intermediate_responses`   | Stream partial updates during tool execution                            |
| `inject_agent_id` / `inject_datetime` | Add agent ID / current time to context                              |
| `enable_agent_state`            | Activate plugin state handlers (kanban, etc.)                           |
| `enable_cmp`                    | Enable Context Memory Path — session-path tracking for multi-task sessions |
| `enable_atg`                    | Enable Atomic Task Graph — compile complex tasks into dependency graphs |
| `always_execute`                | Force agent to stay in execute mode — disables plan mode while keeping CMP/ATG |
| `primary_channel_id`            | Primary communication channel for this agent                            |
| `avatar_path`                   | Path to the agent's avatar image                                        |

### Agent Variables

Agents can have **key-value variables** stored in `agent_variables`. These are used by tools and skills for configuration (e.g., SSH credentials, API keys). Variables can be marked as `is_secret` to prevent them from appearing in logs.

All agent variables are automatically injected as environment variables when `bash` and `runpy` tools execute. Agents should reference them using `$VAR_NAME` in bash or `os.environ['VAR_NAME']` in Python — never output literal secret values.

### Agent-Tool & Agent-Skill Assignment

- **Tools**: assigned via the `agent_tools` table. The `assign_tools` function replaces an agent's entire tool set.
- **Skills**: assigned via the `agent_skills` table. Skills are lazy-loaded by the agent using `use_skill`.

---

## Agent-to-Agent Messaging

### Overview

Agents on the Evonic platform can send messages to each other for **delegation, collaboration, and specialist consultation**. This is implemented as a fire-and-forget system: the sender dispatches a message and the target agent processes it independently. When the target finishes its response, the reply is automatically forwarded back to the sender's user session.

### Key Tools

| Tool                     | Purpose                                                                 |
|--------------------------|-------------------------------------------------------------------------|
| `send_agent_message`     | Send a message to another agent. Returns immediately with delivery confirmation. |
| `escalate_to_user`       | While processing an inter-agent task, forward a question back to the human user. |
| `resolve_agent_approval` | Approve or reject a pending tool-call approval from another agent.      |

### How It Works Internally

1. **Sender** calls `send_agent_message(target_agent_id, message)`.
2. The message is tagged as `[AGENT/<sender_name>]` and delivered to a dedicated **inter-agent session** with `external_user_id = "__agent__<sender_id>"`. This keeps inter-agent conversations separate from human-user sessions.
3. **Target agent** processes the message in its own LLM loop — it sees the tagged message just like a user message.
4. When the target agent produces a final answer (end of turn), the response is automatically forwarded to the sender's human session (identified by `report_to_id`). No polling is required.
5. If the target agent needs clarification from the human, it uses `escalate_to_user`.

### Guard Rails

| Limit              | Value  | Description                                          |
|-------------------|--------|------------------------------------------------------|
| Self-messaging     | Blocked | An agent cannot send a message to itself.           |
| Reply-back loops   | Blocked | Target cannot `send_agent_message` back to the sender — it should just end its turn; the reply auto-forwards. |
| Pair rate limit    | 10 / 60s | Max messages per (sender, target) pair per minute. |
| Global rate limit  | 30 / 60s | Max total messages per sender across all targets per minute. |
| Fan-out limit      | 5 / 5s | Max unique targets per 5-second window (prevents broadcast spam in a single LLM turn). |
| Depth limit        | 3 hops  | Max chain depth (A→B→C→stop) to prevent infinite message chains. |

### Enabling/Disabling

Messaging is controlled per-agent via the `agent_messaging_enabled` toggle (default: ON). When disabled, the agent cannot send or receive inter-agent messages.

---

## HMADS (Heuristic Mal-Activity Detection System)

### What It Is

HMADS is Evonic's built-in safety system that **inspects agent code before execution** to prevent malicious or accidentally dangerous operations. It covers two distinct areas:

1. **Code safety checking** — applied to `bash` and `runpy` tools before any script is executed.
2. **File path safety checking** — applied to file operation tools (`read_file`, `write_file`, `patch`, `str_replace`, `bash`).

### Code Safety: 3-Layer Analysis

When an agent calls `bash` or `runpy`, the code undergoes three layers of analysis before execution:

| Layer | Name            | What It Does                                                                 |
|-------|-----------------|------------------------------------------------------------------------------|
| 1     | Pattern Matching | Scans the code with regex for known dangerous patterns (e.g., `rm -rf`, `curl | bash`, `dd if=/dev/`, `docker` commands, SSH key access). |
| 2     | AST Analysis     | Parses Python code into an Abstract Syntax Tree to structurally detect dangerous calls (`os.system()`, `exec()`, `eval()`, `socket.socket()`). |
| 3     | Scoring & Decision | Combines scores from layers 1+2, applies context modifiers, and decides the final safety level. |

Context modifiers increase the score further: multiple dangerous imports (+5), obfuscation patterns (+5), and network + command execution combinations (+3).

### Output Levels

The combined score determines one of four actions:

| Score Range | Level              | Action                                                    |
|------------|--------------------|-----------------------------------------------------------|
| 0 – 3      | **safe**           | Execute normally.                                         |
| 4 – 7      | **warning**        | Execute and log a warning (no user interaction needed).    |
| 8 – 14     | **requires_approval** | Execution is halted; the user must explicitly approve before it proceeds. |
| 15+        | **dangerous**      | Execution is **rejected immediately** — no override possible. |

### File Path Safety: SSH & SQLite Protection

File operation tools (`read_file`, `write_file`, `patch`, `str_replace`) also have safety guards:

- **SSH protection**: Any path targeting `.ssh/` directories, SSH key files (`id_rsa`, `id_ed25519`, etc.), `authorized_keys`, or `known_hosts` is **automatically blocked** to prevent SSH credential exposure. Uses three-layer checking: regex patterns, path component analysis, and canonical path resolution (symlinks).

- **SQLite protection**: Direct access to `.db`, `.sqlite`, `.sqlite3` files is checked and may trigger approval for sensitive database files like `chat.db`.

### Enabling/Disabling

Safety checking is controlled per-agent via the `safety_checker_enabled` toggle (default: ON). When disabled, all safety checks are bypassed. The super agent should only disable this for trusted agents working in fully sandboxed environments.

---

## Self-Update Mechanism

Evonic can update itself to a new version. The update process works as follows:

1. A new release is downloaded or built into the `releases/` directory (e.g. `releases/v0.1.1/`).
2. The **supervisor** links shared directories (`agents/`, `db/`, `logs/`, `kb/`, etc.) from the `shared/` folder into the new release so agent data is preserved across updates — no data is lost.
3. A `current` symlink in the project root is atomically swapped to point to the new release directory, making the switch instant with zero downtime.
4. The server is then restarted to run the new version.

### Update Commands

- **Check for updates**: `./evonic update --check`
- **Apply an update**: `./evonic update`

After a successful update, the server must be restarted for the new version to take effect.
