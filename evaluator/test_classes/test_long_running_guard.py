"""
Test suite for long_running_guard module.
"""

import os
import sys

# Support running from project root or /workspace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, '/workspace')

from backend.tools.lib.long_running_guard import check_long_running, _detect_long_running, BYPASS_MARKER


# ============================================================================
# Detection tests — commands that SHOULD be flagged
# ============================================================================

class TestDetectLongRunning:

    def test_cmake(self):
        assert check_long_running("cmake -B build -S .") is not None

    def test_cmake_with_flags(self):
        assert check_long_running("cmake -DCMAKE_BUILD_TYPE=Release ..") is not None

    def test_make_bare(self):
        assert check_long_running("make") is not None

    def test_make_with_jobs(self):
        assert check_long_running("make -j4") is not None

    def test_make_target(self):
        assert check_long_running("make all") is not None

    def test_ninja(self):
        assert check_long_running("ninja -C build") is not None

    def test_gcc(self):
        assert check_long_running("gcc -o main main.c") is not None

    def test_gpp(self):
        assert check_long_running("g++ -std=c++17 -o app app.cpp") is not None

    def test_clang(self):
        assert check_long_running("clang -O2 -o prog prog.c") is not None

    def test_clangpp(self):
        assert check_long_running("clang++ -o app main.cpp") is not None

    def test_msbuild(self):
        assert check_long_running("msbuild project.sln") is not None

    def test_cargo_build(self):
        assert check_long_running("cargo build --release") is not None

    def test_cargo_test(self):
        assert check_long_running("cargo test") is not None

    def test_go_build(self):
        assert check_long_running("go build ./...") is not None

    def test_go_test(self):
        assert check_long_running("go test ./...") is not None

    def test_gradle(self):
        assert check_long_running("gradle build") is not None

    def test_gradlew(self):
        assert check_long_running("./gradlew assembleRelease") is not None

    def test_mvn(self):
        assert check_long_running("mvn clean install") is not None

    def test_dotnet_build(self):
        assert check_long_running("dotnet build") is not None

    def test_dotnet_publish(self):
        assert check_long_running("dotnet publish -c Release") is not None

    def test_npm_run_build(self):
        assert check_long_running("npm run build") is not None

    def test_yarn_build(self):
        assert check_long_running("yarn build") is not None

    def test_pnpm_build(self):
        assert check_long_running("pnpm build") is not None

    def test_webpack(self):
        assert check_long_running("webpack --mode production") is not None

    def test_vite_build(self):
        assert check_long_running("vite build") is not None

    def test_next_build(self):
        assert check_long_running("next build") is not None

    def test_npm_install(self):
        assert check_long_running("npm install") is not None

    def test_npm_ci(self):
        assert check_long_running("npm ci") is not None

    def test_yarn_install(self):
        assert check_long_running("yarn install") is not None

    def test_pnpm_install(self):
        assert check_long_running("pnpm install") is not None

    def test_pip_install(self):
        assert check_long_running("pip install -r requirements.txt") is not None

    def test_apt_get_install(self):
        assert check_long_running("apt-get install -y build-essential") is not None

    def test_apt_install(self):
        assert check_long_running("apt install nginx") is not None

    def test_docker_build(self):
        assert check_long_running("docker build -t myapp .") is not None

    def test_docker_compose_build(self):
        assert check_long_running("docker compose build") is not None

    def test_docker_compose_legacy_build(self):
        assert check_long_running("docker-compose build") is not None

    def test_bazel_build(self):
        assert check_long_running("bazel build //src:main") is not None

    def test_bazel_test(self):
        assert check_long_running("bazel test //...") is not None

    def test_configure(self):
        assert check_long_running("./configure --prefix=/usr/local") is not None

    def test_configure_and_make(self):
        assert check_long_running("./configure && make") is not None

    def test_multiline_with_build(self):
        script = "cd /project\nmake -j8\necho done"
        assert check_long_running(script) is not None


# ============================================================================
# Non-detection tests — commands that should NOT be flagged
# ============================================================================

class TestSafeCommands:

    def test_echo(self):
        assert check_long_running("echo hello") is None

    def test_ls(self):
        assert check_long_running("ls -la") is None

    def test_cat(self):
        assert check_long_running("cat file.txt") is None

    def test_grep(self):
        assert check_long_running("grep -r 'pattern' .") is None

    def test_cd_and_pwd(self):
        assert check_long_running("cd /tmp && pwd") is None

    def test_git_status(self):
        assert check_long_running("git status") is None

    def test_git_log(self):
        assert check_long_running("git log --oneline -5") is None

    def test_python_script(self):
        assert check_long_running("python3 script.py") is None

    def test_make_version(self):
        assert check_long_running("make --version") is None

    def test_make_dash_v(self):
        assert check_long_running("make -v") is None

    def test_ninja_version(self):
        assert check_long_running("ninja --version") is None

    def test_mv_file(self):
        assert check_long_running("mv a.txt b.txt") is None

    def test_cp_file(self):
        assert check_long_running("cp src.txt dst.txt") is None

    def test_touch(self):
        assert check_long_running("touch newfile.txt") is None

    def test_which(self):
        assert check_long_running("which cmake") is None

    def test_env(self):
        assert check_long_running("env | grep PATH") is None


# ============================================================================
# Return structure tests
# ============================================================================

class TestReturnStructure:

    def test_has_required_keys(self):
        r = check_long_running("make all")
        assert r is not None
        required_keys = [
            'matched_command', 'suggestion', 'run_script',
            'log_file', 'monitor_script', 'check_status_script',
            'check_exit_code_script',
        ]
        for key in required_keys:
            assert key in r, f"Missing key: {key}"

    def test_log_file_path(self):
        r = check_long_running("make all")
        assert r['log_file'].startswith("/tmp/evonic_build_")
        assert r['log_file'].endswith(".log")

    def test_monitor_script_tails_log(self):
        r = check_long_running("make all")
        assert r['monitor_script'] == f"tail -f {r['log_file']}"

    def test_matched_command_describes_tool(self):
        r = check_long_running("cmake -B build")
        assert "CMake" in r['matched_command']

        r = check_long_running("cargo build")
        assert "Cargo" in r['matched_command']

    def test_suggestion_is_descriptive(self):
        r = check_long_running("make -j4")
        assert "Do NOT retry" in r['suggestion']
        assert "run_script" in r['suggestion']


# ============================================================================
# Wrapper script tests
# ============================================================================

class TestWrapperScript:

    def test_has_tmux_fallback(self):
        r = check_long_running("make all")
        assert "tmux new-session" in r['run_script']

    def test_has_screen_fallback(self):
        r = check_long_running("make all")
        assert "screen -dmS" in r['run_script']

    def test_has_nohup_fallback(self):
        r = check_long_running("make all")
        assert "nohup" in r['run_script']

    def test_logs_exit_code(self):
        r = check_long_running("make all")
        assert "EXIT_CODE=" in r['run_script']

    def test_uses_tee_for_logging(self):
        r = check_long_running("make all")
        assert "tee" in r['run_script']

    def test_single_quotes_escaped(self):
        r = check_long_running("make CFLAGS='-O2 -Wall'")
        assert "'\\''" in r['run_script']

    def test_log_file_referenced_in_script(self):
        r = check_long_running("make all")
        assert r['log_file'] in r['run_script']

    def test_session_name_in_status_check(self):
        r = check_long_running("make all")
        # session name should appear in check_status_script
        assert "evonic_build_" in r['check_status_script']


# ============================================================================
# Integration test — bash.py execute() with long-running guard
# ============================================================================

class TestBashIntegration:

    def test_execute_rejects_build_command(self):
        from backend.tools.bash import execute
        agent = {'session_id': 'test-lr-guard'}
        r = execute(agent, {'script': 'make -j4'})
        assert 'error' in r
        assert 'BLOCKED' in r['error']
        assert 'EVONIC_LR_BYPASS' in r['error']

    def test_execute_allows_safe_command(self):
        from backend.tools.bash import execute
        agent = {'session_id': 'test-lr-guard-safe', '_skip_safety': True}
        r = execute(agent, {'script': 'echo hello'})
        # Should not be blocked by long-running guard
        assert r.get('level') != 'long_running'

    def test_execute_rejects_even_for_super_agent(self):
        from backend.tools.bash import execute
        agent = {'session_id': 'test-lr-super', 'is_super': True}
        r = execute(agent, {'script': 'cmake -B build'})
        assert 'BLOCKED' in r.get('error', '')


# ============================================================================
# Bypass tests — wrapper script must not be re-flagged
# ============================================================================

class TestBypassMarker:

    def test_wrapper_script_not_reflagged(self):
        """Agent runs the suggested run_script — guard must not block it again."""
        r = check_long_running("make -j4")
        assert r is not None
        r2 = check_long_running(r['run_script'])
        assert r2 is None, "Wrapper script should bypass guard"

    def test_bypass_marker_with_leading_whitespace(self):
        script = f"  {BYPASS_MARKER}\nmake -j4"
        assert check_long_running(script) is None

    def test_bypass_marker_exact(self):
        script = f"{BYPASS_MARKER}\nmake -j4"
        assert check_long_running(script) is None

    def test_no_bypass_without_marker(self):
        """A script without the marker should still be flagged."""
        assert check_long_running("make -j4") is not None

    def test_wrapper_contains_bypass_marker(self):
        """Generated run_script must start with the bypass marker."""
        r = check_long_running("cmake -B build")
        assert r['run_script'].startswith(BYPASS_MARKER)
