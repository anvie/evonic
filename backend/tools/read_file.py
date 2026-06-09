"""Real backend implementation for the read_file tool."""

import os

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.tools._workspace import resolve_workspace_path

_MAX_FILE_SIZE = 400 * 1024  # 400 KB
_CHUNK_CHARS = 8000  # max content chars per page (leaves room for header/footer within 8KB LLM limit)


def _is_binary(file_path: str) -> bool:
    """Return True if the file appears to be binary (contains null bytes in the first 8KB)."""
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(8192)
        return b'\x00' in chunk
    except Exception:
        return False


def read_file(file_path: str, offset: int = 1) -> str:
    """
    Reads a text file with optional pagination.

    Constraints:
    - The file must be a text file (not binary).
    - The file size must not exceed 400KB.
    - When the file is large, returns a paginated chunk starting from `offset` (1-based line number).

    Args:
        file_path (str): Path to the file.
        offset (int): 1-based line number to start reading from (default: 1).

    Returns:
        str: Header + formatted lines with line numbers, or a detailed error message.
    """
    if not os.path.exists(file_path):
        return "Error: File not found."

    # Check file size before reading
    file_size = os.path.getsize(file_path)
    if file_size > _MAX_FILE_SIZE:
        size_kb = file_size / 1024
        return (
            f"Error: File size is {size_kb:.1f}KB ({file_size} bytes) which exceeds the 400KB "
            f"limit. Only files up to 400KB (409,600 bytes) can be read with this tool."
        )

    # Reject binary files
    if _is_binary(file_path):
        return (
            "Error: This is a binary file, not a text file. "
            "Only text files (source code, configs, logs, etc.) are supported."
        )

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        if not lines:
            return "(empty file)"

        total_lines = len(lines)
        file_size_kb = file_size / 1024
        filename = os.path.basename(file_path)

        # Clamp offset to valid range
        start_idx = max(0, min(offset - 1, total_lines - 1))

        # Accumulate lines up to _CHUNK_CHARS
        output_lines = []
        chars = 0
        end_idx = start_idx
        for i in range(start_idx, total_lines):
            line_str = f"{i + 1}: {lines[i].rstrip()}"
            if chars + len(line_str) + 1 > _CHUNK_CHARS and output_lines:
                break
            output_lines.append(line_str)
            chars += len(line_str) + 1
            end_idx = i + 1

        shown_start = start_idx + 1
        shown_end = end_idx

        header = f"[File: {filename} | {total_lines} lines | {file_size_kb:.1f}KB | showing lines {shown_start}-{shown_end}]"
        content = "\n".join(output_lines)

        if shown_end < total_lines:
            remaining = total_lines - shown_end
            footer = f"\n[...{remaining} lines remaining. Call read_file with offset={shown_end + 1} to continue.]"
            return f"{header}\n\n{content}{footer}"

        return f"{header}\n\n{content}"

    except PermissionError:
        return "Error: Permission denied — cannot read this file."
    except UnicodeDecodeError:
        return (
            "Error: The file contains characters that are not valid UTF-8 text. "
            "It may be a binary file or use a different text encoding."
        )
    except Exception as e:
        return f"Error: An unexpected error occurred: {str(e)}"


def execute(agent, args: dict) -> dict:
    file_path = args.get("file_path")
    offset = int(args.get("offset", 1))

    # Heuristic safety check: block access to .ssh directory
    if not (agent or {}).get('_skip_safety') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_ssh_path
        ssh_check = check_ssh_path(file_path, agent)
        if ssh_check["blocked"]:
            # Log to security audit
            from backend.security_audit import log_tool_block
            log_tool_block(
                agent_id=(agent or {}).get('id'),
                tool_name='read_file',
                blocked_path=file_path,
                blocked_reason=ssh_check["reason"] or "SSH directory access denied",
                session_id=(agent or {}).get('session_id'),
                details={"offset": offset},
            )
            return ssh_check["error"]

    # Heuristic safety check: require approval for SQLite database access
    if not (agent or {}).get('_skip_safety') and not (agent or {}).get('is_super') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_sqlite_path
        db_check = check_sqlite_path(file_path, agent)
        if db_check["blocked"]:
            # Log to security audit
            from backend.security_audit import log_tool_block
            log_tool_block(
                agent_id=(agent or {}).get('id'),
                tool_name='read_file',
                blocked_path=file_path,
                blocked_reason=db_check["reason"] or "SQLite database access requires approval",
                session_id=(agent or {}).get('session_id'),
                outcome="requires_approval",
                details={"offset": offset, "requires_approval": True},
            )
            return {
                "error": db_check["error"],
                "level": "requires_approval",
                "reasons": [db_check["reason"]],
                "approval_info": {
                    "risk_level": "medium",
                    "description": "Accessing SQLite database files may expose sensitive data.",
                    "file_path": file_path,
                },
            }

    # Heuristic safety check: require approval for sensitive system paths
    if not (agent or {}).get('_skip_safety') and not (agent or {}).get('is_super') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_sensitive_path
        path_check = check_sensitive_path(file_path, agent)
        if path_check["blocked"]:
            # Log to security audit
            from backend.security_audit import log_tool_block
            log_tool_block(
                agent_id=(agent or {}).get('id'),
                tool_name='read_file',
                blocked_path=file_path,
                blocked_reason=path_check["reason"] or "Sensitive system path requires approval",
                session_id=(agent or {}).get('session_id'),
                outcome="requires_approval",
                details={"offset": offset, "requires_approval": True},
            )
            return {
                "error": path_check["error"],
                "level": "requires_approval",
                "reasons": [path_check["reason"]],
                "approval_info": {
                    "risk_level": "medium",
                    "description": "Accessing sensitive system paths may expose critical system data.",
                    "file_path": file_path,
                },
            }

    # Heuristic safety check: require approval for .env files
    if not (agent or {}).get('_skip_safety') and not (agent or {}).get('is_super') and (agent is None or agent.get("safety_checker_enabled", 1)):
        from backend.tools.safety_checker import check_env_path
        env_check = check_env_path(file_path, agent)
        if env_check["blocked"]:
            # Log to security audit
            from backend.security_audit import log_tool_block
            log_tool_block(
                agent_id=(agent or {}).get('id'),
                tool_name='read_file',
                blocked_path=file_path,
                blocked_reason=env_check["reason"] or "Environment file access requires approval",
                session_id=(agent or {}).get('session_id'),
                outcome="requires_approval",
                details={"offset": offset, "requires_approval": True},
            )
            return {
                "error": env_check["error"],
                "level": "requires_approval",
                "reasons": [env_check["reason"]],
                "approval_info": {
                    "risk_level": "medium",
                    "description": "Accessing environment files may expose secrets, API keys, or passwords.",
                    "file_path": file_path,
                },
            }

    # /_self/ path: always route to the agent's local directory on the evonic server.
    # Sub-agents inherit their parent's directory — use effective agent ID.
    from backend.tools._workspace import is_self_path, resolve_self_path
    agent_id = ((agent or {}).get("parent_id") if (agent or {}).get("is_subagent")
                else (agent or {}).get("id", ""))
    if agent_id and is_self_path(file_path):
        local_path = resolve_self_path(agent_id, file_path)
        if not local_path:
            return "Error: Access denied — path escapes agent directory."
        return read_file(local_path, offset=offset)

    # Hint when path starts with _self/ but missing leading slash
    if agent_id and file_path and (file_path.startswith('_self/') or file_path == '_self'):
        return f"Error: File not found: {file_path}. If you meant to access an agent directory, use the prefix `/_self/` (with a leading slash)."
    # /_portal/ path: route through a virtual path mapping to local/SSH/evonet.
    from backend.tools._portal import is_portal_path, resolve_portal_path
    if agent_id and is_portal_path(file_path):
        backend, real_path = resolve_portal_path(agent_id, file_path)
        if backend is None:
            return real_path  # error message
        st = backend.file_stat(real_path)
        if not st.get("exists"):
            return "Error: File not found."
        file_size = st.get("size", 0)
        if file_size > _MAX_FILE_SIZE:
            size_kb = file_size / 1024
            return (
                f"Error: File size is {size_kb:.1f}KB ({file_size} bytes) which exceeds the 400KB "
                f"limit. Only files up to 400KB (409,600 bytes) can be read with this tool."
            )
        if st.get("is_binary"):
            return (
                "Error: This is a binary file, not a text file. "
                "Only text files (source code, configs, logs, etc.) are supported."
            )
        result = backend.read_file(real_path)
        if "error" in result:
            return f"Error: {result['error']}"
        content = result["content"]
        lines = content.split("\n")
        if content.endswith("\n"):
            lines = lines[:-1]
        if not lines and not content:
            return "(empty file)"
        total_lines = len(lines)
        file_size_kb = file_size / 1024
        filename = os.path.basename(file_path)
        start_idx = max(0, min(offset - 1, total_lines - 1))
        output_lines = []
        chars = 0
        end_idx = start_idx
        for i in range(start_idx, total_lines):
            line_str = f"{i + 1}: {lines[i]}"
            if chars + len(line_str) + 1 > _CHUNK_CHARS and output_lines:
                break
            output_lines.append(line_str)
            chars += len(line_str) + 1
            end_idx = i + 1
        shown_start = start_idx + 1
        shown_end = end_idx
        header = f"[File: {filename} | {total_lines} lines | {file_size_kb:.1f}KB | showing lines {shown_start}-{shown_end}]"
        content_block = "\n".join(output_lines)
        if shown_end < total_lines:
            remaining = total_lines - shown_end
            footer = f"\n[...{remaining} lines remaining. Call read_file with offset={shown_end + 1} to continue.]"
            return f"{header}\n\n{content_block}{footer}"
        return f"{header}\n\n{content_block}"

    # When sandbox is enabled or the agent has a workplace, route file I/O
    # through the execution backend (Docker container, SSH remote, etc.).
    sandbox_enabled = (agent or {}).get('sandbox_enabled', 1)
    has_workplace = bool((agent or {}).get('workplace_id'))
    if sandbox_enabled or has_workplace:
        from backend.tools.lib.exec_backend import registry
        session_id = (agent or {}).get('session_id') or 'default'
        backend = registry.get_backend(session_id, agent)

        # Resolve the file path relative to the agent's workspace before
        # sending it to the execution backend (container/SSH).  This ensures
        # that relative paths and bare file names are resolved against the
        # correct workspace directory.
        target_path = resolve_workspace_path(agent, file_path, _WORKSPACE_ROOT)
        # Convert host path to the backend's view (e.g. /workspace for Docker)
        target_path = backend.resolve_path(target_path)
        st = backend.file_stat(target_path)

        if not st.get('exists'):
            return "Error: File not found."

        file_size = st.get('size', 0)
        if file_size > _MAX_FILE_SIZE:
            size_kb = file_size / 1024
            return (
                f"Error: File size is {size_kb:.1f}KB ({file_size} bytes) which exceeds the 400KB "
                f"limit. Only files up to 400KB (409,600 bytes) can be read with this tool."
            )

        if st.get('is_binary'):
            return (
                "Error: This is a binary file, not a text file. "
                "Only text files (source code, configs, logs, etc.) are supported."
            )

        result = backend.read_file(target_path)
        if 'error' in result:
            return f"Error: {result['error']}"

        content = result['content']
        lines = content.split('\n')
        # Preserve trailing newline semantics
        if content.endswith('\n'):
            lines = lines[:-1]  # remove empty last element from split

        if not lines and not content:
            return "(empty file)"

        total_lines = len(lines)
        file_size_kb = file_size / 1024
        filename = os.path.basename(file_path)

        # Clamp offset to valid range
        start_idx = max(0, min(offset - 1, total_lines - 1))

        # Accumulate lines up to _CHUNK_CHARS
        output_lines = []
        chars = 0
        end_idx = start_idx
        for i in range(start_idx, total_lines):
            line_str = f"{i + 1}: {lines[i]}"
            if chars + len(line_str) + 1 > _CHUNK_CHARS and output_lines:
                break
            output_lines.append(line_str)
            chars += len(line_str) + 1
            end_idx = i + 1

        shown_start = start_idx + 1
        shown_end = end_idx

        header = f"[File: {filename} | {total_lines} lines | {file_size_kb:.1f}KB | showing lines {shown_start}-{shown_end}]"
        content_block = "\n".join(output_lines)

        if shown_end < total_lines:
            remaining = total_lines - shown_end
            footer = f"\n[...{remaining} lines remaining. Call read_file with offset={shown_end + 1} to continue.]"
            return f"{header}\n\n{content_block}{footer}"

        return f"{header}\n\n{content_block}"

    # No sandbox — direct host filesystem access (original behavior)
    file_path = resolve_workspace_path(agent, file_path, _WORKSPACE_ROOT)
    return read_file(file_path, offset=offset)


def test_execute():
    # Create a dummy text file for testing
    test_file = "/tmp/evonic_read_file_test_sample.txt"
    with open(test_file, "w") as f:
        for i in range(1, 51):
            f.write(f"This is line number {i}\n")

    print("--- Test 1: Read whole file ---")
    print(read_file(test_file))

    print("\n--- Test 2: Non-existent file ---")
    print(read_file("ghost_file.txt"))

    print("\n--- Test 3: Binary file ---")
    binary_file = "/tmp/evonic_read_file_test_binary.bin"
    with open(binary_file, "wb") as f:
        f.write(b"\x00\x01\x02\x03binary content")
    print(read_file(binary_file))

    print("\n--- Test 4: File too large (>400KB) ---")
    large_file = "/tmp/evonic_read_file_test_large.txt"
    with open(large_file, "w") as f:
        f.write("x" * (401 * 1024))
    print(read_file(large_file))

    # Cleanup
    for path in [test_file, binary_file, large_file]:
        if os.path.exists(path):
            os.remove(path)
