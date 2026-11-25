"""FastContext skill backend — Grep, Glob tools."""
import os
import glob as _glob


def _auto_correct_path(requested_path: str, workspace: str, path_is_dir: bool = False) -> str:
    """If the requested path doesn't exist, try glob-resolving from workspace root.

    The FastContext model often hallucinates paths (e.g. 'skills/' instead of
    'evonic/skills/'). This fallback searches for a suffix match. Returns the
    original path if nothing is found.
    """
    if os.path.exists(requested_path):
        return requested_path

    if not os.path.isdir(workspace):
        return requested_path

    basename = os.path.basename(requested_path.rstrip(os.sep)) or requested_path.rstrip(os.sep)

    pattern = os.path.join(workspace, '**', basename)
    matches = _glob.glob(pattern, recursive=True)

    if path_is_dir:
        matches = [m for m in matches if os.path.isdir(m)]
    else:
        matches = [m for m in matches if os.path.isfile(m)]

    return sorted(matches)[0] if matches else requested_path
