# Telegram tool-call notifications

Off by default. Turn it on per channel: **Agents → the agent → Channels → open the Telegram channel → Tool-call notifications**. That writes `show_tool_calls: true` into the channel config (`PUT /api/agents/<agent>/channels/<id>` with the config object) and restarts a running channel, because the listener is registered when the channel starts.

With it on, the Telegram chat gets one compact line per burst of tool calls instead of one message per call:

```
🛠️ Tool: read_file (3x), bash (1x)
```

A `⚠️` replaces the icon when any call in the burst errored, and the same tool called repeatedly collapses to `name (Nx)`.

Two behaviours worth knowing:

- **Coalescing.** Calls are collected for a 5 s window, so a 100-tool loop produces a handful of lines, not 100.
- **Ordering.** The line is held until the reply that is streaming has been flushed to the chat, then sent. It never interleaves with an answer that is still arriving. Stopping the channel drops any line still queued.

The flag is read only at channel start; no environment variable, no new dependency. Existing deployments keep the old behaviour until an operator flips the toggle.
