"""
compressor_registry.py — Central registry for RTK token compressor.

Loads filters from builtin, agent-specific, and project-specific directories,
provides lookup-by-command with LRU caching, and a convenience compress()
method that ties lookup + pipeline together.

Usage:
    from backend.token_compressor.compressor_registry import get_registry

    reg = get_registry()
    compressed = reg.compress("git status", 0, raw_output)

    # Or with agent/project overrides:
    reg = get_registry(agent_id="linus", project_root="/workspace")
"""

from __future__ import annotations

import functools
import logging
import threading
from pathlib import Path
from typing import Optional

from config import TOOL_COMPRESSION_ENABLED, TOOL_COMPRESSION_VERBOSE
from .filter_schema import CompiledFilter, load_filters, load_filter, merge_filters
from .filter_pipeline import compress as run_pipeline

logger = logging.getLogger(__name__)

# Default LRU cache size
_DEFAULT_CACHE_SIZE = 128

# Built-in filters directory (relative to this package)
_BUILTIN_DIR = Path(__file__).resolve().parent / "filters" / "builtin"

# -----------------------------------------------------------------------
# Token counting (approximate, via tiktoken cl100k_base)
# -----------------------------------------------------------------------

_tiktoken_encoding: object | None = None  # tiktoken.Encoding, lazy-loaded


def _get_token_encoding() -> object:
    """Return a shared tiktoken encoding for cl100k_base.

    Lazy-loaded on first call — avoids import cost when compression
    is disabled or tiktoken is not installed.
    """
    global _tiktoken_encoding
    if _tiktoken_encoding is None:
        import tiktoken
        _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
    return _tiktoken_encoding


def _count_tokens(text: str) -> int:
    """Count tokens in *text* using tiktoken cl100k_base.

    Returns 0 if tiktoken is not available (fail-safe).
    Token counts are approximate — tiktoken estimates, not exact.
    """
    try:
        enc = _get_token_encoding()
        return len(enc.encode(text))
    except Exception:
        return 0


# Thread-safe lock for cumulative counters
_counter_lock = threading.Lock()

# Cumulative stats (updated under _counter_lock)
_compression_count: int = 0
_total_pre_tokens: int = 0
_total_post_tokens: int = 0


def _record_compression(pre_tokens: int, post_tokens: int) -> None:
    """Record one compression event in the cumulative counters (thread-safe)."""
    global _compression_count, _total_pre_tokens, _total_post_tokens
    with _counter_lock:
        _compression_count += 1
        _total_pre_tokens += pre_tokens
        _total_post_tokens += post_tokens


def get_gain_stats() -> dict:
    """Return cumulative token savings statistics.

    Returns:
        dict with keys: pre_tokens, post_tokens, savings_pct, compressions.
        savings_pct is 0.0 when no tokens have been counted.
    """
    with _counter_lock:
        pre = _total_pre_tokens
        post = _total_post_tokens
        count = _compression_count

    if pre > 0:
        pct = round((1 - post / pre) * 100, 1)
    else:
        pct = 0.0

    return {
        "pre_tokens": pre,
        "post_tokens": post,
        "savings_pct": pct,
        "compressions": count,
    }


def reset_gain_counters() -> None:
    """Reset all cumulative token counters to zero.

    Called on new session to start fresh tracking.
    """
    global _compression_count, _total_pre_tokens, _total_post_tokens
    with _counter_lock:
        _compression_count = 0
        _total_pre_tokens = 0
        _total_post_tokens = 0


class CompressorRegistry:
    """Central registry that loads, caches, and applies compression filters.

    Thread-safe for reads after initialization.  Not safe for concurrent
    reload() calls — call reload() during setup, not in hot paths.
    """

    def __init__(
        self,
        agent_id: Optional[str] = None,
        project_root: Optional[str | Path] = None,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> None:
        self._agent_id = agent_id
        self._project_root = Path(project_root) if project_root else None
        self._cache_size = cache_size

        # All loaded filters, keyed by command regex pattern string
        self._filters: dict[str, CompiledFilter] = {}

        # LRU lookup cache: command_str -> CompiledFilter | None (None = no match)
        self._lookup_cache: functools._lru_cache_wrapper | None = None

        self._loaded = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Scan helpers
    # ------------------------------------------------------------------

    def _scan_agent_filters(self, agent_id: str) -> dict[str, CompiledFilter]:
        """Scan agent-specific filter overrides from KB directory.

        Looks for TOML files in ``agents/<agent_id>/kb/filters/``.
        Returns a dict keyed by ``command_re.pattern``.  Use
        :func:`merge_filters` to apply these over a built-in filter.
        """
        agent_dir = Path(f"agents/{agent_id}/kb/filters")
        if not agent_dir.is_dir():
            return {}
        return load_filters(agent_dir=str(agent_dir))

    def _scan_project_filters(self, project_root: Path) -> dict[str, CompiledFilter]:
        """Scan project-specific filter overrides from ``.evonic/filters/``.

        Looks for TOML files in ``<project_root>/.evonic/filters/``.
        Returns a dict keyed by ``command_re.pattern``.  Use
        :func:`merge_filters` to apply these over an agent or built-in filter.
        """
        project_dir = project_root / ".evonic" / "filters"
        if not project_dir.is_dir():
            return {}
        return load_filters(project_dir=str(project_dir))

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load all filters from builtin, agent, and project directories.

        Idempotent — subsequent calls are no-ops.  Call reload() to force
        re-scan.

        Merge priority (highest wins):
            1. Project-level  (``.evonic/filters/*.toml``)
            2. Agent-level    (``agents/<id>/kb/filters/*.toml``)
            3. Built-in       (``filters/builtin/*.toml``)

        Filters with the same ``command_re.pattern`` are merged field-by-field:
        a higher-priority filter only overrides the fields it explicitly
        specifies.
        """
        if self._loaded:
            return

        # --- Built-in filters (lowest priority) ---
        self._filters = load_filters(builtin_dir=str(_BUILTIN_DIR))

        # --- Agent-specific filters (medium priority) ---
        if self._agent_id:
            agent_filters = self._scan_agent_filters(self._agent_id)
            for key, agent_filt in agent_filters.items():
                if key in self._filters:
                    self._filters[key] = merge_filters(self._filters[key], agent_filt)
                else:
                    self._filters[key] = agent_filt

        # --- Project-specific filters (highest priority) ---
        if self._project_root:
            project_filters = self._scan_project_filters(self._project_root)
            for key, proj_filt in project_filters.items():
                if key in self._filters:
                    self._filters[key] = merge_filters(self._filters[key], proj_filt)
                else:
                    self._filters[key] = proj_filt

        # --- Build LRU cache ---
        self._build_cache()

        self._loaded = True
        logger.info(
            "CompressorRegistry loaded %d filters (agent=%s, project=%s)",
            len(self._filters),
            self._agent_id or "none",
            self._project_root or "none",
        )

    def reload(self) -> None:
        """Force reload all filters from disk."""
        self._loaded = False
        self._filters.clear()
        self._lookup_cache = None
        self.load()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def lookup(self, command_str: str) -> Optional[CompiledFilter]:
        """Find the first filter whose command_re matches *command_str*.

        Results are LRU-cached — repeated lookups for the same command
        string are O(1).

        Returns None if no filter matches.
        """
        self._ensure_loaded()
        return self._lookup_cache(command_str)

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------

    def compress(
        self,
        command_str: str,
        exit_code: int,
        output: str,
    ) -> str:
        """Look up filter for command_str and run the compression pipeline.

        Args:
            command_str: The command string to match (e.g. "git status").
            exit_code: Exit code of the tool. Non-zero → skip compression.
            output: Raw tool output text.

        Returns:
            Compressed text, or original output if no filter matched or
            on failure.
        """
        # --- Exit code guard ---
        if exit_code != 0:
            return output

        # --- Global disable via RTK_NO_COMPRESS env var ---
        if not TOOL_COMPRESSION_ENABLED:
            return output

        # --- Verbose logging via RTK_VERBOSE env var ---
        verbose = TOOL_COMPRESSION_VERBOSE

        try:
            filt = self.lookup(command_str)
            if filt is None:
                if verbose:
                    logger.debug("RTK: no filter for command %r", command_str)
                return output

            # --- Token counting (approximate, via tiktoken) ---
            pre_tokens = _count_tokens(output)

            result = run_pipeline(output, filt, exit_code)

            post_tokens = _count_tokens(result)

            # --- Record cumulative savings (thread-safe) ---
            if pre_tokens > 0 or post_tokens > 0:
                _record_compression(pre_tokens, post_tokens)

            if verbose:
                savings = (1 - post_tokens / pre_tokens) * 100 if pre_tokens else 0
                logger.debug(
                    "RTK: %r compressed %d → %d tokens (%.0f%%) using %s",
                    command_str,
                    pre_tokens,
                    post_tokens,
                    savings,
                    filt.description or filt.command_re.pattern,
                )

            return result

        except Exception:
            logger.warning(
                "RTK: exception compressing %r — returning original output",
                command_str,
                exc_info=True,
            )
            return output

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def filter_count(self) -> int:
        """Number of loaded filters."""
        return len(self._filters)

    @property
    def loaded(self) -> bool:
        """Whether filters have been loaded."""
        return self._loaded

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _build_cache(self) -> None:
        """Create a new LRU cache for lookups."""

        @functools.lru_cache(maxsize=self._cache_size)
        def _cached_scan(cmd: str):
            """Scan all filters for a matching command. Returns CompiledFilter or None."""
            for filt in self._filters.values():
                if filt.matches_command(cmd):
                    return filt
            return None

        self._lookup_cache = _cached_scan


# -----------------------------------------------------------------------
# Sentinel for "no match found" in cache
# -----------------------------------------------------------------------

class _NoMatchSentinel:
    """Marker object for cache entries representing 'no filter matched'."""
    pass


_NO_MATCH_SENTINEL = _NoMatchSentinel()


# -----------------------------------------------------------------------
# Singleton
# -----------------------------------------------------------------------

_registry: Optional[CompressorRegistry] = None


def get_registry(
    agent_id: Optional[str] = None,
    project_root: Optional[str | Path] = None,
) -> CompressorRegistry:
    """Return a shared CompressorRegistry instance.

    On first call, creates the registry with the given agent_id and
    project_root.  Subsequent calls return the same instance (parameters
    are ignored).

    Use reload() on the returned instance to pick up new agent/project
    context.
    """
    global _registry
    if _registry is None:
        _registry = CompressorRegistry(
            agent_id=agent_id,
            project_root=project_root,
        )
        _registry.load()
    return _registry


def reset_registry() -> None:
    """Clear the singleton (useful for testing)."""
    global _registry
    _registry = None


def is_compression_enabled(agent_id: Optional[str] = None) -> bool:
    """Check whether RTK compression is enabled for the given agent.

    Priority:
    1. RTK_NO_COMPRESS env var — force-disables globally (via TOOL_COMPRESSION_ENABLED)
    2. Per-agent tool_compression_enabled column (default: True)

    Args:
        agent_id: The agent ID to check. If None, only env var is checked.

    Returns:
        True if compression should proceed, False if it should be skipped.
    """
    # Global env var takes highest priority
    if not TOOL_COMPRESSION_ENABLED:
        return False

    # Per-agent toggle: check DB if agent_id provided
    if agent_id:
        try:
            from models.db import db
            agent = db.get_agent(agent_id)
            if agent and not agent.get("tool_compression_enabled", True):
                return False
        except Exception:
            # Fail-open: if DB check fails, don't block compression
            pass

    return True
