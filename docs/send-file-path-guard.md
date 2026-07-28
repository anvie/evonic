# Per-agent `send_file` path guard

The `send_file_guard` plugin provides an optional per-agent allow-list for file
attachments. It is configured on the agent detail page under **Settings →
Plugin Settings**, in the section described as **“Per-agent settings from
active plugins.”**

Set **Allowed send_file path regex** to a Python regular expression matching
the canonical source path permitted for that agent. For example:

```regex
^/srv/evonic/shared/agents/my-agent/artifacts/
```

An unset or empty value disables the guard and preserves the existing
`send_file` behavior. When configured, the file tool resolves and normalizes
the path before matching it, so relative traversal and symlink aliases cannot
turn a disallowed source into an allowed one. Non-matching paths, malformed
regular expressions, and policy lookup failures are rejected with a generic
error that does not disclose the source path.

The setting is per agent. Agents without a configured regex remain unaffected;
this is deliberately not a platform-wide deny-by-default policy. Workplace
backends must expose a path that can be canonicalized locally for the regex to
be useful; deployments should use a pattern appropriate to their backend path
representation.
