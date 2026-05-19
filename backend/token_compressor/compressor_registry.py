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
import os
from pathlib import Path
from typing import Optional

from .filter_schema import CompiledFilter, load_filters, load_filter
from .filter_pipeline import compress as run_pipeline

logger = logging.getLogger(__name__)

# Default LRU cache size
_DEFAULT_CACHE_SIZE = 128

# Built-in filters directory (relative to this package)
_BUILTIN_DIR = Path(__file__).resolve().parent / "filters" / "builtin"


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

        # Total saved token counters (for gain tracking)
        self._total_tokens_before: int = 0
        self._total_tokens_after: int = 0

        self._loaded = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load all filters from builtin, agent, and project directories.

        Idempotent — subsequent calls are no-ops.  Call reload() to force
        re-scan.
        """
        if self._loaded:
            return

        # --- Built-in filters (lowest priority) ---
        self._filters = load_filters(builtin_dir=str(_BUILTIN_DIR))

        # --- Agent-specific filters (medium priority) ---
        if self._agent_id:
            agent_dir = Path(f"agents/{self._agent_id}/kb/filters")
            if agent_dir.is_dir():
                agent_filters = load_filters(agent_dir=str(agent_dir))
                self._filters.update(agent_filters)

        # --- Project-specific filters (highest priority) ---
        if self._project_root:
            project_dir = self._project_root / ".evonic" / "filters"
            if project_dir.is_dir():
                project_filters = load_filters(project_dir=str(project_dir))
                self._filters.update(project_filters)

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

        # --- RTK_NO_COMPRESS env var check ---
        if os.environ.get("RTK_NO_COMPRESS", "") == "1":
            return output

        # --- Verbose logging ---
        verbose = os.environ.get("RTK_VERBOSE", "") == "1"

        try:
            filt = self.lookup(command_str)
            if filt is None:
                if verbose:
                    logger.debug("RTK: no filter for command %r", command_str)
                return output

            if verbose:
                before = len(output)
                after_est = before  # approximate

            result = run_pipeline(output, filt, exit_code)

            if verbose:
                after = len(result)
                logger.debug(
                    "RTK: %r compressed %d → %d chars (%.0f%%) using %s",
                    command_str,
                    before,
                    after,
                    (1 - after / before) * 100 if before else 0,
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
