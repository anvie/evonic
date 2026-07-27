"""Unit and Git-format compatibility tests for ``backend/tools/patch.py``.

The model-facing ``execute()`` contract requires a complete, single-file text
patch in ``git diff`` format.  The low-level Python helpers remain independently
testable with hunk-only input.
"""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

_patch_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'backend', 'tools', 'patch.py')
_spec = importlib.util.spec_from_file_location('patch_tool', _patch_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

apply_patch = _mod.apply_patch
apply_hunks = _mod.apply_hunks
parse_hunks = _mod.parse_hunks
_find_hunk_pos = _mod._find_hunk_pos
_find_first_anchor = _mod._find_first_anchor
execute = _mod.execute
validate_git_patch_format = _mod.validate_git_patch_format


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_file(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8')
    f.write(content)
    f.close()
    return f.name


def read_file(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. Basic apply operations (apply_patch — binary or Python)
# ---------------------------------------------------------------------------

class TestBasicApply:
    def test_replace_line(self):
        p = make_file('line one\nline two\nline three\n')
        r = apply_patch(p, '@@ -1,3 +1,3 @@\n line one\n-line two\n+line TWO\n line three\n')
        assert r['result'] == 'success'
        assert read_file(p) == 'line one\nline TWO\nline three\n'
        os.unlink(p)

    def test_insert_lines(self):
        p = make_file('alpha\nbeta\ngamma\n')
        r = apply_patch(p, '@@ -1,2 +1,4 @@\n alpha\n+inserted1\n+inserted2\n beta\n')
        assert r['result'] == 'success'
        assert read_file(p) == 'alpha\ninserted1\ninserted2\nbeta\ngamma\n'
        os.unlink(p)

    def test_delete_line(self):
        p = make_file('keep1\nremove_me\nkeep2\n')
        r = apply_patch(p, '@@ -1,3 +1,2 @@\n keep1\n-remove_me\n keep2\n')
        assert r['result'] == 'success'
        assert read_file(p) == 'keep1\nkeep2\n'
        os.unlink(p)

    def test_multi_hunk(self):
        p = make_file('a\nb\nc\nd\ne\nf\n')
        patch = '@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -5,2 +5,2 @@\n e\n-f\n+F\n'
        r = apply_patch(p, patch)
        assert r['result'] == 'success'
        assert read_file(p) == 'a\nB\nc\nd\ne\nF\n'
        os.unlink(p)

    def test_hunks_applied_count(self):
        p = make_file('x\ny\nz\n')
        patch = '@@ -1,1 +1,1 @@\n-x\n+X\n@@ -3,1 +3,1 @@\n-z\n+Z\n'
        r = apply_patch(p, patch)
        assert r['hunks_applied'] == 2
        os.unlink(p)

    def test_delete_all_lines(self):
        p = make_file('a\nb\nc\n')
        r = apply_patch(p, '@@ -1,3 +1,0 @@\n-a\n-b\n-c\n')
        assert r['result'] == 'success'
        assert read_file(p) == ''
        os.unlink(p)

    def test_git_diff_headers_ignored(self):
        p = make_file('foo\nbar\n')
        patch = (
            'diff --git a/file.py b/file.py\n'
            'index abc..def 100644\n'
            '--- a/file.py\n'
            '+++ b/file.py\n'
            '@@ -1,2 +1,2 @@\n'
            ' foo\n'
            '-bar\n'
            '+BAR\n'
        )
        r = apply_patch(p, patch)
        assert r['result'] == 'success'
        assert read_file(p) == 'foo\nBAR\n'
        os.unlink(p)

    def test_hunk_count_implicit_1(self):
        p = make_file('only line\n')
        r = apply_patch(p, '@@ -1 +1 @@\n-only line\n+ONLY LINE\n')
        assert r['result'] == 'success'
        os.unlink(p)

    def test_create_new_file(self):
        path = tempfile.mktemp(suffix='.txt')
        r = apply_patch(path, '@@ -0,0 +1,3 @@\n+first\n+second\n+third\n')
        assert r['result'] == 'success'
        assert read_file(path) == 'first\nsecond\nthird\n'
        os.unlink(path)

    def test_insert_at_end(self):
        p = make_file('line1\nline2\n')
        r = apply_patch(p, '@@ -2,1 +2,2 @@\n line2\n+line3\n')
        assert r['result'] == 'success'
        assert read_file(p) == 'line1\nline2\nline3\n'
        os.unlink(p)


# ---------------------------------------------------------------------------
# 2. Python fallback: insertion-only hunks (apply_hunks directly)
# ---------------------------------------------------------------------------

class TestInsertionOnly:
    """Insertion-only hunks have old_count=0 — no context lines to match."""

    def test_insert_at_start(self):
        p = make_file('line1\nline2\n')
        r = apply_hunks(p, '@@ -0,0 +1,2 @@\n+new1\n+new2\n')
        assert r['result'] == 'success', r
        assert read_file(p) == 'new1\nnew2\nline1\nline2\n'
        os.unlink(p)

    def test_insert_in_middle(self):
        p = make_file('line1\nline2\nline3\n')
        r = apply_hunks(p, '@@ -1,0 +2,2 @@\n+new_a\n+new_b\n')
        assert r['result'] == 'success', r
        assert read_file(p) == 'line1\nnew_a\nnew_b\nline2\nline3\n'
        os.unlink(p)

    def test_insert_at_end_of_file(self):
        p = make_file('a\nb\n')
        r = apply_hunks(p, '@@ -2,0 +3,1 @@\n+c\n')
        assert r['result'] == 'success', r
        assert read_file(p) == 'a\nb\nc\n'
        os.unlink(p)

    def test_insert_into_empty_file(self):
        p = make_file('')
        r = apply_hunks(p, '@@ -0,0 +1,2 @@\n+hello\n+world\n')
        assert r['result'] == 'success', r
        assert read_file(p) == 'hello\nworld\n'
        os.unlink(p)

    def test_multiple_insertion_hunks(self):
        """Two insertion-only hunks applied sequentially."""
        p = make_file('a\nb\nc\n')
        patch = '@@ -0,0 +1,1 @@\n+BEFORE\n@@ -3,0 +4,1 @@\n+AFTER\n'
        r = apply_hunks(p, patch)
        assert r['result'] == 'success', r
        content = read_file(p)
        assert 'BEFORE' in content
        assert 'AFTER' in content
        os.unlink(p)


# ---------------------------------------------------------------------------
# 3. Python fallback: line-number drift tolerance (apply_hunks directly)
# ---------------------------------------------------------------------------

class TestDriftTolerance:
    def test_drift_within_window(self):
        """Patch says line 1 but content is at line 45 (drift=44) — within ±50."""
        lines = [f'filler_{i}\n' for i in range(44)]
        lines += ['target line\n', 'after target\n']
        p = make_file(''.join(lines))
        r = apply_hunks(p, '@@ -1,2 +1,2 @@\n target line\n-after target\n+REPLACED\n')
        assert r['result'] == 'success', r
        assert 'REPLACED' in read_file(p)
        os.unlink(p)

    def test_drift_negative_direction(self):
        """Patch says line 50 but content is at line 10 (drift=-40)."""
        lines = [f'line_{i}\n' for i in range(9)]
        lines += ['special line\n', 'next line\n']
        p = make_file(''.join(lines))
        r = apply_hunks(p, '@@ -50,2 +50,2 @@\n special line\n-next line\n+REPLACED\n')
        assert r['result'] == 'success', r
        assert 'REPLACED' in read_file(p)
        os.unlink(p)

    def test_drift_beyond_window_uses_full_scan(self):
        """Drift > 50 is handled by full-file scan (tier 3)."""
        lines = [f'filler_{i}\n' for i in range(60)]
        lines.append('target\n')
        p = make_file(''.join(lines))
        hunks = parse_hunks('@@ -1,1 +1,1 @@\n target\n')
        file_lines = open(p).readlines()
        pos, _ = _find_hunk_pos(file_lines, hunks[0]['lines'], 0, fuzzy=True)
        assert pos == 60  # Found via full-file scan
        os.unlink(p)


# ---------------------------------------------------------------------------
# 4. Python fallback: CRLF and encoding edge cases (apply_hunks directly)
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_crlf_preserved(self):
        """CRLF line endings must be preserved after patching."""
        p = tempfile.mktemp(suffix='.txt')
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write('line1\r\nline2\r\nline3\r\n')
        r = apply_hunks(p, '@@ -2,1 +2,1 @@\n-line2\n+LINE2\n')
        assert r['result'] == 'success', r
        raw = open(p, 'rb').read()
        assert b'\r\n' in raw, 'CRLF endings must be preserved'
        os.unlink(p)

    def test_no_trailing_newline(self):
        p = make_file('a\nb\nc')  # no trailing newline
        r = apply_hunks(p, '@@ -2,1 +2,1 @@\n-b\n+B\n')
        assert r['result'] == 'success', r
        os.unlink(p)

    def test_large_file_patch_last_line(self):
        lines = [f'line{i}\n' for i in range(200)] + ['last line\n']
        p = make_file(''.join(lines))
        r = apply_patch(p, '@@ -201,1 +201,1 @@\n-last line\n+LAST LINE\n')
        assert r['result'] == 'success', r
        assert read_file(p).endswith('LAST LINE\n')
        os.unlink(p)


# ---------------------------------------------------------------------------
# 5. Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    def test_file_not_found(self):
        r = apply_patch('/nonexistent/path/file.txt', '@@ -1,1 +1,1 @@\n-x\n+y\n')
        assert 'error' in r

    def test_no_hunks(self):
        p = make_file('hello\n')
        r = apply_patch(p, 'this is not a patch')
        assert 'error' in r
        os.unlink(p)

    def test_context_mismatch(self):
        """Binary is lenient with fuzz; test via Python fallback which is strict."""
        p = make_file('line1\nline2\nline3\n')
        r = apply_hunks(p, '@@ -1,2 +1,2 @@\n WRONG_CONTEXT\n-line2\n+LINE2\n')
        assert 'error' in r
        os.unlink(p)

    def test_missing_file_path_arg(self):
        from backend.tools.patch import execute
        r = execute({}, {'patch': '@@ -1 +1 @@\n-x\n+y\n'})
        assert 'error' in r

    def test_missing_patch_arg(self):
        from backend.tools.patch import execute
        r = execute({}, {'file_path': '/tmp/x.txt'})
        assert 'error' in r

    def test_python_fallback_wrong_context(self):
        """Python fallback returns error on wrong context."""
        p = make_file('a\nb\nc\n')
        r = apply_hunks(p, '@@ -1,1 +1,1 @@\n NONEXISTENT\n')
        assert 'error' in r
        os.unlink(p)


# ---------------------------------------------------------------------------
# 6. Internal function unit tests
# ---------------------------------------------------------------------------

class TestInternals:
    def test_find_first_anchor_found(self):
        lines = ['foo\n', 'bar\n', 'TARGET = 1\n', 'baz\n']
        hunks = parse_hunks('@@ -10,1 +10,1 @@\n TARGET = 1\n')
        pos = _find_first_anchor(lines, hunks[0]['lines'])
        assert pos == 2

    def test_find_first_anchor_not_found(self):
        lines = ['foo\n', 'bar\n']
        hunks = parse_hunks('@@ -1,1 +1,1 @@\n NONEXISTENT\n')
        pos = _find_first_anchor(lines, hunks[0]['lines'])
        assert pos == -1

    def test_parse_hunks_multiple(self):
        patch = '@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -10,2 +10,2 @@\n x\n-y\n+Y\n'
        hunks = parse_hunks(patch)
        assert len(hunks) == 2
        assert hunks[0]['old_start'] == 1
        assert hunks[1]['old_start'] == 10

    def test_find_hunk_pos_insertion_only(self):
        """Insertion-only hunk (no context) trusts stated position."""
        lines = ['a\n', 'b\n', 'c\n']
        hunks = parse_hunks('@@ -2,0 +2,1 @@\n+inserted\n')
        pos, _ = _find_hunk_pos(lines, hunks[0]['lines'], 1, fuzzy=True)
        assert pos == 1

    def test_find_hunk_pos_exact_match(self):
        lines = ['a\n', 'b\n', 'c\n']
        hunks = parse_hunks('@@ -2,1 +2,1 @@\n b\n')
        pos, _ = _find_hunk_pos(lines, hunks[0]['lines'], 1, fuzzy=True)
        assert pos == 1

    def test_find_hunk_pos_drift(self):
        """Content found 3 lines away from stated position."""
        lines = ['x\n', 'y\n', 'z\n', 'TARGET\n', 'after\n']
        hunks = parse_hunks('@@ -1,2 +1,2 @@\n TARGET\n-after\n+AFTER\n')
        pos, _ = _find_hunk_pos(lines, hunks[0]['lines'], 0, fuzzy=True)
        assert pos == 3

    def test_find_hunk_pos_lines_with_endings(self):
        """Works with readlines() output (lines have \\n endings)."""
        p = make_file('alpha\nbeta\ngamma\n')
        file_lines = open(p).readlines()
        hunks = parse_hunks('@@ -2,1 +2,1 @@\n beta\n')
        pos, _ = _find_hunk_pos(file_lines, hunks[0]['lines'], 1, fuzzy=True)
        assert pos == 1
        os.unlink(p)

    def test_find_hunk_pos_indent_tolerant(self):
        """Indent-tolerant matching when exact match fails."""
        lines = ['    foo\n', '    bar\n']
        hunks = parse_hunks('@@ -1,2 +1,2 @@\n   foo\n-  bar\n+  BAR\n')
        pos, _ = _find_hunk_pos(lines, hunks[0]['lines'], 0, fuzzy=True)
        assert pos == 0

    def test_find_hunk_pos_full_file_scan(self):
        """Full-file scan when content is outside ±50 window."""
        lines = [f'filler_{i}\n' for i in range(60)] + ['target\n']
        hunks = parse_hunks('@@ -1,1 +1,1 @@\n target\n')
        pos, _ = _find_hunk_pos(lines, hunks[0]['lines'], 0, fuzzy=True)
        assert pos == 60


# ---------------------------------------------------------------------------
# 7. Fuzzy matching (indent-tolerant & full-file scan)
# ---------------------------------------------------------------------------

class TestFuzzyMatching:
    def test_indent_tolerant_within_window(self):
        """Patch has wrong indentation but content is within ±50 lines."""
        p = make_file('    def foo():\n        return 1\n')
        # Patch uses 2-space indent instead of 4-space
        r = apply_hunks(p, '@@ -1,2 +1,2 @@\n   def foo():\n-      return 1\n+      return 2\n')
        assert r['result'] == 'success', r
        assert 'return 2' in read_file(p)
        os.unlink(p)

    def test_tabs_vs_spaces(self):
        """File uses tabs, patch uses spaces."""
        p = make_file('\tdef foo():\n\t\treturn 1\n')
        r = apply_hunks(p, '@@ -1,2 +1,2 @@\n     def foo():\n-        return 1\n+        return 2\n')
        assert r['result'] == 'success', r
        os.unlink(p)

    def test_full_file_scan_fallback(self):
        """Content is >50 lines away from stated position."""
        lines = [f'filler_{i}\n' for i in range(100)]
        lines += ['unique_target\n', 'after_unique\n']
        p = make_file(''.join(lines))
        r = apply_hunks(p, '@@ -1,2 +1,2 @@\n unique_target\n-after_unique\n+REPLACED\n')
        assert r['result'] == 'success', r
        assert 'REPLACED' in read_file(p)
        os.unlink(p)

    def test_full_file_scan_with_indent_tolerance(self):
        """Content is >50 lines away AND has wrong indentation."""
        lines = [f'filler_{i}\n' for i in range(100)]
        lines += ['    indented_target\n', '    after_target\n']
        p = make_file(''.join(lines))
        r = apply_hunks(p, '@@ -1,2 +1,2 @@\n indented_target\n-after_target\n+REPLACED\n')
        assert r['result'] == 'success', r
        assert 'REPLACED' in read_file(p)
        os.unlink(p)

    def test_nonexistent_content_still_fails(self):
        """Truly non-existent content still returns error."""
        p = make_file('line1\nline2\nline3\n')
        r = apply_hunks(p, '@@ -1,2 +1,2 @@\n TOTALLY_NONEXISTENT\n-ALSO_NONEXISTENT\n+REPLACED\n')
        assert 'error' in r
        os.unlink(p)


# ---------------------------------------------------------------------------
# 8. Model-facing Git patch format
# ---------------------------------------------------------------------------

class TestGitPatchFormat:
    def test_accepts_existing_file_patch(self):
        patch = (
            'diff --git a/app.py b/app.py\n'
            'index 1111111..2222222 100644\n'
            '--- a/app.py\n'
            '+++ b/app.py\n'
            '@@ -1,2 +1,2 @@\n'
            ' alpha\n'
            '-beta\n'
            '+BETA\n'
        )
        info = validate_git_patch_format(patch)
        assert info == {
            'old_path': 'a/app.py',
            'new_path': 'b/app.py',
            'is_new_file': False,
            'hunk_count': 1,
        }

    def test_accepts_new_file_patch(self):
        patch = (
            'diff --git a/new.py b/new.py\n'
            'new file mode 100644\n'
            '--- /dev/null\n'
            '+++ b/new.py\n'
            '@@ -0,0 +1,2 @@\n'
            '+first\n'
            '+second\n'
        )
        info = validate_git_patch_format(patch)
        assert info['is_new_file'] is True
        assert info['hunk_count'] == 1

    def test_accepts_git_quoted_paths(self):
        patch = (
            'diff --git "a/space name.txt" "b/space name.txt"\n'
            '--- "a/space name.txt"\n'
            '+++ "b/space name.txt"\n'
            '@@ -1 +1 @@\n'
            '-old\n'
            '+new\n'
        )
        info = validate_git_patch_format(patch)
        assert info['old_path'] == 'a/space name.txt'
        assert info['new_path'] == 'b/space name.txt'

    @pytest.mark.parametrize(
        ('patch', 'message'),
        [
            (
                '@@ -1 +1 @@\n-old\n+new\n',
                'Missing "diff --git',
            ),
            (
                'diff --git a/a.txt b/a.txt\n'
                '--- a/a.txt\n'
                '+++ b/a.txt\n'
                '@@ -1,2 +1,2 @@\n'
                '-old\n'
                '+new\n',
                'Hunk line counts do not match',
            ),
            (
                'diff --git a/a.txt b/a.txt\n'
                '--- a/a.txt\n'
                '+++ b/a.txt\n'
                '@@ -1 +1 @@\n'
                '-old\n'
                '+new\n'
                '*** End Patch\n',
                'Invalid hunk line',
            ),
            (
                'diff --git a/a.txt b/a.txt\n'
                'deleted file mode 100644\n'
                '--- a/a.txt\n'
                '+++ /dev/null\n'
                '@@ -1 +0,0 @@\n'
                '-old\n',
                'File-deletion patches are not supported',
            ),
            (
                'diff --git a/a.txt b/a.txt\n'
                'diff --git a/b.txt b/b.txt\n',
                'exactly one file per call',
            ),
        ],
    )
    def test_rejects_nonstandard_or_unsupported_input(self, patch, message):
        with pytest.raises(ValueError, match=message):
            validate_git_patch_format(patch)

    def test_execute_rejects_hunk_only_patch(self, tmp_path):
        target = tmp_path / 'file.txt'
        target.write_text('old\n')
        result = execute(
            {'sandbox_enabled': 0, 'safety_checker_enabled': 0},
            {
                'file_path': str(target),
                'patch': '@@ -1 +1 @@\n-old\n+new\n',
            },
        )
        assert 'Invalid Git unified diff' in result['error']
        assert target.read_text() == 'old\n'


# ---------------------------------------------------------------------------
# 9. Differential compatibility with git apply
# ---------------------------------------------------------------------------

GIT_AVAILABLE = shutil.which('git') is not None


def _write_fixture(root: Path, relative_path: str, content):
    if content is None:
        return
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _git_apply(root: Path, patch_text: str):
    subprocess.run(
        ['git', 'init', '-q'],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ['git', 'apply', '--whitespace=nowarn', '-'],
        cwd=root,
        input=patch_text,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(not GIT_AVAILABLE, reason='git is required for differential tests')
@pytest.mark.parametrize(
    ('case_name', 'relative_path', 'initial', 'patch_text'),
    [
        (
            'existing-untracked-replacement',
            'sample.txt',
            b'alpha\nbeta\ngamma\n',
            'diff --git a/sample.txt b/sample.txt\n'
            'index 1111111..2222222 100644\n'
            '--- a/sample.txt\n'
            '+++ b/sample.txt\n'
            '@@ -1,3 +1,3 @@\n'
            ' alpha\n'
            '-beta\n'
            '+BETA\n'
            ' gamma\n',
        ),
        (
            'existing-untracked-multiple-hunks',
            'sample.txt',
            b'a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\n',
            'diff --git a/sample.txt b/sample.txt\n'
            'index 1111111..2222222 100644\n'
            '--- a/sample.txt\n'
            '+++ b/sample.txt\n'
            '@@ -1,3 +1,3 @@\n'
            ' a\n'
            '-b\n'
            '+B\n'
            ' c\n'
            '@@ -10,3 +10,3 @@\n'
            ' j\n'
            '-k\n'
            '+K\n'
            ' l\n',
        ),
        (
            'existing-untracked-quoted-path',
            'space name.txt',
            b'old\n',
            'diff --git "a/space name.txt" "b/space name.txt"\n'
            'index 1111111..2222222 100644\n'
            '--- "a/space name.txt"\n'
            '+++ "b/space name.txt"\n'
            '@@ -1 +1 @@\n'
            '-old\n'
            '+new\n',
        ),
        (
            'hunk-content-resembles-file-headers',
            'sample.txt',
            b'-- old\n',
            'diff --git a/sample.txt b/sample.txt\n'
            'index 1111111..2222222 100644\n'
            '--- a/sample.txt\n'
            '+++ b/sample.txt\n'
            '@@ -1 +1 @@\n'
            '--- old\n'
            '+++ new\n',
        ),
        (
            'new-file',
            'nested/new.txt',
            None,
            'diff --git a/nested/new.txt b/nested/new.txt\n'
            'new file mode 100644\n'
            'index 0000000..1111111\n'
            '--- /dev/null\n'
            '+++ b/nested/new.txt\n'
            '@@ -0,0 +1,2 @@\n'
            '+first\n'
            '+second\n',
        ),
        (
            'new-file-without-trailing-newline',
            'new.txt',
            None,
            'diff --git a/new.txt b/new.txt\n'
            'new file mode 100644\n'
            'index 0000000..1111111\n'
            '--- /dev/null\n'
            '+++ b/new.txt\n'
            '@@ -0,0 +1 @@\n'
            '+only line\n'
            '\\ No newline at end of file\n',
        ),
        (
            'replace-file-without-trailing-newline',
            'sample.txt',
            b'old',
            'diff --git a/sample.txt b/sample.txt\n'
            'index 1111111..2222222 100644\n'
            '--- a/sample.txt\n'
            '+++ b/sample.txt\n'
            '@@ -1 +1 @@\n'
            '-old\n'
            '\\ No newline at end of file\n'
            '+new\n'
            '\\ No newline at end of file\n',
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and '\n' not in value else None,
)
def test_evonic_matches_git_apply_for_supported_text_patches(
        tmp_path, case_name, relative_path, initial, patch_text):
    evonic_root = tmp_path / 'evonic'
    git_root = tmp_path / 'git'
    evonic_root.mkdir()
    git_root.mkdir()
    _write_fixture(evonic_root, relative_path, initial)
    _write_fixture(git_root, relative_path, initial)

    evonic_target = evonic_root / relative_path
    evonic_result = execute(
        {'sandbox_enabled': 0, 'safety_checker_enabled': 0},
        {'file_path': str(evonic_target), 'patch': patch_text},
    )
    git_result = _git_apply(git_root, patch_text)

    assert git_result.returncode == 0, (
        f'{case_name}: git apply rejected fixture: {git_result.stderr}'
    )
    assert evonic_result.get('result') == 'success', (
        f'{case_name}: Evonic rejected fixture: {evonic_result}'
    )
    assert evonic_target.read_bytes() == (git_root / relative_path).read_bytes()


@pytest.mark.skipif(not GIT_AVAILABLE, reason='git is required for differential tests')
@pytest.mark.parametrize(
    ('case_name', 'target_exists', 'patch_text'),
    [
        (
            'hunk-only',
            True,
            '@@ -1 +1 @@\n-old\n+new\n',
        ),
        (
            'bad-hunk-count',
            True,
            'diff --git a/sample.txt b/sample.txt\n'
            '--- a/sample.txt\n'
            '+++ b/sample.txt\n'
            '@@ -1,2 +1,2 @@\n'
            '-old\n'
            '+new\n',
        ),
        (
            'regular-patch-for-missing-file',
            False,
            'diff --git a/sample.txt b/sample.txt\n'
            '--- a/sample.txt\n'
            '+++ b/sample.txt\n'
            '@@ -1 +1 @@\n'
            '-old\n'
            '+new\n',
        ),
        (
            'new-file-patch-for-existing-file',
            True,
            'diff --git a/sample.txt b/sample.txt\n'
            'new file mode 100644\n'
            '--- /dev/null\n'
            '+++ b/sample.txt\n'
            '@@ -0,0 +1 @@\n'
            '+new\n',
        ),
    ],
)
def test_evonic_and_git_apply_both_reject_invalid_or_inapplicable_patches(
        tmp_path, case_name, target_exists, patch_text):
    evonic_root = tmp_path / 'evonic'
    git_root = tmp_path / 'git'
    evonic_root.mkdir()
    git_root.mkdir()
    initial = b'old\n' if target_exists else None
    _write_fixture(evonic_root, 'sample.txt', initial)
    _write_fixture(git_root, 'sample.txt', initial)

    evonic_target = evonic_root / 'sample.txt'
    before = evonic_target.read_bytes() if evonic_target.exists() else None
    evonic_result = execute(
        {'sandbox_enabled': 0, 'safety_checker_enabled': 0},
        {'file_path': str(evonic_target), 'patch': patch_text},
    )
    git_result = _git_apply(git_root, patch_text)

    assert git_result.returncode != 0, f'{case_name}: git apply unexpectedly succeeded'
    assert 'error' in evonic_result, (
        f'{case_name}: Evonic unexpectedly succeeded: {evonic_result}'
    )
    after = evonic_target.read_bytes() if evonic_target.exists() else None
    assert after == before
