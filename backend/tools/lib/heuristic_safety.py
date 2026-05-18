"""
heuristic_safety — 3-layer safety checking for runpy & bash tools.

Layers:
  1. Pattern Matching (regex) — fast, deterministic
  2. AST Analysis (Python only) — structural code analysis
  3. Semantic Scoring — combine layers, apply modifiers, decide

Output Levels:
  safe (0-3)           → execute normally
  warning (4-7)        → execute + log warning
  requires_approval (8-14) → halt, request user confirmation
  dangerous (15+)      → reject immediately (no override)

Usage:
    from backend.tools.lib.heuristic_safety import check_safety

    result = check_safety(code, tool_type='python')
    # or
    result = check_safety(script, tool_type='bash')
"""

import ast
import logging
import re
from typing import Any

from backend.tools.lib.safety_base import SafetyCheckerBase, CheckResult

logger = logging.getLogger(__name__)


def _compile(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pre-compile regex patterns with IGNORECASE flag."""
    compiled = []
    for p in patterns:
        entry = dict(p)
        entry["compiled"] = re.compile(p["pattern"], re.IGNORECASE)
        compiled.append(entry)
    return compiled


# ============================================================================
# Pattern Libraries
# ============================================================================

# A. Destructive Commands (requires_approval, score 8-14)
DESTRUCTIVE_PATTERNS: list[dict[str, Any]] = [
    # File destruction
    {"pattern": r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*|-[rf][a-zA-Z]*)\s+(?!/tmp/|__pycache__|\.cache|\.DS_Store|\.tox|\.mypy_cache|\.pytest_cache|\.eggs|build/|\.next|\.nuxt)", "weight": 10, "category": "file_destruction", "description": "Destructive file removal command (rm -rf, except safe cleanup targets)"},
    {"pattern": r"\brmdir\s+(-[a-zA-Z]*)?\b", "weight": 8, "category": "directory_destruction", "description": "Directory removal command"},
    {"pattern": r"\bshred\b", "weight": 12, "category": "secure_deletion", "description": "Secure file deletion (shred)"},
    {"pattern": r"\bgit\s+add\s+\.", "weight": 8, "category": "git_staging", "description": "Git add all files (git add .)"},
    {"pattern": r"\bgit\s+add\s+-A\b", "weight": 8, "category": "git_staging", "description": "Git add all files (git add -A)"},
    {"pattern": r"\bgit\s+rebase\b", "weight": 9, "category": "git_history_rewrite", "description": "Git rebase (history rewrite risk)"},
    {"pattern": r"\bgit\s+reset\s+--hard\b", "weight": 10, "category": "git_history_rewrite", "description": "Git reset --hard (data loss risk)"},
    {"pattern": r"\bgit\s+branch\s+-D\b", "weight": 8, "category": "git_branch_deletion", "description": "Git force delete branch (git branch -D)"},
    {"pattern": r"\bgit\b.*\bpush\b.*(--force-with-lease|--force|-f)\b", "weight": 10, "category": "git_history_rewrite", "description": "Git force push (remote history rewrite)"},
    {"pattern": r"\bwipe\b", "weight": 12, "category": "secure_deletion", "description": "Secure file deletion (wipe)"},
    {"pattern": r"\btruncate\s+(-s\s+0|-s\s*0\b)", "weight": 8, "category": "file_truncation", "description": "File truncation command"},
    {"pattern": r">\s*/\w+", "weight": 6, "category": "file_overwrite", "description": "File overwrite via redirection"},
    {"pattern": r">\s*>\s*/\w+", "weight": 6, "category": "file_overwrite", "description": "File overwrite via double redirection"},
    # Disk operations
    {"pattern": r"\bdd\s+if=/dev/", "weight": 12, "category": "disk_overwrite", "description": "Disk overwrite command (dd)"},
    {"pattern": r"\bmkfs\b", "weight": 13, "category": "filesystem_format", "description": "Filesystem format command (mkfs)"},
    # Privilege escalation
    {"pattern": r"\bchmod\s+777\b", "weight": 5, "category": "permission_escalation", "description": "Permission escalation (chmod 777)"},
    {"pattern": r"\bchown\s+root\b", "weight": 8, "category": "privilege_escalation", "description": "Privilege escalation (chown root)"},
    {"pattern": r"\bsudo\s+", "weight": 6, "category": "privilege_escalation", "description": "Privilege escalation (sudo)"},
    # Remote code execution
    {"pattern": r"\bcurl\s+.*\|\s*(ba)?sh\b", "weight": 12, "category": "remote_code_execution", "description": "Remote code execution via pipe (curl | bash)"},
    {"pattern": r"\bwget\s+.*\|\s*(ba)?sh\b", "weight": 12, "category": "remote_code_execution", "description": "Remote code execution via pipe (wget | sh)"},
    # Package manager uninstall/remove operations
    {"pattern": r"\bapt-get\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Package removal via apt-get remove"},
    {"pattern": r"\bapt-get\s+purge\b", "weight": 10, "category": "package_uninstall", "description": "Package purge via apt-get purge"},
    {"pattern": r"\bapt-get\s+--purge\s+remove\b", "weight": 12, "category": "package_uninstall", "description": "Package purge+remove via apt-get --purge remove"},
    {"pattern": r"\bapt\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Package removal via apt remove"},
    {"pattern": r"\bapt\s+purge\b", "weight": 10, "category": "package_uninstall", "description": "Package purge via apt purge"},
    {"pattern": r"\bevonic\s+plugin\s+uninstall\b", "weight": 10, "category": "package_uninstall", "description": "Evonic plugin uninstall"},
    {"pattern": r"\bbrew\s+uninstall\b", "weight": 10, "category": "package_uninstall", "description": "Homebrew package uninstall"},
    {"pattern": r"\bbrew\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Homebrew package removal"},
    {"pattern": r"\bbrew\s+uninstall\s+--force\b", "weight": 12, "category": "package_uninstall", "description": "Homebrew force uninstall"},
    {"pattern": r"\bpip\s+uninstall\b", "weight": 8, "category": "package_uninstall", "description": "Python package uninstall via pip"},
    {"pattern": r"\bnpm\s+uninstall\b", "weight": 8, "category": "package_uninstall", "description": "Node package uninstall via npm"},
    {"pattern": r"\bnpm\s+remove\b", "weight": 8, "category": "package_uninstall", "description": "Node package removal via npm"},
    {"pattern": r"\byum\s+(remove|uninstall)\b", "weight": 10, "category": "package_uninstall", "description": "Package removal via yum remove/uninstall"},
    {"pattern": r"\bdnf\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Package removal via dnf remove"},
    {"pattern": r"\bsnap\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Snap package removal"},
]

# B. Dangerous Commands (auto-reject, score 15+)
# NOTE: Each category should be UNIQUE across all pattern lists.
# DANGEROUS_PATTERNS owns the "heavy" patterns. NETWORK_PATTERNS and
# SENSITIVE_FILE_PATTERNS are ONLY used for Python code when the dangerous
# variant is not present (deduplication is handled in _layer1_pattern_matching).
DANGEROUS_PATTERNS: list[dict[str, Any]] = [
    # Sandbox bypass
    {"pattern": r"\bimport\s+ctypes\b", "weight": 12, "category": "sandbox_bypass", "description": "Import ctypes (sandbox bypass risk)"},
    # Command execution
    {"pattern": r"os\.system\s*\(", "weight": 10, "category": "command_execution", "description": "os.system() call (command execution)"},
    {"pattern": r"os\.popen\s*\(", "weight": 10, "category": "command_execution", "description": "os.popen() call (command execution)"},
    {"pattern": r"\bexec\s*\(", "weight": 8, "category": "code_execution", "description": "exec() call (code execution)"},
    {"pattern": r"\bcompile\s*\(", "weight": 7, "category": "code_execution", "description": "compile() call (code execution)"},
    # Network (heavy patterns)
    {"pattern": r"\bimport\s+socket\b", "weight": 8, "category": "network", "description": "Import socket (network access)"},
    {"pattern": r"\bsocket\.socket\s*\(", "weight": 8, "category": "network", "description": "Socket creation (network access)"},
    # Sensitive file access (heavy patterns)
    {"pattern": r"/etc/shadow", "weight": 10, "category": "sensitive_file", "description": "Access to /etc/shadow (authentication file)"},
    {"pattern": r"/etc/passwd", "weight": 6, "category": "sensitive_file", "description": "Access to /etc/passwd (user file)"},
    {"pattern": r"\.env\b", "weight": 6, "category": "sensitive_file", "description": "Access to .env file"},
    {"pattern": r"credentials", "weight": 8, "category": "sensitive_file", "description": "Reference to credentials"},
    {"pattern": r"private_key", "weight": 8, "category": "sensitive_file", "description": "Reference to private key"},
    # SSH directory protection
    {"pattern": r"(?:^|/|~/)\.ssh(?:/|$)", "weight": 15, "category": "ssh_access", "description": "Access to .ssh directory (SSH keys/config)"},
    {"pattern": r"/home/[^/]+/\.ssh(?:/|$)", "weight": 15, "category": "ssh_access", "description": "Access to /home/<user>/.ssh (SSH keys/config)"},
    {"pattern": r"/root/\.ssh(?:/|$)", "weight": 15, "category": "ssh_access", "description": "Access to /root/.ssh (SSH keys/config)"},
    {"pattern": r"\bid_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?\b", "weight": 12, "category": "ssh_key", "description": "Reference to SSH private/public key file"},
    {"pattern": r"\bauthorized_keys2?\b", "weight": 10, "category": "ssh_key", "description": "Reference to authorized_keys file"},
    {"pattern": r"\bknown_hosts\b", "weight": 8, "category": "ssh_key", "description": "Reference to known_hosts file"},
    # Docker reference (allowed — sandbox is already Docker-isolated)
    {"pattern": r"\bdocker\s+", "weight": 4, "category": "sandbox_escape", "description": "Docker reference (not blocked, sandbox is already isolated)"},
    # Network exploit
    {"pattern": r"(?:^|\s)nc\s+", "weight": 12, "category": "network_exploit", "description": "Netcat command (network exploit)"},
    {"pattern": r"\bnetcat\b", "weight": 12, "category": "network_exploit", "description": "Netcat command (network exploit)"},
    {"pattern": r"reverse\s*shell", "weight": 15, "category": "network_exploit", "description": "Reverse shell pattern"},
    # Obfuscation
    {"pattern": r"base64\s+-d", "weight": 7, "category": "obfuscation", "description": "Base64 decode (obfuscation)"},
    {"pattern": r"base64\s+--decode", "weight": 7, "category": "obfuscation", "description": "Base64 decode (obfuscation)"},
    {"pattern": r"\bexec\s*\(\s*base64", "weight": 10, "category": "obfuscation", "description": "Base64 obfuscated code execution"},
    {"pattern": r"\beval\s*\(\s*base64", "weight": 10, "category": "obfuscation", "description": "Base64 obfuscated code execution"},
]

# C. Network Patterns (score 5-8)
# These are ONLY used for Python code when the dangerous variant is not present.
# Deduplication is handled in _layer1_pattern_matching.
NETWORK_PATTERNS: list[dict[str, Any]] = [
    {"pattern": r"\bimport\s+urllib\b", "weight": 5, "category": "network", "description": "Import urllib (network access)"},
    {"pattern": r"\bimport\s+http\b", "weight": 3, "category": "network", "description": "Import http (network access)"},
    {"pattern": r"\burllib\.request\b", "weight": 5, "category": "network", "description": "urllib request (network access)"},
    {"pattern": r"\brequests\.\w+\s*\(", "weight": 4, "category": "network", "description": "Requests call (network access)"},
    {"pattern": r"\bhttp\.\w+\s*\(", "weight": 3, "category": "network", "description": "HTTP call (network access)"},
]

# D. Sensitive File Patterns (score 6-10)
# These are ONLY used for Python code when the dangerous variant is not present.
# Deduplication is handled in _layer1_pattern_matching.
SENSITIVE_FILE_PATTERNS: list[dict[str, Any]] = [
    {"pattern": r"password", "weight": 6, "category": "password", "description": "Password reference"},
    {"pattern": r"secret", "weight": 6, "category": "secret", "description": "Secret reference"},
]

# E. Bash-specific dangerous patterns
BASH_DANGEROUS_PATTERNS: list[dict[str, Any]] = [
    {"pattern": r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*|-[rf][a-zA-Z]*)\s+(?!/tmp/|__pycache__|\.cache|\.DS_Store|\.tox|\.mypy_cache|\.pytest_cache|\.eggs|build/|\.next|\.nuxt)", "weight": 10, "category": "file_destruction", "description": "Destructive file removal command (rm -rf, except safe cleanup targets)"},
    {"pattern": r"\bdocker\s+", "weight": 4, "category": "sandbox_escape", "description": "Docker reference (not blocked, sandbox is already isolated)"},
    {"pattern": r"(?:^|\s)nc\s+", "weight": 12, "category": "network_exploit", "description": "Netcat command (network exploit)"},
    {"pattern": r"\bnetcat\b", "weight": 12, "category": "network_exploit", "description": "Netcat command (network exploit)"},
    {"pattern": r"reverse\s*shell", "weight": 15, "category": "network_exploit", "description": "Reverse shell pattern"},
    {"pattern": r"\bcurl\s+.*\|\s*(ba)?sh\b", "weight": 12, "category": "remote_code_execution", "description": "Remote code execution via pipe (curl | bash)"},
    {"pattern": r"\bwget\s+.*\|\s*(ba)?sh\b", "weight": 12, "category": "remote_code_execution", "description": "Remote code execution via pipe (wget | sh)"},
    {"pattern": r"\bchmod\s+777\b", "weight": 5, "category": "permission_escalation", "description": "Permission escalation (chmod 777)"},
    {"pattern": r"\bchown\s+root\b", "weight": 8, "category": "privilege_escalation", "description": "Privilege escalation (chown root)"},
    {"pattern": r"\bsudo\s+", "weight": 6, "category": "privilege_escalation", "description": "Privilege escalation (sudo)"},
    {"pattern": r"\bdd\s+if=/dev/", "weight": 12, "category": "disk_overwrite", "description": "Disk overwrite command (dd)"},
    {"pattern": r"\bmkfs\b", "weight": 13, "category": "filesystem_format", "description": "Filesystem format command (mkfs)"},
    {"pattern": r"\bshred\b", "weight": 12, "category": "secure_deletion", "description": "Secure file deletion (shred)"},
    {"pattern": r"\bwipe\b", "weight": 12, "category": "secure_deletion", "description": "Secure file deletion (wipe)"},
    {"pattern": r"\brmdir\b", "weight": 8, "category": "directory_destruction", "description": "Directory removal command (rmdir)"},
    {"pattern": r"\bbase64\s+(-d|--decode)", "weight": 7, "category": "obfuscation", "description": "Base64 decode (obfuscation)"},
    {"pattern": r"\bgit\s+add\s+\.", "weight": 8, "category": "git_staging", "description": "Git add all files (git add .)"},
    {"pattern": r"\bgit\s+add\s+-A\b", "weight": 8, "category": "git_staging", "description": "Git add all files (git add -A)"},
    {"pattern": r"\bgit\s+rebase\b", "weight": 9, "category": "git_history_rewrite", "description": "Git rebase (history rewrite risk)"},
    {"pattern": r"\bgit\s+reset\s+--hard\b", "weight": 10, "category": "git_history_rewrite", "description": "Git reset --hard (data loss risk)"},
    {"pattern": r"\bgit\s+branch\s+-D\b", "weight": 8, "category": "git_branch_deletion", "description": "Git force delete branch (git branch -D)"},
    # Package manager uninstall/remove operations
    {"pattern": r"\bapt-get\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Package removal via apt-get remove"},
    {"pattern": r"\bapt-get\s+purge\b", "weight": 10, "category": "package_uninstall", "description": "Package purge via apt-get purge"},
    {"pattern": r"\bapt-get\s+--purge\s+remove\b", "weight": 12, "category": "package_uninstall", "description": "Package purge+remove via apt-get --purge remove"},
    {"pattern": r"\bapt\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Package removal via apt remove"},
    {"pattern": r"\bapt\s+purge\b", "weight": 10, "category": "package_uninstall", "description": "Package purge via apt purge"},
    {"pattern": r"\bevonic\s+plugin\s+uninstall\b", "weight": 10, "category": "package_uninstall", "description": "Evonic plugin uninstall"},
    {"pattern": r"\bbrew\s+uninstall\b", "weight": 10, "category": "package_uninstall", "description": "Homebrew package uninstall"},
    {"pattern": r"\bbrew\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Homebrew package removal"},
    {"pattern": r"\bbrew\s+uninstall\s+--force\b", "weight": 12, "category": "package_uninstall", "description": "Homebrew force uninstall"},
    {"pattern": r"\bpip\s+uninstall\b", "weight": 8, "category": "package_uninstall", "description": "Python package uninstall via pip"},
    {"pattern": r"\bnpm\s+uninstall\b", "weight": 8, "category": "package_uninstall", "description": "Node package uninstall via npm"},
    {"pattern": r"\bnpm\s+remove\b", "weight": 8, "category": "package_uninstall", "description": "Node package removal via npm"},
    {"pattern": r"\byum\s+(remove|uninstall)\b", "weight": 10, "category": "package_uninstall", "description": "Package removal via yum remove/uninstall"},
    {"pattern": r"\bdnf\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Package removal via dnf remove"},
    {"pattern": r"\bsnap\s+remove\b", "weight": 10, "category": "package_uninstall", "description": "Snap package removal"},
    {"pattern": r"\bgit\b.*\bpush\b.*(--force-with-lease|--force|-f)\b", "weight": 10, "category": "git_history_rewrite", "description": "Git force push (remote history rewrite)"},
    # SSH directory protection
    {"pattern": r"(?:^|/|~/)\.ssh(?:/|$)", "weight": 15, "category": "ssh_access", "description": "Access to .ssh directory (SSH keys/config)"},
    {"pattern": r"/home/[^/]+/\.ssh(?:/|$)", "weight": 15, "category": "ssh_access", "description": "Access to /home/<user>/.ssh (SSH keys/config)"},
    {"pattern": r"/root/\.ssh(?:/|$)", "weight": 15, "category": "ssh_access", "description": "Access to /root/.ssh (SSH keys/config)"},
    {"pattern": r"\bcat\s+.*\.ssh/", "weight": 15, "category": "ssh_access", "description": "Reading .ssh directory contents via cat"},
    {"pattern": r"\bls\s+.*\.ssh", "weight": 15, "category": "ssh_access", "description": "Listing .ssh directory contents"},
    {"pattern": r"\bcp\s+.*\.ssh/", "weight": 15, "category": "ssh_access", "description": "Copying .ssh directory contents"},
    {"pattern": r"\bmv\s+.*\.ssh/", "weight": 15, "category": "ssh_access", "description": "Moving .ssh directory contents"},
    {"pattern": r"\bid_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?\b", "weight": 12, "category": "ssh_key", "description": "Reference to SSH private/public key file"},
    {"pattern": r"\bauthorized_keys2?\b", "weight": 10, "category": "ssh_key", "description": "Reference to authorized_keys file"},
]


# F. SQLite Database Access Patterns (warning-level, score 2-3)
# These are intentionally low-weight — basic SQLite access (SELECT, INSERT, UPDATE)
# should NOT trigger requires_approval.  Only destructive SQL operations (DROP,
# TRUNCATE, DELETE) in SQL_DESTRUCTIVE_PATTERNS below push the score into
# requires_approval/dangerous territory.
SQLITE_ACCESS_PATTERNS: list[dict[str, Any]] = [
    # SQLite command-line tool
    {"pattern": r"\bsqlite3\b", "weight": 3, "category": "sqlite_access", "description": "SQLite3 command-line tool invocation"},
    # Python SQLite imports and calls
    {"pattern": r"\bimport\s+sqlite3\b", "weight": 3, "category": "sqlite_access", "description": "Import sqlite3 module (database access)"},
    {"pattern": r"\bsqlite3\.connect\b", "weight": 3, "category": "sqlite_access", "description": "sqlite3.connect() call (database access)"},
    # Specific database file references (low weight — combined with sqlite_access stays in warning range)
    {"pattern": r"\bchat\.db\b", "weight": 3, "category": "sqlite_db_file", "description": "Access to chat.db (project database)"},
    # Generic database file references (lowest weight — broad match, needs destructive SQL to hit requires_approval)
    {"pattern": r"\b\w+\.db\b", "weight": 2, "category": "sqlite_db_file", "description": "Access to .db database file"},
    {"pattern": r"\b\w+\.sqlite3?\b", "weight": 2, "category": "sqlite_db_file", "description": "Access to .sqlite/.sqlite3 database file"},
]


# G. SQL Destructive Patterns (requires_approval / dangerous, score 10-15)
# These patterns detect destructive SQL operations.  When combined with
# sqlite_access patterns above, the cumulative score triggers approval
# or outright rejection.  Regex is case-insensitive (via _compile).
#
# IMPORTANT: Each pattern requires a SQL-like identifier after the keyword to
# avoid false positives on natural language (e.g. "drop database connection",
# "delete from the list", "truncate the file").  The _SQL_ID helper matches
# unquoted identifiers, double-quoted names, backtick-quoted names, and
# bracket-quoted names.  Common English words are excluded via negative
# lookahead to further reduce false positives.
_SQL_ID = r'(?:[A-Za-z_][\w.]*|["`][^"`]+["`]|\[[^\]]+\])'
_SQL_ENG = (r'(?!the\b|a\b|an\b|to\b|for\b|with\b|from\b|in\b|on\b|at\b|'
            r'by\b|of\b|is\b|was\b|are\b|be\b|it\b|this\b|that\b|and\b|or\b|'
            r'not\b|no\b|connection\b|support\b|list\b|file\b|code\b|output\b|'
            r'concept\b|todo\b|module\b|old\b|unused\b|feature\b|version\b|'
            r'array\b|design\b|slow\b)')

SQL_DESTRUCTIVE_PATTERNS: list[dict[str, Any]] = [
    # Data deletion — require SQL identifier after keyword
    {"pattern": r"\bDROP\s+TABLE\s+" + _SQL_ENG + _SQL_ID, "weight": 12, "category": "sql_destructive", "description": "DROP TABLE - permanently deletes a table"},
    {"pattern": r"\bDROP\s+DATABASE\s+" + _SQL_ENG + _SQL_ID, "weight": 15, "category": "sql_destructive", "description": "DROP DATABASE - permanently deletes entire database"},
    {"pattern": r"\bDROP\s+INDEX\s+" + _SQL_ENG + _SQL_ID, "weight": 10, "category": "sql_destructive", "description": "DROP INDEX - deletes a database index"},
    {"pattern": r"\bDROP\s+VIEW\s+" + _SQL_ENG + _SQL_ID, "weight": 10, "category": "sql_destructive", "description": "DROP VIEW - deletes a database view"},
    {"pattern": r"\bTRUNCATE\s+(?:TABLE\s+)?" + _SQL_ENG + _SQL_ID, "weight": 12, "category": "sql_destructive", "description": "TRUNCATE - removes all rows from a table"},
    {"pattern": r"\bDELETE\s+FROM\s+" + _SQL_ENG + _SQL_ID, "weight": 10, "category": "sql_destructive", "description": "DELETE FROM - deletes rows from a table"},
    {"pattern": r"\bALTER\s+TABLE\s+" + _SQL_ENG + _SQL_ID + r".*\bDROP\b", "weight": 12, "category": "sql_destructive", "description": "ALTER TABLE ... DROP - destructive schema change"},
]

# Pre-compiled regex patterns (module-level, avoids recompiling on each call)
_DESTRUCTIVE_COMPILED = _compile(DESTRUCTIVE_PATTERNS)
_DANGEROUS_COMPILED = _compile(DANGEROUS_PATTERNS)
_NETWORK_COMPILED = _compile(NETWORK_PATTERNS)
_SENSITIVE_FILE_COMPILED = _compile(SENSITIVE_FILE_PATTERNS)
_BASH_DANGEROUS_COMPILED = _compile(BASH_DANGEROUS_PATTERNS)
_SQLITE_COMPILED = _compile(SQLITE_ACCESS_PATTERNS)
_SQL_DESTRUCTIVE_COMPILED = _compile(SQL_DESTRUCTIVE_PATTERNS)


# ============================================================================
# Layer 1: Pattern Matching
# ============================================================================

def _layer1_pattern_matching(code: str, tool_type: str = 'python') -> dict:
    """
    Layer 1: Scan code for dangerous patterns using regex.
    
    Args:
        code: Python code or Bash script to check
        tool_type: 'python' or 'bash'
    
    Returns:
        {
            "matched_patterns": list[dict],
            "total_score": int
        }
    """
    matched_patterns = []
    total_score = 0
    
    # Select pre-compiled patterns based on tool type
    if tool_type == 'bash':
        patterns = _BASH_DANGEROUS_COMPILED + _SQLITE_COMPILED + _SQL_DESTRUCTIVE_COMPILED
    else:
        # Python: combine all pre-compiled pattern lists
        patterns = (
            _DANGEROUS_COMPILED +
            _NETWORK_COMPILED +
            _SENSITIVE_FILE_COMPILED +
            _DESTRUCTIVE_COMPILED +
            _SQLITE_COMPILED +
            _SQL_DESTRUCTIVE_COMPILED
        )
    
    for p in patterns:
        if p["compiled"].search(code):
            matched_patterns.append({
                "pattern": p["pattern"],
                "weight": p["weight"],
                "category": p["category"],
                "description": p["description"],
            })
            total_score += p["weight"]
    
    # Deduplicate by category: keep only the highest-weight match per category
    deduped = _deduplicate_by_category(matched_patterns)
    
    # Recalculate total score after deduplication
    total_score = sum(m["weight"] for m in deduped)
    
    return {
        "matched_patterns": deduped,
        "total_score": total_score,
    }


def _deduplicate_by_category(matched_patterns: list[dict]) -> list[dict]:
    """
    Deduplicate matched patterns by category, keeping only the highest-weight match.
    
    This prevents double-counting when the same concept appears in multiple
    pattern lists (e.g., /etc/shadow in both DANGEROUS_PATTERNS and SENSITIVE_FILE_PATTERNS).
    """
    best_by_category: dict[str, dict] = {}
    
    for m in matched_patterns:
        cat = m["category"]
        if cat not in best_by_category or m["weight"] > best_by_category[cat]["weight"]:
            best_by_category[cat] = m
    
    return list(best_by_category.values())


# ============================================================================
# Layer 2: AST Analysis (Python only)
# ============================================================================

def _layer2_ast_analysis(code: str) -> dict:
    """
    Layer 2: Parse Python code into AST and analyze for dangerous patterns.
    
    Args:
        code: Python code to analyze
    
    Returns:
        {
            "imports": list[str],
            "function_calls": list[dict],
            "total_score": int
        }
    """
    result = {
        "imports": [],
        "function_calls": [],
        "total_score": 0,
    }
    
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # If code can't be parsed, skip AST analysis
        # (Pattern matching will catch most issues anyway)
        return result
    
    # Track imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result["imports"].append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                result["imports"].append(f"{node.module}")
    
    # Track function calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_info = _analyze_call(node)
            if call_info:
                result["function_calls"].append(call_info)
                result["total_score"] += call_info["weight"]
    
    return result


def _analyze_call(node: ast.Call) -> dict | None:
    """
    Analyze a function call node for dangerous patterns.
    
    Returns:
        dict with call info and weight, or None if safe
    """
    # Check for dangerous function names
    if isinstance(node.func, ast.Name):
        func_name = node.func.id
        dangerous_names = {
            "exec": 8,
            "compile": 7,
        }
        if func_name in dangerous_names:
            return {
                "type": "function_call",
                "name": func_name,
                "weight": dangerous_names[func_name],
                "description": f"Call to {func_name}()",
            }
    
    # Check for attribute access (e.g., os.system)
    if isinstance(node.func, ast.Attribute):
        attr_name = node.func.attr
        obj_name = ""
        
        if isinstance(node.func.value, ast.Name):
            obj_name = node.func.value.id
        elif isinstance(node.func.value, ast.Attribute):
            # Nested attribute access (e.g., os.path.join)
            obj_name = _get_attr_chain(node.func.value)
        
        full_call = f"{obj_name}.{attr_name}" if obj_name else attr_name
        
        # Check for dangerous patterns
        dangerous_calls = {
            "os.system": 10,
            "os.popen": 10,
            "socket.socket": 8,
            "socket.create_connection": 8,
        }
        
        if full_call in dangerous_calls:
            return {
                "type": "method_call",
                "name": full_call,
                "weight": dangerous_calls[full_call],
                "description": f"Call to {full_call}()",
            }
    
    return None


def _get_attr_chain(node: ast.Attribute) -> str:
    """Get the full attribute chain (e.g., 'os.path' from 'os.path.join')."""
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        if isinstance(current.value, ast.Name):
            parts.append(current.value.id)
            break
        elif isinstance(current.value, ast.Attribute):
            current = current.value
        else:
            break
    return ".".join(reversed(parts))


# ============================================================================
# Layer 3: Scoring & Decision
# ============================================================================

def _layer3_scoring(
    pattern_results: dict,
    ast_results: dict,
    tool_type: str = 'python'
) -> dict:
    """
    Layer 3: Combine results from all layers, apply modifiers, and decide.
    
    Args:
        pattern_results: Results from Layer 1 (pattern matching)
        ast_results: Results from Layer 2 (AST analysis)
        tool_type: 'python' or 'bash'
    
    Returns:
        {
            "level": "safe" | "warning" | "requires_approval" | "dangerous",
            "score": int,
            "reasons": list[str],
            "blocked_patterns": list[str],
            "requires_approval": bool,
            "approval_info": dict | None
        }
    """
    total_score = pattern_results["total_score"] + ast_results.get("total_score", 0)
    
    # Collect all reasons
    reasons = []
    blocked_patterns = []
    
    # Add pattern matching reasons
    for p in pattern_results["matched_patterns"]:
        reasons.append(p["description"])
        blocked_patterns.append(p["category"])
    
    # Add AST analysis reasons
    for call in ast_results.get("function_calls", []):
        reasons.append(call["description"])
        blocked_patterns.append(call.get("category", "code_execution"))
    
    # Apply modifiers based on context
    total_score = _apply_modifiers(total_score, pattern_results, ast_results, tool_type)
    
    # Determine level
    if total_score >= 15:
        level = "dangerous"
        requires_approval = False
        approval_info = None
    elif total_score >= 8:
        level = "requires_approval"
        requires_approval = True
        approval_info = _generate_approval_info(pattern_results, ast_results, tool_type)
    elif total_score >= 4:
        level = "warning"
        requires_approval = False
        approval_info = None
    else:
        level = "safe"
        requires_approval = False
        approval_info = None
    
    return {
        "level": level,
        "score": total_score,
        "reasons": reasons,
        "blocked_patterns": list(set(blocked_patterns)),
        "requires_approval": requires_approval,
        "approval_info": approval_info,
    }


def _apply_modifiers(
    score: int,
    pattern_results: dict,
    ast_results: dict,
    tool_type: str
) -> int:
    """
    Apply modifiers to the base score based on context.
    
    Modifiers:
        - Multiple dangerous imports: +5
        - Combination of network + command execution: +3
        - Obfuscation patterns: +5
    """
    # Check for multiple dangerous imports
    imports = ast_results.get("imports", [])
    dangerous_imports = [i for i in imports if any(
        d in i for d in ["ctypes", "socket", "os", "sys"]
    )]
    if len(dangerous_imports) >= 2:
        score += 5
    
    # Check for obfuscation
    has_obfuscation = any(
        p["category"] == "obfuscation"
        for p in pattern_results["matched_patterns"]
    )
    if has_obfuscation:
        score += 5
    
    # Check for network + command execution combination
    has_network = any(
        p["category"] in ["network", "network_exploit"]
        for p in pattern_results["matched_patterns"]
    )
    has_command_exec = any(
        p["category"] in ["command_execution", "remote_code_execution"]
        for p in pattern_results["matched_patterns"]
    )
    if has_network and has_command_exec:
        score += 3
    
    return score


def _generate_approval_info(
    pattern_results: dict,
    ast_results: dict,
    tool_type: str
) -> dict:
    """
    Generate approval information for requires_approval cases.
    
    Returns:
        dict with command details, risk level, and description
    """
    # Determine risk level based on categories
    categories = [p["category"] for p in pattern_results["matched_patterns"]]
    
    if "sandbox_escape" in categories or "network_exploit" in categories:
        risk_level = "critical"
        description = "This action poses a critical security risk and may compromise the system."
    elif "sql_destructive" in categories:
        risk_level = "high"
        description = "This action performs destructive SQL operations (DROP, TRUNCATE, DELETE) that may permanently destroy data."
    elif "remote_code_execution" in categories or "secure_deletion" in categories:
        risk_level = "high"
        description = "This action may cause significant damage to the system."
    elif "file_destruction" in categories or "disk_overwrite" in categories:
        risk_level = "high"
        description = "This action may permanently delete or overwrite data."
    elif "git_history_rewrite" in categories or "git_branch_deletion" in categories:
        risk_level = "high"
        description = "This action may permanently alter or destroy version history."
    elif "git_staging" in categories:
        risk_level = "medium"
        description = "This action stages all files which may include unintended changes."
    elif "sqlite_access" in categories or "sqlite_db_file" in categories:
        risk_level = "medium"
        description = "This action accesses local SQLite database files which may contain sensitive data."
    elif "privilege_escalation" in categories or "permission_escalation" in categories:
        risk_level = "medium"
        description = "This action may escalate privileges or change permissions."
    else:
        risk_level = "medium"
        description = "This action requires careful consideration."
    
    return {
        "risk_level": risk_level,
        "description": description,
        "categories": list(set(categories)),
        "pattern_count": len(pattern_results["matched_patterns"]),
    }


# ============================================================================
# HeuristicSafetyChecker — class wrapper implementing SafetyCheckerBase
# ============================================================================

class HeuristicSafetyChecker(SafetyCheckerBase):
    """Built-in (system) safety checker using hardcoded heuristic patterns.

    This wraps the existing 3-layer logic (pattern matching, AST analysis,
    semantic scoring) behind the :class:`SafetyCheckerBase` interface so it
    can participate in the :class:`SafetyPipeline`.

    The ``check()`` method returns a :class:`CheckResult` (partial score +
    reasons).  Final level/threshold logic is handled by the pipeline.
    """

    def check(self, code: str, tool_type: str = 'python', agent_context: dict[str, Any] | None = None) -> CheckResult:
        # Layer 1: Pattern matching
        pattern_results = _layer1_pattern_matching(code, tool_type)

        # Layer 2: AST analysis (Python only)
        ast_results = _layer2_ast_analysis(code) if tool_type == 'python' else {}

        # Aggregate score
        score = pattern_results["total_score"] + ast_results.get("total_score", 0)
        score = _apply_modifiers(score, pattern_results, ast_results, tool_type)

        reasons: list[str] = []
        blocked_patterns: list[str] = []

        for p in pattern_results["matched_patterns"]:
            reasons.append(p["description"])
            blocked_patterns.append(p["category"])

        for call in ast_results.get("function_calls", []):
            reasons.append(call["description"])
            blocked_patterns.append(call.get("category", "code_execution"))

        return CheckResult(
            score=score,
            reasons=reasons,
            matched_patterns=pattern_results["matched_patterns"],
            blocked_patterns=list(set(blocked_patterns)),
        )


# Singleton for use by the pipeline
heuristic_checker = HeuristicSafetyChecker()


# ============================================================================
# Main Entry Point (backward-compatible module-level function)
# ============================================================================

def check_safety(code: str, tool_type: str = 'python') -> dict:
    """
    Check code safety across 3 layers.

    Args:
        code: Python code or Bash script to check
        tool_type: 'python' or 'bash'

    Returns:
        {
            "level": "safe" | "warning" | "requires_approval" | "dangerous",
            "score": int,
            "reasons": list[str],
            "blocked_patterns": list[str],
            "requires_approval": bool,
            "approval_info": dict | None
        }

    Examples:
        >>> check_safety("print(2 + 2)")
        {'level': 'safe', 'score': 0, 'reasons': [], ...}

        >>> check_safety("import ctypes")
        {'level': 'dangerous', 'score': 12, 'reasons': [...], ...}

        >>> check_safety("rm -rf /tmp")
        {'level': 'requires_approval', 'score': 10, 'reasons': [...], ...}
    """
    # Layer 1: Pattern matching
    pattern_results = _layer1_pattern_matching(code, tool_type)

    # Layer 2: AST analysis (Python only)
    ast_results = _layer2_ast_analysis(code) if tool_type == 'python' else {}

    # Layer 3: Scoring & decision
    result = _layer3_scoring(pattern_results, ast_results, tool_type)

    # Log warning when anything suspicious is detected
    if result["level"] != "safe":
        categories = ", ".join(result["blocked_patterns"]) or "-"
        reasons_summary = "; ".join(result["reasons"][:3])
        if len(result["reasons"]) > 3:
            reasons_summary += f" (+ {len(result['reasons']) - 3} more)"
        logger.warning(
            "[heuristic_safety] level=%s score=%d tool=%s categories=[%s] reasons: %s",
            result["level"], result["score"], tool_type, categories, reasons_summary,
        )

    return result
