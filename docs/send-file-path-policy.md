# Core send_file attachment path policy

Agents can restrict `send_file` in **Agent detail → Advanced Settings** with the
optional **Allowed send_file path regex** setting. An empty value preserves the
existing behavior. For ordinary paths, the regex is matched against the
canonical (realpath) path after traversal and symlink resolution. For `/_self/`
requests, expressions that mention `/_self` are also matched against the
original virtual request before resolution; this prevents canonicalization from
bypassing an explicit virtual-path restriction. The canonical-path check still
runs afterward.

The check runs before file metadata or bytes are exposed. Rejections use a
generic error and do not disclose the rejected path or contents. Invalid regex
values fail closed while the policy is enabled.

Plugins that need additional attachment controls can register a generic
`backend.plugin_hooks.register_attachment_policy` hook. A hook receives
`(agent, canonical_path)` and returns `None` to allow or an error dictionary to
reject. The hook runs after canonicalization and before attachment exposure.
