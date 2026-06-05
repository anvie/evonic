"""
Workspace path validation to prevent directory traversal and unauthorized filesystem access.

This module provides validation for agent workspace paths to ensure they don't
point to sensitive system directories or use path traversal attacks.
"""
import os
import pathlib
from typing import Optional


# System directories that should never be used as workspaces
DANGEROUS_PATHS_UNIX = {
    '/etc', '/root', '/var', '/usr', '/bin', '/sbin', '/boot', '/sys', '/proc', '/dev'
}

DANGEROUS_PATHS_WINDOWS = {
    'C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)',
    'C:\\ProgramData', 'C:\\System Volume Information'
}

# Combine all dangerous paths
DANGEROUS_PATHS = DANGEROUS_PATHS_UNIX | DANGEROUS_PATHS_WINDOWS


def is_dangerous_path(path: str) -> bool:
    """Check if a path points to or contains a dangerous system directory.
    
    Args:
        path: The filesystem path to validate
        
    Returns:
        True if the path is dangerous, False otherwise
    """
    if not path:
        return False
    
    # Normalize the path to resolve relative components
    try:
        normalized = os.path.normpath(path)
    except (ValueError, TypeError):
        return True  # Invalid path is dangerous
    
    # Check for exact matches with dangerous paths
    for dangerous in DANGEROUS_PATHS:
        # Normalize dangerous path for comparison
        try:
            dangerous_normalized = os.path.normpath(dangerous)
        except (ValueError, TypeError):
            continue
        
        if normalized == dangerous_normalized:
            return True
        # Check if path is inside a dangerous directory
        if normalized.startswith(dangerous_normalized + os.sep):
            return True
        # Check if dangerous path is inside this path (e.g., '/etc/passwd')
        if dangerous_normalized.startswith(normalized + os.sep):
            return True
        
        # Also check against the original (non-normalized) dangerous path
        # This handles cases where Unix paths like /etc don't normalize on Windows
        if normalized == dangerous:
            return True
        # Use both forward and backward slashes for cross-platform compatibility
        if normalized.startswith(dangerous + '/') or normalized.startswith(dangerous + '\\'):
            return True
        if dangerous.startswith(normalized + '/') or dangerous.startswith(normalized + '\\'):
            return True
    
    return False


def has_path_traversal(path: str) -> bool:
    """Check if a path contains path traversal sequences.
    
    Args:
        path: The filesystem path to validate
        
    Returns:
        True if path traversal is detected, False otherwise
    """
    if not path:
        return False
    
    # Check for obvious path traversal patterns
    if '..' in path:
        return True
    
    # Normalize and check if it escapes the intended directory
    try:
        normalized = os.path.normpath(path)
        # If normalization results in going up directories, it's traversal
        if normalized.startswith('..') or '/..' in normalized or '\\..' in normalized:
            return True
    except (ValueError, TypeError):
        return True
    
    return False


def validate_workspace_path(path: Optional[str]) -> tuple[bool, Optional[str]]:
    """Validate a workspace path for security issues.
    
    Args:
        path: The workspace path to validate (can be None or empty)
        
    Returns:
        Tuple of (is_valid, error_message)
        - (True, None) if path is valid
        - (False, error_message) if path is invalid
    """
    # None or empty is allowed (will use default)
    if not path or not path.strip():
        return (True, None)
    
    path = path.strip()
    
    # Check for path traversal
    if has_path_traversal(path):
        return (False, "Workspace path contains path traversal sequences (..)") 
    
    # Check for dangerous system paths
    if is_dangerous_path(path):
        return (False, "Workspace path points to a restricted system directory")
    
    return (True, None)
