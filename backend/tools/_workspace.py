"""
_workspace.py — shared workspace path resolution for file tools.
"""
from typing import Optional

import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENTS_DIR = os.path.join(_BASE_DIR, 'agents')
_SHARED_AGENTS_DIR = os.path.join(_BASE_DIR, 'shared', 'agents')
_SELF_PREFIX = '/_self/'


def scratch_dir(agent_id: str) -> str:
    """Per-agent scratchpad for throwaway scripts and temp files.

    A sandbox/container path (tmpfs) — NOT under the workspace mount.  The
    $SCRATCH env var, the sandbox cwd redirect, and sub-agent relative-write
    redirection all resolve here so an agent's scripts and their output stay
    together and never clutter the project workspace.
    """
    return f"/tmp/evonic-temp/{agent_id or 'default'}-scratchpad"


def effective_agent_id(agent: dict) -> str:
    """Return the effective agent ID for /_self/ path resolution.

    Sub-agents don't have their own directory under agents/ — they inherit
    their parent's SYSTEM.md, KB files, tool assignments, and skill
    assignments.  This mirrors context.py:_effective_id().
    """
    agent_id = (agent or {}).get('id', '')
    if (agent or {}).get('is_subagent'):
        return (agent or {}).get('parent_id', agent_id)
    return agent_id


def is_self_path(file_path: str) -> bool:
    """Return True if file_path uses the /_self/ virtual prefix."""
    return bool(file_path) and (file_path.startswith(_SELF_PREFIX) or file_path == '/_self')


def missing_slash_self_hint(file_path: str) -> Optional[str]:
    """Return a hint string when file_path starts with '_self/' without leading slash.

    Small models sometimes drop the leading '/' when constructing /_self/... paths.
    This hint tells them to use the correct prefix.
    """
    if file_path and (file_path.startswith('_self/') or file_path == '_self'):
        return (
            f"If you meant to access an agent directory, "
            f"use the prefix `/_self/` (with a leading slash)."
        )
    return None


def resolve_self_path(agent_id: str, file_path: str) -> Optional[str]:
    """Resolve /_self/... to the agent's local directory on the evonic server.

    Returns the absolute local path, or None if the resolved path escapes
    the agent's directory (path traversal / symlink attack prevention).
    """
    rel = file_path[len(_SELF_PREFIX):] if file_path.startswith(_SELF_PREFIX) else ''

    # /_self/artifacts/ resolves to shared/agents/<id>/artifacts/ (not agents/<id>/artifacts/)
    if rel == 'artifacts' or rel.startswith('artifacts/'):
        base_dir = _SHARED_AGENTS_DIR
        safe_root = os.path.realpath(os.path.join(_SHARED_AGENTS_DIR, agent_id, 'artifacts'))
    else:
        base_dir = _AGENTS_DIR
        safe_root = os.path.realpath(os.path.join(_AGENTS_DIR, agent_id))

    abs_path = os.path.normpath(os.path.join(base_dir, agent_id, rel))
    resolved = os.path.realpath(abs_path)
    if not resolved.startswith(safe_root + os.sep) and resolved != safe_root:
        return None
    return resolved


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings.

    Used for fuzzy path suggestions when an LLM makes a single-char typo
    in a /_self/ path component (e.g. 'jakarta-kemeneu' vs 'jakarta-kemenkeu').
    """
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Insertion, deletion, substitution
            curr_row.append(min(
                curr_row[-1] + 1,          # insert
                prev_row[j + 1] + 1,       # delete
                prev_row[j] + (c1 != c2)   # substitute
            ))
        prev_row = curr_row
    return prev_row[-1]


def list_self_dir(agent_id: str, rel_path: str) -> str:
    """Return a formatted directory listing for a /_self/ path.

    Allows agents to verify what files exist in /_self/kb/ etc. without
    relying on bash ls (which cannot resolve /_self/ virtual paths).
    """
    local_path = resolve_self_path(agent_id, rel_path)
    if not local_path:
        return f"Error: Access denied — path {rel_path}."
    try:
        entries = sorted(os.listdir(local_path))
    except OSError as e:
        return f"Error: Cannot list directory {rel_path}: {e}"

    lines = [f"[Directory listing for {rel_path}]"]
    for entry in entries:
        full = os.path.join(local_path, entry)
        if os.path.isdir(full):
            lines.append(f"  {entry}/")
        else:
            lines.append(f"  {entry}")
    return "\n".join(lines)


def _self_fuzzy_suggestion(agent_id: str, file_path: str) -> Optional[str]:
    """Return a 'Did you mean: X (edit distance: N)?' hint for /_self/ path typos.

    When an LLM hallucinates a typo (e.g. 'jakarta-kemeneu' instead of
    'jakarta-kemenkeu'), this walks up the path hierarchy to find the
    first existing parent, then suggests corrections for the next
    component using Levenshtein distance ≤ 3.
    """
    if not file_path.startswith(_SELF_PREFIX):
        return None
    rel = file_path[len(_SELF_PREFIX):]
    components = rel.split('/')

    # Walk components from right to left, finding the first existing parent.
    for i in range(len(components), 0, -1):
        prefix_path = '/'.join(components[:i])
        parent_self = _SELF_PREFIX + prefix_path
        parent_local = resolve_self_path(agent_id, parent_self)

        if parent_local and os.path.exists(parent_local):
            # Found existing parent. The next component (if any) is the typo.
            if i < len(components):
                typo_component = components[i]
                try:
                    siblings = os.listdir(parent_local)
                except OSError:
                    return None

                best = None
                best_dist = 999
                for sib in siblings:
                    dist = _levenshtein_distance(typo_component, sib)
                    if dist <= 3 and dist < best_dist:
                        best = sib
                        best_dist = dist

                if best:
                    # Reconstruct the suggested full path
                    suggested_components = components[:i] + [best] + components[i+1:]
                    suggested_path = _SELF_PREFIX + '/'.join(suggested_components)
                    return f"{suggested_path} (edit distance: {best_dist})?"

                # If no close sibling, suggest available entries
                if siblings:
                    return (
                        f"Cannot find '{typo_component}' in {parent_self}. "
                        f"Available: {', '.join(sorted(siblings)[:10])}"
                        f"{'...' if len(siblings) > 10 else ''}?"
                    )
                return None
            # Full path exists — no typo
            return None

    return None


def resolve_workspace_path(agent, file_path: str, fallback_workspace: str) -> str:
    """Resolve a file path to an absolute path, honoring the agent's workspace.

    Rules (in priority order):
    1. Preserve an absolute path that is already inside the agent's workspace.
    2. If path is '/workspace' or starts with '/workspace/', strip that virtual
       prefix and join with the agent's workspace (or fallback_workspace). This
       is the runpy-sandbox convention for paths inside a Docker container.
    3. If path is relative (not absolute) and the agent has a workspace set,
       resolve it relative to that workspace.
    4. If the agent has a workspace set, validate that the resolved path stays
       within the workspace boundary using realpath prefix-check. Absolute paths
       outside the workspace are blocked by returning a non-existent path that
       the caller will report as "file not found".
    5. Otherwise return the path unchanged.
    """
    if not file_path:
        return file_path

    # Expand ~ and ~user — bash/shell does this automatically, but Python's
    # os.path.isabs() returns False for tilde-prefixed paths, causing them to
    # be treated as relative and joined with the workspace base (wrong).
    file_path = os.path.expanduser(file_path)

    # Artifacts registry: /workspace/shared/agents/<id>/artifacts ALWAYS maps to
    # the HOST registry BASE_DIR/shared/agents/<id>/artifacts that the web UI,
    # list_artifacts, fetch_artifact and save_artifact serve — independent of
    # the agent's workspace.  Without this, an agent whose workspace differs
    # from BASE_DIR (e.g. workspace=agents/<id>/) would resolve the artifacts
    # path to a workspace-relative COPY that the UI never reads (silent
    # artifact divergence).  Sandbox backends translate this host registry path
    # to the container bind path in resolve_path(); local agents use it as-is.
    if file_path.startswith('/workspace/shared/agents/'):
        eff_id = effective_agent_id(agent)
        tail = file_path[len('/workspace/shared/agents/'):]
        if tail == eff_id or tail.startswith(eff_id + '/'):
            sub = tail[len(eff_id):].lstrip('/')
            if sub == 'artifacts' or sub.startswith('artifacts/'):
                return os.path.join(_SHARED_AGENTS_DIR, eff_id, sub)

    workspace = (agent or {}).get('workspace')
    if workspace and os.path.isabs(file_path):
        try:
            workspace_real = os.path.realpath(workspace)
            path_real = os.path.realpath(file_path)
            if path_real == workspace_real or path_real.startswith(workspace_real + os.sep):
                return file_path
        except (OSError, PermissionError):
            pass

    if file_path == '/workspace' or file_path.startswith('/workspace/'):
        workspace_root = workspace or fallback_workspace
        rel = file_path[len('/workspace'):].lstrip('/')
        workspace_root_abs = os.path.abspath(workspace_root)
        resolved = os.path.join(workspace_root_abs, rel) if rel else workspace_root_abs
        # Boundary check
        workspace = (agent or {}).get('workspace')
        if workspace:
            try:
                workspace_real = os.path.realpath(workspace)
                path_real = os.path.realpath(resolved)
                if not (path_real == workspace_real or
                        path_real.startswith(workspace_real + os.sep)):
                    return file_path  # block: return unresolvable path
            except (OSError, PermissionError):
                pass
        return resolved

    if not os.path.isabs(file_path):
        # Sub-agents are delegated, throwaway tasks — contain their relative-path
        # writes inside the per-agent scratchpad (tmpfs, outside the workspace) so
        # they never clutter the project and stay alongside bash/runpy output.
        # Absolute paths pass through unchanged (mirrors the shell cwd redirect).
        if (agent or {}).get('is_subagent') and not (agent or {}).get('is_explorer'):
            base = scratch_dir((agent or {}).get('id', ''))
            resolved = os.path.normpath(os.path.join(base, file_path))
            # Keep the write inside the scratchpad (block ../ escape).  Uses
            # normpath, not realpath: this is a container path that need not
            # exist on the host, and realpath would rewrite /tmp on some hosts.
            if resolved == base or resolved.startswith(base + os.sep):
                return resolved
            return file_path  # block: return unresolvable path
        workspace = (agent or {}).get('workspace')
        if workspace:
            base = os.path.abspath(workspace)
            resolved = os.path.join(base, file_path)
            # Boundary check for relative path traversal (e.g. ../../etc/passwd)
            try:
                workspace_real = os.path.realpath(workspace)
                path_real = os.path.realpath(resolved)
                if not (path_real == workspace_real or
                        path_real.startswith(workspace_real + os.sep)):
                    return file_path  # block: return unresolvable path
            except (OSError, PermissionError):
                pass
            return resolved

    # Absolute path — if agent has a workspace, block escape
    workspace = (agent or {}).get('workspace')
    if workspace and os.path.isabs(file_path):
        try:
            workspace_real = os.path.realpath(workspace)
            path_real = os.path.realpath(file_path)
            if path_real == workspace_real or path_real.startswith(workspace_real + os.sep):
                return file_path
        except (OSError, PermissionError):
            pass
        return file_path

    return file_path


# Extensions treated as "scratch" — agent-authored one-off scripts / temp output.
_SCRATCH_EXTENSIONS = ('.py', '.sh', '.txt', '.json', '.log', '.tmp', '.md', '.csv')


def root_pollution_nudge(agent, host_path: Optional[str]) -> Optional[str]:
    """Return a corrective message if writing host_path would drop a NEW
    scratch-like file directly into the evonic project root.

    Fires only when the agent's effective workspace IS the project root (the
    super agent, its sub-agents, or any empty-workspace agent).  Never blocks
    edits to existing files, files inside a subdirectory, or non-scratch
    extensions.  This is the only enforcing layer that reaches the super agent,
    whose cwd legitimately stays at the project root.
    """
    if not host_path:
        return None
    workspace = (agent or {}).get('workspace') or _BASE_DIR
    base = os.path.realpath(_BASE_DIR)
    try:
        if os.path.realpath(workspace) != base:
            return None  # agent has its own workspace — already isolated
        parent = os.path.realpath(os.path.dirname(os.path.abspath(host_path)))
    except (OSError, PermissionError):
        return None
    if parent != base:
        return None  # not a direct child of the project root
    if os.path.exists(host_path):
        return None  # editing an existing root file is fine
    _, ext = os.path.splitext(host_path)
    if ext.lower() not in _SCRATCH_EXTENSIONS:
        return None
    name = os.path.basename(host_path)
    return (
        f"Refused: '{name}' would be written to the project root, which must stay clean. "
        f"Write throwaway scripts and temp files to your scratchpad "
        f"({scratch_dir((agent or {}).get('id', ''))}, available as the $SCRATCH env var). "
        f"Durable project scripts belong in scripts/ (migrations in scripts/migrations/). "
        f"Re-issue the write with one of those paths."
    )
