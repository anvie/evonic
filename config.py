import os
import sys
import logging

# Load .env file — prefer envcrypt (supports encrypted values), fall back to internal loader
_envcrypt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib', 'envcrypt', 'libs', 'python')
sys.path.append(_envcrypt_path)

# Load .env from the config module itself so every entrypoint gets it —
# not just app.py. The CLI (`evonic`), supervisor subprocesses, management
# scripts, and tests all import config without going through app.py, so
# relying on a single load_dotenv() in app.py leaves them with an empty
# environment and falls back to hardcoded defaults (e.g. PORT=8080).
#
# Calling load_dotenv() here is safe even when app.py also calls it:
# the loader defaults to override=False, so a second call is a no-op for
# variables that already exist in os.environ. No duplicate work, no surprises.
_envcrypt_config = os.path.join(os.path.expanduser('~'), '.envcrypt.yaml')
if os.path.exists(_envcrypt_config):
    try:
        import envcrypt
        envcrypt.load(".env")
    except Exception:
        from backend.dotenv_loader import load_dotenv
        load_dotenv()
else:
    from backend.dotenv_loader import load_dotenv
    load_dotenv()

_logger = logging.getLogger(__name__)


def _get_env_bool(name: str, default: bool, invert: bool = False) -> bool:
    """Read a boolean environment variable.

    Args:
        name: Environment variable name.
        default: Default value when env var is not set.
        invert: If True, invert the result (e.g. RTK_NO_COMPRESS=1 means disabled).
    """
    raw = os.getenv(name, "")
    if raw == "":
        result = default
    else:
        result = raw.lower() in ("1", "true", "yes", "on")
    return not result if invert else result


def _get_env_int(name: str, default: int, min_val: int = None, max_val: int = None) -> int:
    """Read an integer environment variable with validation and bounds clamping."""
    try:
        value = int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        _logger.warning("Invalid %s, using default %s", name, default)
        return default
    if min_val is not None and value < min_val:
        _logger.warning("%s=%d below minimum %d, clamping to %d", name, value, min_val, min_val)
        return min_val
    if max_val is not None and value > max_val:
        _logger.warning("%s=%d above maximum %d, clamping to %d", name, value, max_val, max_val)
        return max_val
    return value


def _get_env_float(name: str, default: float, min_val: float = None, max_val: float = None) -> float:
    """Read a float environment variable with validation and bounds clamping."""
    try:
        value = float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        _logger.warning("Invalid %s, using default %s", name, default)
        return default
    if min_val is not None and value < min_val:
        _logger.warning("%s=%f below minimum %f, clamping to %f", name, value, min_val, min_val)
        return min_val
    if max_val is not None and value > max_val:
        _logger.warning("%s=%f above maximum %f, clamping to %f", name, value, max_val, max_val)
        return max_val
    return value

# Two-Pass Extraction Configuration
# PASS 1: LLM generates answer with reasoning
# PASS 2: LLM extracts ONLY the final answer in strict format
TWO_PASS_ENABLED = os.getenv("TWO_PASS_ENABLED", "1") == "1"
TWO_PASS_TEMPERATURE = float(os.getenv("TWO_PASS_TEMPERATURE", "0.0"))

# Task Complexity Classifier
# Default enabled state (can be overridden via system settings UI)
TASK_CLASSIFIER_ENABLED = os.getenv("TASK_CLASSIFIER_ENABLED", "1") == "1"

# Domain Evaluator Configuration
# Override default evaluator for specific domains
# Available types: two_pass, keyword, sql_executor, tool_call
# Example: EVALUATOR_MATH=keyword would use keyword matching for math (not recommended)
EVALUATOR_OVERRIDES = {
    # "math": "keyword",        # Override math to use keyword evaluator
    # "conversation": "two_pass",  # Override conversation to use two-pass
}

def get_evaluator_type(domain: str) -> str:
    """Get configured evaluator type for domain"""
    # Check environment variable first
    env_key = f"EVALUATOR_{domain.upper()}"
    env_value = os.getenv(env_key)
    if env_value:
        return env_value.lower()
    
    # Check config overrides
    if domain in EVALUATOR_OVERRIDES:
        return EVALUATOR_OVERRIDES[domain].lower()
    
    return "default"

# Database paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_ROOT = BASE_DIR
_shared_db_dir = os.path.join(APP_ROOT, "shared", "db")
if not os.path.isdir(_shared_db_dir):
    os.makedirs(_shared_db_dir, exist_ok=True)

DB_PATH = os.path.join(_shared_db_dir, "evonic.db")
TEST_DB_PATH = os.path.join(BASE_DIR, "seed", "test_db.sqlite")

# Flask — SECRET_KEY: auto-generate once and persist to .env if missing.
# The previous manual .env regex scanner (added when load_dotenv() was absent
# from config.py) is gone — load_dotenv() above already makes SECRET_KEY
# visible via os.getenv() for every entrypoint.
_SECRET_KEY_ENV = os.getenv("SECRET_KEY")
if not _SECRET_KEY_ENV:
    import secrets
    import tempfile

    _SECRET_KEY_ENV = secrets.token_urlsafe(48)
    _env_path = os.path.join(BASE_DIR, ".env")

    # Atomic write: update existing .env or create a new one
    if os.path.exists(_env_path):
        with open(_env_path, "r") as _f:
            _lines = _f.readlines()
        if _lines and not _lines[-1].endswith("\n"):
            _lines.append("\n")
        _lines.append(f"SECRET_KEY={_SECRET_KEY_ENV}\n")
        _tmp_fd, _tmp_path = tempfile.mkstemp(dir=os.path.dirname(_env_path), prefix=".env.")
        try:
            with os.fdopen(_tmp_fd, "w") as _f:
                _f.writelines(_lines)
            os.replace(_tmp_path, _env_path)
        except Exception:
            if os.path.exists(_tmp_path):
                os.unlink(_tmp_path)
            raise
    else:
        with open(_env_path, "w") as _f:
            _f.write(f"SECRET_KEY={_SECRET_KEY_ENV}\n")

    os.environ["SECRET_KEY"] = _SECRET_KEY_ENV
    _logger.info("Generated new SECRET_KEY and saved to .env")
    _logger.warning(
        "SECRET_KEY was written to .env. Ensure this file is NOT world-readable "
        "(e.g. chmod 600 .env on Linux) — if leaked, session signing keys and "
        "all encrypted cookies can be forged."
    )

SECRET_KEY = _SECRET_KEY_ENV
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _get_env_int("PORT", 8080, min_val=1, max_val=65535)
DEBUG = os.getenv("DEBUG", "0") == "1"

# External service manager. When unset, Evonic manages its own process.
SERVICE_SYSTEM = os.getenv("SERVICE_SYSTEM", "").strip().lower()
SYSTEMD_SERVICE_NAME = os.getenv("SYSTEMD_SERVICE_NAME", "").strip()

# Cookie security — FORCE_INSECURE_COOKIES=true disables the Secure flag
# for all cookies (session + CSRF). Intended for local development without
# HTTPS. Default False: cookies ALWAYS have Secure=True regardless of DEBUG.
FORCE_INSECURE_COOKIES = os.getenv("FORCE_INSECURE_COOKIES", "0") == "1"
SESSION_COOKIE_SECURE = not FORCE_INSECURE_COOKIES

# Authentication
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")

# Real-time log verbosity
LOG_FULL_THINKING = os.getenv("LOG_FULL_THINKING", "0") == "1"
LOG_FULL_RESPONSE = os.getenv("LOG_FULL_RESPONSE", "0") == "1"

# Raw LLM API call logging (markdown)
LLM_API_LOG_ENABLED = os.getenv("LLM_API_LOG_ENABLED", "0") == "1"
LLM_API_LOG_FILE = os.getenv("LLM_API_LOG_FILE", os.path.join(APP_ROOT, "logs", "llm_api_calls.md"))

# Event stream logging to file
EVENT_LOG_FILE = os.getenv("EVENT_LOG_FILE", os.path.join(APP_ROOT, "logs", "events.log"))

# Docker sandbox configuration (shared by runpy, bash, etc.)
SANDBOX_WORKSPACE = os.getenv("SANDBOX_WORKSPACE", BASE_DIR)
SANDBOX_IDLE_TIMEOUT = _get_env_int("SANDBOX_IDLE_TIMEOUT", 1800, min_val=1, max_val=43200)  # 30 min
SANDBOX_MEMORY_LIMIT = os.getenv("SANDBOX_MEMORY_LIMIT", "512m")
SANDBOX_CPU_LIMIT = os.getenv("SANDBOX_CPU_LIMIT", "1")
SANDBOX_NETWORK = os.getenv("SANDBOX_NETWORK", "bridge")  # 'none' or 'bridge'
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "evonic-sandbox:latest")
SANDBOX_MAX_CONTAINERS = _get_env_int("SANDBOX_MAX_CONTAINERS", 10, min_val=1, max_val=100)
# When True (default), the main agent's Docker sandbox container is keyed by
# agent_id instead of session_id, runs with --restart=unless-stopped, and is
# stopped (not removed) on `evonic stop` so installed packages and state survive
# across sessions. Subagents and explorer sub-workspaces remain session-scoped.
SANDBOX_PERSISTENT_CONTAINER_ENABLED = _get_env_bool(
    "SANDBOX_PERSISTENT_CONTAINER_ENABLED", True)

# Sandbox backend selector: 'docker' (default) or 'bwrap' (Linux-only, bubblewrap).
# 'bwrap' runs each command in a lightweight namespace sandbox (no daemon/image) —
# suited for small VPS deployments without Docker.
SANDBOX_BACKEND = os.getenv("SANDBOX_BACKEND", "docker").strip().lower()
# Opt-in virtual-memory ulimit (MB) for bwrap sandboxes; 0 disables. Hard cgroup
# limits (systemd-run --scope) are a follow-up.
SANDBOX_BWRAP_ULIMIT_V_MB = _get_env_int("SANDBOX_BWRAP_ULIMIT_V_MB", 0, min_val=0, max_val=65536)

# SSH backend configuration (used by sshc tool / SSHBackend)
SSH_DEFAULT_TIMEOUT = _get_env_int("SSH_DEFAULT_TIMEOUT", 30, min_val=1, max_val=3600)   # seconds per command
SSH_IDLE_TIMEOUT = _get_env_int("SSH_IDLE_TIMEOUT", 1800, min_val=1, max_val=43200)       # 30 min idle disconnect

# Tunnel Workplace connector relay (WebSocket server for Evonet)
CONNECTOR_WS_HOST = os.getenv("CONNECTOR_WS_HOST", "0.0.0.0")
CONNECTOR_WS_PORT = _get_env_int("CONNECTOR_WS_PORT", 8081, min_val=1024, max_val=65535)
CONNECTOR_PING_INTERVAL = _get_env_int("CONNECTOR_PING_INTERVAL", 30, min_val=5, max_val=300)
CONNECTOR_PING_TIMEOUT = _get_env_int("CONNECTOR_PING_TIMEOUT", 10, min_val=1, max_val=60)
CONNECTOR_PAIRING_CODE_TTL = _get_env_int("CONNECTOR_PAIRING_CODE_TTL", 300, min_val=60, max_val=3600)  # seconds

AGENT_MAX_TOOL_ITERATIONS = _get_env_int("AGENT_MAX_TOOL_ITERATIONS", 100, min_val=1, max_val=1000)
EVAL_MAX_TOOL_ITERATIONS = _get_env_int("EVAL_MAX_TOOL_ITERATIONS", 30, min_val=1, max_val=500)
AGENT_MAX_TOOL_RESULT_CHARS = _get_env_int("AGENT_MAX_TOOL_RESULT_CHARS", 8000, min_val=1, max_val=1_048_576)
# Maximum time the agent loop waits for each parallel tool, measured from
# submission. Running Python threads cannot be terminated, so expired workers
# are abandoned while pending work is cancelled during non-blocking cleanup.
AGENT_PARALLEL_TOOL_WAIT_TIMEOUT = _get_env_int(
    "AGENT_PARALLEL_TOOL_WAIT_TIMEOUT", 300, min_val=1, max_val=3600)

# Same-turn context projection. Enforced mode sends the validated bounded payload;
# shadow mode emits attribution while sending canonical messages, and off disables
# compaction. Projection failures always fail open to canonical messages.
ACTIVE_CONTEXT_MODE = os.getenv("ACTIVE_CONTEXT_MODE", "enforced").strip().lower()
if ACTIVE_CONTEXT_MODE not in ("off", "shadow", "enforced"):
    _logger.warning("Invalid ACTIVE_CONTEXT_MODE=%r, using off", ACTIVE_CONTEXT_MODE)
    ACTIVE_CONTEXT_MODE = "off"
ACTIVE_CONTEXT_SOFT_TOKENS = _get_env_int(
    "ACTIVE_CONTEXT_SOFT_TOKENS", 12000, min_val=0, max_val=10_000_000)
ACTIVE_CONTEXT_RECENT_GROUPS = _get_env_int(
    "ACTIVE_CONTEXT_RECENT_GROUPS", 2, min_val=0, max_val=100)
ACTIVE_CONTEXT_RECEIPT_MAX_CHARS = _get_env_int(
    "ACTIVE_CONTEXT_RECEIPT_MAX_CHARS", 4000, min_val=128, max_val=100_000)

# RTK token compression — per-agent toggle with env var control
# TOOL_COMPRESSION_ENABLED: True unless RTK_NO_COMPRESS=1 (env var force-disables)
# TOOL_COMPRESSION_VERBOSE: True if RTK_VERBOSE=1 (logs pre/post compression stats)
TOOL_COMPRESSION_ENABLED = _get_env_bool("RTK_NO_COMPRESS", False, invert=True)
TOOL_COMPRESSION_VERBOSE = _get_env_bool("RTK_VERBOSE", False)

# Long-running command guard -- True unless LR_GUARD_DISABLED=1 (env var force-disables)
# When disabled, build/compile commands (make, cargo, npm build, etc.) run directly
# without the tmux/screen wrapper. The DB setting long_running_guard_enabled can also
# override this at runtime via the system settings UI.
LONG_RUNNING_GUARD_ENABLED = _get_env_bool("LR_GUARD_DISABLED", False, invert=True)

# ---- WhatsApp safe outbound delivery (global, system-wide) ----
# These settings apply to ALL agents using WhatsApp channels.
# They can be overridden at runtime via the System Settings UI (app_settings table).

# Master toggle: when disabled (0), all safe-delivery features are bypassed.
WHATSAPP_SAFE_DELIVERY_ENABLED = _get_env_bool("WHATSAPP_SAFE_DELIVERY_ENABLED", True)

# Pooling window in seconds: text chunks arriving within this window are merged
# before sending, reducing bursty multi-message delivery.
WHATSAPP_POOL_WINDOW_SECONDS = _get_env_float("WHATSAPP_POOL_WINDOW_SECONDS", 2.0, min_val=0.1, max_val=30.0)

# Hard minimum interval (seconds) between sends to the same chat, independent of
# the cosmetic typing delay.
WHATSAPP_MIN_SEND_INTERVAL_SECONDS = _get_env_float("WHATSAPP_MIN_SEND_INTERVAL_SECONDS", 2.0, min_val=0.1, max_val=60.0)

# Characters-per-second used to estimate a bounded typing delay from content length.
WHATSAPP_TYPING_CHARS_PER_SECOND = _get_env_float("WHATSAPP_TYPING_CHARS_PER_SECOND", 20.0, min_val=1.0, max_val=100.0)

# Hard upper bound (seconds) on the computed typing delay so users are not left
# waiting indefinitely for long messages.
WHATSAPP_MAX_TYPING_DELAY_SECONDS = _get_env_float("WHATSAPP_MAX_TYPING_DELAY_SECONDS", 15.0, min_val=1.0, max_val=120.0)

# Fraction of the computed delay that is added/subtracted as random jitter to
# break up mechanical timing patterns. 0.15 means ±15% variation.
WHATSAPP_DELAY_JITTER_RATIO = _get_env_float("WHATSAPP_DELAY_JITTER_RATIO", 0.15, min_val=0.0, max_val=0.5)

# Per-channel rolling outbound cap (messages/minute). Exceeding this triggers
# throttling warnings and circuit-breaker behavior.
WHATSAPP_MAX_OUTBOUND_PER_MINUTE = _get_env_int("WHATSAPP_MAX_OUTBOUND_PER_MINUTE", 30, min_val=1, max_val=600)

# Master toggle for WhatsApp-native text formatting (headings→labels, bullets→•,
# [label](url)→"label: url", code blocks→CODE, etc.). When disabled (0), the
# original _strip_markdown is used.
WHATSAPP_NATURAL_FORMATTING_ENABLED = _get_env_bool("WHATSAPP_NATURAL_FORMATTING_ENABLED", True)

# Session archiving on /clear — when enabled, session data (messages, JSONL,
# summaries, state) is saved to shared/db/session_archive.db before clearing.
# Only active when EVONIC_SESSION_ARCHIVE=1 (or true/True/TRUE).
SESSION_ARCHIVE = _get_env_bool("EVONIC_SESSION_ARCHIVE", False)

AGENT_MAX_SUMMARIZE_BATCH = _get_env_int("AGENT_MAX_SUMMARIZE_BATCH", 20, min_val=1, max_val=500)
AGENT_TIMEOUT_RETRIES = _get_env_int("AGENT_TIMEOUT_RETRIES", 2, min_val=0, max_val=20)
AGENT_QUEUE_WORKERS = _get_env_int("AGENT_QUEUE_WORKERS", 5, min_val=1, max_val=32)
AGENT_SIDEBAR_LIMIT = _get_env_int("AGENT_SIDEBAR_LIMIT", 10, min_val=1, max_val=500)

# Stale session injection — inject staleness-awareness note when the previous
# message in a session is older than the threshold. Per-agent setting with
# global fallback via the DB settings system (stale_session_injection_enabled,
# stale_session_threshold_seconds).
STALE_SESSION_INJECTION_ENABLED = _get_env_bool("STALE_SESSION_INJECTION_ENABLED", True)
STALE_SESSION_THRESHOLD_SECONDS = _get_env_int("STALE_SESSION_THRESHOLD_SECONDS", 25200, min_val=1, max_val=604800)

# Long gap injection — inject system context when user hasn't messaged in N+ weeks.
# Separate from STALE_SESSION which checks gap between consecutive messages.
LONG_GAP_WEEKS = _get_env_int("LONG_GAP_WEEKS", 3, min_val=1, max_val=52)

# Background jobs injection — list the session's still-running background
# processes each turn, so the agent does not forget what it started.
BACKGROUND_JOBS_INJECTION_ENABLED = _get_env_bool("BACKGROUND_JOBS_INJECTION_ENABLED", True)

# Thinking budget cap for small reasoning models (tokens per turn).
# Only active when explicitly set per-model via thinking_budget field in DB.
# Models with thinking_budget=0 have no cap (disabled by default).
# This value is used as reference/documentation only — not auto-applied.
THINKING_BUDGET = _get_env_int("THINKING_BUDGET", 4096, min_val=64, max_val=32768)

# Release version (written by supervisor during staging; "dev" in flat-repo mode)
EVONIC_VERSION = "dev"
_version_file = os.path.join(BASE_DIR, "VERSION")
if os.path.exists(_version_file):
    with open(_version_file) as _vf:
        EVONIC_VERSION = _vf.read().strip()
