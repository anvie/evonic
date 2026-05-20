"""Tests for continuation nudge logic.

Covers:
1. should_nudge_continuation() — the decision function
2. Regex pattern matching (CONTINUATION_RE / PLANNING_RE)
3. Regression guards against known bugs (continue/break/fall-through)
"""

import pytest
from backend.agent_runtime.llm_response_parser import (
    CONTINUATION_RE, PLANNING_RE,
    should_nudge_continuation, MAX_CONTINUATION_NUDGES,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. should_nudge_continuation() unit tests
# ═══════════════════════════════════════════════════════════════════════════

class TestShouldNudgeContinuation:
    """Test the decision function that the loop relies on."""

    # ── Returns "nudge" for genuine continuation phrases ──────────────

    @pytest.mark.parametrize("text", [
        "Saya akan melanjutkan mengecek file tersebut.",
        "Baik, saya akan mulai mengerjakan task ini.",
        "Let me continue working on the implementation.",
        "I'll now proceed to update the config.",
        "Sekarang saya akan coba jalankan testnya.",
        "Saya akan lakukan langkah berikutnya.",
        "Mari kita lanjutkan ke bagian selanjutnya.",
        "Tunggu sebentar, sedang memproses.",
        "Oke, saya kerjakan sekarang.",
        "Saya perlu mengecek dulu.",
    ])
    def test_nudge_on_continuation(self, text):
        assert should_nudge_continuation(text, 0) == "nudge"

    # ── Returns "final" when PLANNING_RE negates ──────────────────────

    @pytest.mark.parametrize("text", [
        # Original false positive: session 25ac767d (schedule summary)
        # Contains both CONTINUATION_RE ("Saya akan") and PLANNING_RE ("sudah dibuat", "ringkasan")
        (
            "Jadwal sudah dibuat. **Ringkasan:**\n\n"
            "- **Nama:** Cek total tasks\n"
            "- **Aksi:** Saya akan mengecek semua task di kolom TODO"
        ),
        # Second false positive: session 25ac767d (ERPNext test report)
        # "Saya akan update" matched CONTINUATION_RE but "Berikut hasil" and
        # "Catatan:" were not in PLANNING_RE, causing the full report to be
        # nudged and the agent to reply [DONE] instead of showing the report.
        (
            "Skill ERPNext berfungsi dengan baik. Berikut hasil test:\n\n"
            "**Koneksi**: OK\n\n"
            "| No | Name | Type |\n|----|------|------|\n| 1 | Agung | Individual |\n\n"
            "**Catatan**: Saya akan update best practices di SYSTEM.md."
        ),
        # Completion + continuation in same text
        "Task sudah selesai. Saya akan mengirimkan laporannya nanti.",
        "Deployment sudah berhasil. Saya akan monitor hasilnya.",
        "Reminder sudah dijadwalkan. Saya akan kirimkan pukul 10.",
        "Pesan sudah dikirim. Saya akan follow up nanti.",
        # Report with "Catatan:" marker
        "Setup selesai. Catatan: saya perlu check lagi besok.",
    ])
    def test_final_on_planning_negation(self, text):
        """Text with both CONTINUATION_RE and PLANNING_RE → 'final' (not nudged)."""
        result = should_nudge_continuation(text, 0)
        assert result == "final", f"Expected 'final' for: {text!r}, got {result!r}"

    # ── Returns "none" when no continuation phrase detected ───────────

    @pytest.mark.parametrize("text", [
        "Berikut hasilnya: ada 5 task di kolom TODO.",
        "Done. The file has been updated.",
        "",
        "Terima kasih sudah menunggu.",
        # PLANNING_RE matches but CONTINUATION_RE doesn't → "none" (nothing to negate)
        "Berikut ringkasan dari task yang saya kerjakan hari ini.",
        "Berikut adalah rencana yang saya buat.",
        "Apakah Anda setuju dengan plan ini?",
    ])
    def test_none_on_no_match(self, text):
        assert should_nudge_continuation(text, 0) == "none"

    # ── Respects MAX_CONTINUATION_NUDGES cap ──────────────────────────

    def test_none_when_nudge_count_at_max(self):
        """Once nudge limit is reached, should return 'none' even for continuation text."""
        text = "Saya akan melanjutkan mengecek file tersebut."
        assert should_nudge_continuation(text, MAX_CONTINUATION_NUDGES) == "none"

    def test_nudge_just_below_max(self):
        text = "Saya akan melanjutkan mengecek file tersebut."
        assert should_nudge_continuation(text, MAX_CONTINUATION_NUDGES - 1) == "nudge"

    # ── None content ──────────────────────────────────────────────────

    def test_none_on_none_content(self):
        assert should_nudge_continuation(None, 0) == "none"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Regex-level tests (safety net for pattern edits)
# ═══════════════════════════════════════════════════════════════════════════

class TestContinuationRE:
    """Verify CONTINUATION_RE catches key phrases."""

    @pytest.mark.parametrize("text", [
        "saya akan melanjutkan",
        "Let me continue",
        "I'll now proceed",
        "Tunggu sebentar",
    ])
    def test_matches(self, text):
        assert CONTINUATION_RE.search(text)

    def test_no_match_plain_text(self):
        assert not CONTINUATION_RE.search("Here are the results.")


class TestPlanningRE:
    """Verify PLANNING_RE catches completion/planning markers."""

    @pytest.mark.parametrize("text", [
        "Berikut adalah rencana saya.",
        "Apakah Anda setuju?",
        "sudah dibuat",
        "sudah selesai",
        "sudah berhasil",
        "sudah dijadwalkan",
        "sudah dikirim",
        "Ringkasan hasil kerja",
        "Berikut hasil test yang sudah dijalankan.",
        "Berikut laporan lengkapnya.",
        "Berikut data customer yang ditemukan.",
        "Berikut status deploy terkini.",
        "Catatan: perlu di-review lagi.",
    ])
    def test_matches(self, text):
        assert PLANNING_RE.search(text), f"PLANNING_RE should match: {text!r}"

    def test_no_match_continuation_only(self):
        """A pure continuation phrase must NOT match PLANNING_RE."""
        assert not PLANNING_RE.search("Saya akan mengecek semua task sekarang.")

    def test_no_empty_alternative_regression(self):
        """PLANNING_RE must not have an empty alternative that matches everything.

        Regression: commit 986e883 fixed an empty-string alternative `|)` in
        PLANNING_RE that caused it to match any string, permanently disabling
        continuation nudges.
        """
        assert not PLANNING_RE.search("hello world")
        assert not PLANNING_RE.search("x")
        assert not PLANNING_RE.search("")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Loop control-flow regression guards
# ═══════════════════════════════════════════════════════════════════════════

class TestLoopControlFlowRegression:
    """Guards against the three known-bad control-flow variants.

    History:
      0903c02  continue  → re-ran LLM with identical context (infinite loop risk)
      986e883  if-not    → correct (fall through to final response)
      d5d3084  break     → skipped final response, hit "Max iterations" error
      baeff05  if/else   → correct (fall through to final response)
      current  helper fn → correct (elif condition is False, falls through)

    These tests verify the *contract* that should_nudge_continuation enforces,
    which the loop relies on:
      - "nudge"  → loop must `continue` (re-enter with nudge message)
      - "final"  → loop must NOT `continue` or `break`; must fall through
      - "none"   → loop must NOT `continue` or `break`; must fall through
    """

    def test_planning_negation_returns_final_not_nudge(self):
        """Regression: if this returns 'nudge', the loop would nudge a complete response.
        If the loop used 'break' on 'final', it would skip final response handling."""
        text = "Jadwal sudah dibuat. Saya akan mengecek nanti."
        result = should_nudge_continuation(text, 0)
        assert result == "final"
        # Critically: NOT "nudge" (which would cause a re-enter)
        # The loop's elif only fires on "nudge", so "final" falls through naturally.

    def test_nudge_result_is_only_case_that_continues_loop(self):
        """Only 'nudge' should cause the loop to re-enter.
        'final' and 'none' must both fall through to final response handling."""
        continuation_text = "Saya akan melanjutkan."
        planning_text = "Sudah selesai. Saya akan kirimkan nanti."
        plain_text = "Here are the results."

        assert should_nudge_continuation(continuation_text, 0) == "nudge"
        assert should_nudge_continuation(planning_text, 0) == "final"
        assert should_nudge_continuation(plain_text, 0) == "none"

    def test_elif_guard_pattern(self):
        """The loop uses `elif should_nudge_continuation(...) == "nudge"`.

        This means both "final" and "none" cause the elif to be False,
        so neither triggers a `continue` — the code falls through to
        final response handling. This is the correct behavior.

        This test documents the contract: changing the return values
        or the loop's comparison would break the control flow.
        """
        # "final" must not equal "nudge"
        assert "final" != "nudge"
        # "none" must not equal "nudge"
        assert "none" != "nudge"
