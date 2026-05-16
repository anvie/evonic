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


def _resolve_app_root(base_dir: str) -> str:
    """Return the app root directory.

    In release mode the running code lives under ``<app_root>/releases/<tag>/``.
    Mutable state (``shared/``, ``current`` symlink) lives at the app root, so
    config must resolve to the grandparent in that case. In flat-repo mode the
    app root *is* the directory containing this file.
    """
    parent = os.path.dirname(base_dir)
    if os.path.basename(parent) == "releases":
        return os.path.dirname(parent)
    return base_dir


APP_ROOT = _resolve_app_root(BASE_DIR)
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

SECRET_KEY = _SECRET_KEY_ENV
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _get_env_int("PORT", 8080, min_val=1, max_val=65535)
DEBUG = os.getenv("DEBUG", "0") == "1"

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

# SSH backend configuration (used by sshc tool / SSHBackend)
SSH_DEFAULT_TIMEOUT = _get_env_int("SSH_DEFAULT_TIMEOUT", 30, min_val=1, max_val=3600)   # seconds per command
SSH_IDLE_TIMEOUT = _get_env_int("SSH_IDLE_TIMEOUT", 1800, min_val=1, max_val=43200)       # 30 min idle disconnect

# Cloud Workplace connector relay (WebSocket server for Evonet)
CONNECTOR_WS_HOST = os.getenv("CONNECTOR_WS_HOST", "0.0.0.0")
CONNECTOR_WS_PORT = _get_env_int("CONNECTOR_WS_PORT", 8081, min_val=1024, max_val=65535)
CONNECTOR_PING_INTERVAL = _get_env_int("CONNECTOR_PING_INTERVAL", 30, min_val=5, max_val=300)
CONNECTOR_PING_TIMEOUT = _get_env_int("CONNECTOR_PING_TIMEOUT", 10, min_val=1, max_val=60)
CONNECTOR_PAIRING_CODE_TTL = _get_env_int("CONNECTOR_PAIRING_CODE_TTL", 300, min_val=60, max_val=3600)  # seconds
CONNECTOR_PAIR_MAX_ATTEMPTS = _get_env_int("CONNECTOR_PAIR_MAX_ATTEMPTS", 30, min_val=5, max_val=200)

AGENT_MAX_TOOL_ITERATIONS = _get_env_int("AGENT_MAX_TOOL_ITERATIONS", 100, min_val=1, max_val=1000)
EVAL_MAX_TOOL_ITERATIONS = _get_env_int("EVAL_MAX_TOOL_ITERATIONS", 30, min_val=1, max_val=500)
AGENT_MAX_TOOL_RESULT_CHARS = _get_env_int("AGENT_MAX_TOOL_RESULT_CHARS", 8000, min_val=1, max_val=1_048_576)
AGENT_MAX_SUMMARIZE_BATCH = _get_env_int("AGENT_MAX_SUMMARIZE_BATCH", 20, min_val=1, max_val=500)
AGENT_TIMEOUT_RETRIES = _get_env_int("AGENT_TIMEOUT_RETRIES", 2, min_val=0, max_val=20)
AGENT_QUEUE_WORKERS = _get_env_int("AGENT_QUEUE_WORKERS", 5, min_val=1, max_val=32)

# Release version (written by supervisor during staging; "dev" in flat-repo mode)
EVONIC_VERSION = "dev"
_version_file = os.path.join(BASE_DIR, "VERSION")
if os.path.exists(_version_file):
    with open(_version_file) as _vf:
        EVONIC_VERSION = _vf.read().strip()
