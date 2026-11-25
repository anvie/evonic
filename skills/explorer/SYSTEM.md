## **CRITICAL!**

- You MUST delegate ALL codebase exploration, directory investigation, and multi-file searches to the `Explore()` tool. Never use Grep, read_file, or Glob directly for exploration.
- Direct file tools (Grep, read_file, Glob) are permitted ONLY for truly trivial operations: reading a single known file path, listing one known directory, or finding a specific string in a single known file.
- When in doubt: if answering a question requires more than ONE file operation, use `Explore()`.
- You can run multiple `Explore()` calls in parallel to investigate several paths at once.
- The explorer runs independently and reports back automatically — you do not need to poll it.

## How to use

Call `Explore` with the path to investigate and a required `query`. Use
`context_vars` for additional placeholders.

```json
Explore({"path": "/home/www/web-a", "query": "Please search xxx in /frontend"})
```

With extra context variables:
```json
Explore({"path": "/home/www/web-a", "query": "find the login handler in /backend", "context_vars": {"focus": "security"}})
```

- `path` (required): an existing directory. It becomes the explorer's root —
  the explorer's Grep/read_file/Glob are confined to it. Accepts an absolute host
  path (including outside your workspace), `/workspace` (your own workspace), or
  a path relative to your workspace.
- `query` (required): the question or focus for the exploration. Injected into
  the explorer sub-agent's system prompt.
- `context_vars` (optional): additional flat key→value pairs injected as
  placeholders in the explorer's system prompt.

