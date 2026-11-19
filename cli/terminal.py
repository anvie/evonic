"""
`evonic chat <agent-id>` — interactive chat room with an agent.

Thin client of the running server: it reuses the web chat endpoints (send,
history, session, approve, stop) and the realtime SSE stream, authenticating
with the local CLI token (see ``config.cli_api_token``).
"""
from __future__ import annotations

import getpass
import json
import os
import queue
import re
import sys
import threading
import time
import uuid

import requests

_LOCAL_COMMANDS = {
    "/help": "Show this help",
    "/approve": "Approve the pending tool call",
    "/reject": "Reject the pending tool call",
    "/exit": "Leave the chat",
    "/quit": "Leave the chat",
}
_POST_GRACE_SECONDS = 3.0
_HISTORY_LIMIT = 12


def _truncate(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


def _short_args(args) -> str:
    if isinstance(args, dict):
        parts = [f"{k}={_truncate(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v, 40)}"
                 for k, v in args.items()]
        return ", ".join(parts)
    return str(args or "")


def _result_preview(result) -> str:
    if isinstance(result, dict):
        for key in ("output", "result", "content", "message", "error", "stdout"):
            if result.get(key):
                return str(result[key])
        return json.dumps(result, ensure_ascii=False)
    return str(result or "")


class _Stream(threading.Thread):
    """Reads the realtime SSE stream into a queue; reconnects with Last-Event-ID."""

    def __init__(self, base, headers, agent_id, session_id, cursor):
        super().__init__(daemon=True)
        self.base, self.headers = base, headers
        self.agent_id, self.session_id = agent_id, session_id
        self.last_id = cursor
        self.events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.stop_flag = False

    def run(self):
        backoff = 1.0
        while not self.stop_flag:
            try:
                headers = dict(self.headers)
                params = {
                    "chat": "1", "channels": "approvals", "agent_id": self.agent_id,
                    "session_id": self.session_id, "cursor_version": "2", "snapshot": "0",
                }
                if self.last_id:
                    params["after"] = self.last_id
                    headers["Last-Event-ID"] = str(self.last_id)
                with requests.get(f"{self.base}/api/realtime/stream", params=params,
                                  headers=headers, stream=True, timeout=(5, 60)) as resp:
                    if resp.status_code in (401, 403):
                        self.events.put(("fatal", {"message": "Authentication rejected by server."}))
                        return
                    resp.raise_for_status()
                    backoff = 1.0
                    self._read(resp)
            except requests.RequestException:
                pass
            if not self.stop_flag:
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _read(self, resp):
        event, data, eid = "message", [], None
        for raw in resp.iter_lines(decode_unicode=True):
            if self.stop_flag:
                return
            if raw is None:
                continue
            if raw == "":
                if data:
                    try:
                        payload = json.loads("\n".join(data))
                    except ValueError:
                        payload = {}
                    if eid:
                        self.last_id = eid
                    if event == "auth_expired":
                        self.events.put(("fatal", {"message": "Session expired."}))
                        return
                    if event not in ("heartbeat", "ready"):
                        self.events.put((event, payload))
                event, data, eid = "message", [], None
            elif raw.startswith("id:"):
                eid = raw[3:].strip()
            elif raw.startswith("event:"):
                event = raw[6:].strip()
            elif raw.startswith("data:"):
                data.append(raw[5:].lstrip())


class Terminal:
    def __init__(self, agent, base, token, model_label="default"):
        from rich.console import Console
        self.agent = agent
        self.agent_id = agent["id"]
        self.name = agent.get("name") or self.agent_id
        self.model = model_label
        self.base = base
        self.headers = {"Authorization": f"Bearer {token}"}
        self.console = Console()
        self.user_id = re.sub(r"[^a-zA-Z0-9_\-.@]", "_", f"cli_{getpass.getuser()}")[:128]
        self.session_id = None
        self.stream = None
        self.turn_active = False
        self.pending_approval = None
        self._turn_lock = threading.Lock()

    # ---- HTTP helpers -------------------------------------------------
    def _url(self, path):
        return f"{self.base}/api/agents/{self.agent_id}{path}"

    def _get(self, url, **kw):
        return requests.get(url, headers=self.headers, timeout=15, **kw)

    def _post(self, url, payload, timeout=15):
        return requests.post(url, json=payload, headers=self.headers, timeout=timeout)

    # ---- rendering ----------------------------------------------------
    def header(self):
        from rich.panel import Panel
        from rich.text import Text
        body = Text()
        body.append(f"{self.name}", style="bold cyan")
        body.append(f"  ({self.agent_id})\n", style="dim")
        body.append(f"model {self.model} · session {str(self.session_id)[:8]}\n", style="dim")
        body.append("Enter send · Alt+Enter newline · /help · Ctrl+C stop · Ctrl+D exit", style="dim italic")
        self.console.print(Panel(body, border_style="cyan", padding=(0, 2)))

    def print_user(self, text, dim=False):
        from rich.text import Text
        t = Text()
        t.append("You  ", style="bold green" if not dim else "green dim")
        t.append(text, style="dim" if dim else "")
        self.console.print(t)

    def print_agent(self, text, dim=False, console=None, footer=None):
        from rich.console import Group
        from rich.markdown import Markdown
        from rich.padding import Padding
        from rich.text import Text
        console = console or self.console
        label = Text(self.name, style="bold cyan" if not dim else "cyan dim")
        parts = [label, Padding(Markdown(text, code_theme="monokai"), (0, 0, 0, 2))]
        if footer:
            parts.append(Text(footer, style="dim"))
        console.print(Group(*parts))
        console.print()

    def show_history(self):
        try:
            resp = self._get(self._url("/chat"), params={"session_id": self.session_id, "limit": _HISTORY_LIMIT})
            resp.raise_for_status()
            entries = resp.json().get("entries", [])
            self.cursor = resp.headers.get("X-Evonic-Realtime-Cursor", "")
        except (requests.RequestException, ValueError):
            self.cursor = ""
            return
        shown = False
        for e in entries:
            etype, text = e.get("type"), (e.get("content") or "").strip()
            if not text or (e.get("metadata") or {}).get("busy_ack"):
                continue
            if not shown:
                self.console.rule("[dim]earlier[/dim]", style="dim")
                shown = True
            if etype == "user":
                self.print_user(text, dim=True)
                self.console.print()
            elif etype in ("final", "intermediate", "error", "system"):
                self.print_agent(text, dim=True)
        if shown:
            self.console.rule("[dim]now[/dim]", style="dim")

    # ---- turn handling ------------------------------------------------
    @staticmethod
    def _tool_line(data):
        from rich.text import Text
        ok = not data.get("error")
        line = Text()
        line.append("  ✓ " if ok else "  ✗ ", style="green" if ok else "red")
        line.append(data.get("tool", ""), style="bold")
        line.append(f"({_truncate(_short_args(data.get('args')), 50)})", style="dim")
        preview = _truncate(_result_preview(data.get("result")), 70)
        if preview:
            line.append(f"  → {preview}", style="dim")
        return line

    # ---- turns that did not start from this prompt ---------------------
    def _idle_loop(self):
        """Render events from turns started elsewhere (web, another agent, ...)
        while the prompt is waiting. Yields to run_turn while one of ours runs."""
        buf, last_done = "", None
        self._idle_printed = None
        while not self.stream.stop_flag:
            with self._turn_lock:
                kind = None
                if not self.turn_active:
                    try:
                        kind, data = self.stream.events.get_nowait()
                    except queue.Empty:
                        pass
            if kind is None:
                time.sleep(0.1)
                continue
            try:
                buf, last_done = self._render_idle(kind, data, buf, last_done)
            except Exception as exc:  # never let rendering kill the listener
                self.console.print(f"[red]render error: {exc}[/red]")

    def _render_idle(self, kind, data, buf, last_done):
        from rich.text import Text
        if kind == "message_received":
            if data.get("role", "user") == "user" and not str(data.get("client_message_id") or "").startswith("cli-"):
                t = Text()
                t.append(f"◀ {data.get('sender') or 'incoming'}  ", style="bold magenta")
                t.append(str(data.get("message") or data.get("content") or ""), style="dim")
                self.console.print(t)
        elif kind == "turn_begin":
            buf = ""
            self._idle_printed = None
            self.console.print(Text("  … responding", style="dim italic"))
        elif kind == "tool_executed":
            self.console.print(self._tool_line(data))
        elif kind == "response_chunk":
            buf += data.get("content", "")
            if data.get("send_as_message") and buf.strip():
                self.print_agent(buf.strip())
                self._idle_printed = buf.strip()
                buf = ""
        elif kind == "approval_required" and data.get("agent_id") in ("", self.agent_id):
            self.pending_approval = data
            self.console.print(
                f"[yellow]⚠ approval required: {data.get('tool', 'tool')}"
                f"({_truncate(_short_args(data.get('args')), 80)}) — type /approve or /reject[/yellow]")
        elif kind == "fatal":
            self.console.print(f"[red]{data.get('message', 'Disconnected')}[/red]")
        elif kind == "done":
            key = data.get("message_id")
            text = (data.get("response") or buf or "").strip()
            if text and text != self._idle_printed and not (key and key == last_done):
                if data.get("is_error"):
                    self.console.print(f"[red]✗ {text}[/red]\n")
                else:
                    self.print_agent(text)
            last_done, buf, self._idle_printed = key or last_done, "", None
        return buf, last_done

    def resolve_pending(self, decision):
        data, self.pending_approval = self.pending_approval, None
        if not data:
            self.console.print("[dim]No approval pending.[/dim]")
            return
        try:
            self._post(self._url("/chat/approve"), {"approval_id": data.get("approval_id"), "decision": decision})
            self.console.print(f"[dim]{decision}d.[/dim]")
        except requests.RequestException as exc:
            self.console.print(f"[red]Could not send decision: {exc}[/red]")

    def _drain(self):
        while True:
            try:
                self.stream.events.get_nowait()
            except queue.Empty:
                return

    def ask_approval(self, payload):
        from rich.panel import Panel
        from rich.text import Text
        body = Text()
        body.append(f"{payload.get('tool', 'tool')}", style="bold yellow")
        body.append(f"({_truncate(_short_args(payload.get('args')), 200)})\n")
        for reason in payload.get("reasons") or []:
            body.append(f"• {reason}\n", style="dim")
        self.console.print(Panel(body, title="Approval required", border_style="yellow"))
        try:
            answer = self.console.input("[yellow]Approve? [y/N] [/yellow]").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = "n"
        decision = "approve" if answer in ("y", "yes") else "reject"
        try:
            self._post(self._url("/chat/approve"),
                       {"approval_id": payload.get("approval_id"), "decision": decision})
        except requests.RequestException as exc:
            self.console.print(f"[red]Could not send decision: {exc}[/red]")

    def run_turn(self, message):
        with self._turn_lock:
            self.turn_active = True
            self._drain()
        try:
            self._run_turn(message)
        finally:
            self.turn_active = False

    def _run_turn(self, message):
        from rich.console import Group
        from rich.live import Live
        from rich.markdown import Markdown
        from rich.padding import Padding
        from rich.spinner import Spinner
        from rich.text import Text

        post_result: dict = {}

        def worker():
            try:
                resp = self._post(self._url("/chat"), {
                    "message": message, "user_id": self.user_id,
                    "client_message_id": f"cli-{uuid.uuid4().hex[:16]}",
                }, timeout=3600)
                post_result["status"] = resp.status_code
                post_result["data"] = resp.json()
            except (requests.RequestException, ValueError) as exc:
                post_result["error"] = str(exc)
            post_result["at"] = time.time()

        threading.Thread(target=worker, daemon=True).start()

        started = time.time()
        buf = ""
        thinking = ""
        tools = 0
        stop_sent = False
        printed = None
        final = None

        def view():
            elapsed = int(time.time() - started)
            parts = [Spinner("dots", text=Text(f" working… {elapsed}s", style="dim"))]
            if thinking and not buf:
                tail = "\n".join(thinking.strip().splitlines()[-3:])
                parts.append(Padding(Text(_truncate(tail, 300), style="dim italic"), (0, 0, 0, 2)))
            if buf:
                parts.append(Padding(Markdown(buf, code_theme="monokai"), (0, 0, 0, 2)))
            return Group(*parts)

        with Live(view(), console=self.console, refresh_per_second=12,
                  transient=True, vertical_overflow="visible") as live:
            while final is None:
                try:
                    try:
                        kind, data = self.stream.events.get(timeout=0.08)
                    except queue.Empty:
                        kind, data = None, None
                    live.update(view())

                    if kind == "fatal":
                        final = {"is_error": True, "response": data.get("message", "Disconnected")}
                    elif kind == "thinking":
                        thinking += data.get("content", "")
                    elif kind == "response_chunk":
                        buf += data.get("content", "")
                        if data.get("send_as_message") and buf.strip():
                            self.print_agent(buf.strip(), console=live.console)
                            printed = buf.strip()
                            buf = ""
                    elif kind == "tool_call_started":
                        tools += 1
                        pass
                    elif kind == "tool_executed":
                        live.console.print(self._tool_line(data))
                    elif kind == "approval_required" and data.get("agent_id") in ("", self.agent_id):
                        live.stop()
                        self.ask_approval(data)
                        live.start()
                    elif kind == "done":
                        final = data

                    # POST finished without a matching `done` event: use its result.
                    if final is None and "at" in post_result:
                        res = post_result.get("data") or {}
                        if "error" in post_result or post_result.get("status", 200) >= 400:
                            final = {"is_error": True, "response": res.get("error") or post_result.get("error", "Request failed")}
                        elif res.get("response") is not None and not res.get("buffered") and not res.get("injected"):
                            if res.get("slash_command") or time.time() - post_result["at"] > _POST_GRACE_SECONDS:
                                final = {"response": res.get("response", ""), "is_error": bool(res.get("error")),
                                         "slash_command": res.get("slash_command"), "clear_ui": res.get("clear_ui")}
                except KeyboardInterrupt:
                    if stop_sent:
                        final = {"interrupted": True, "response": buf}
                    else:
                        stop_sent = True
                        try:
                            self._post(f"{self.base}/api/sessions/{self.session_id}/stop", {})
                        except requests.RequestException:
                            pass
                        live.console.print("[yellow]stopping… (Ctrl+C again to stop waiting)[/yellow]")

        text = (final.get("response") or buf or "").strip()
        if text == printed:       # already shown as a streamed message
            text = ""
        elapsed = time.time() - started
        res = post_result.get("data") or {}
        if final.get("is_error"):
            self.console.print(f"[red]✗ {text or 'The agent reported an error.'}[/red]\n")
            return
        if final.get("clear_ui") or res.get("clear_ui"):
            self.console.clear()
            self.header()
        footer = f"{elapsed:.1f}s" + (f" · {tools} tool call{'s' if tools != 1 else ''}" if tools else "")
        if final.get("interrupted"):
            footer += " · stopped"
        if text:
            self.print_agent(text, footer=footer)
        else:
            self.console.print(f"[dim]{footer}[/dim]\n")

    # ---- main loop ----------------------------------------------------
    def start(self):
        try:
            self.session_id = self._get(self._url("/chat/session"), params={"user_id": self.user_id}).json()["session_id"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            print(f"Error: could not open chat session ({exc}).")
            sys.exit(1)
        self.console.clear()
        self.header()
        self.show_history()
        self.stream = _Stream(self.base, self.headers, self.agent_id, self.session_id, self.cursor)
        self.stream.start()
        threading.Thread(target=self._idle_loop, daemon=True).start()

        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")

        session = PromptSession(
            history=FileHistory(os.path.expanduser("~/.evonic_terminal_history")),
            completer=WordCompleter(list(_LOCAL_COMMANDS), sentence=True),
            key_bindings=kb, multiline=True,
            prompt_continuation=lambda width, *_: " " * width,
            bottom_toolbar=HTML(f" <b>{self.name}</b> · {self.model} · session {str(self.session_id)[:8]} "),
        )
        with patch_stdout(raw=True):
            self._chat_loop(session, HTML)
        self.stream.stop_flag = True
        self.console.print("[dim]Bye.[/dim]")

    def _chat_loop(self, session, HTML):
        while True:
            try:
                text = session.prompt(HTML("<ansigreen><b>❯ </b></ansigreen>")).strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not text:
                continue
            cmd = text.split()[0].lower()
            if cmd in ("/exit", "/quit"):
                break
            if cmd in ("/approve", "/reject"):
                self.resolve_pending("approve" if cmd == "/approve" else "reject")
                continue
            if cmd == "/help":
                for name, desc in _LOCAL_COMMANDS.items():
                    self.console.print(f"  [cyan]{name}[/cyan]  {desc}")
                self.console.print("  [dim]Other /commands are handled by the agent.[/dim]\n")
                continue
            self.run_turn(text)


def run_terminal(agent_id):
    import config
    from cli.commands import _api_base_url, _get_db

    if not sys.stdin.isatty():
        print("Error: `evonic chat` needs an interactive terminal.")
        sys.exit(1)
    try:
        import rich  # noqa: F401
        import prompt_toolkit  # noqa: F401
    except ImportError:
        print("Error: missing dependencies. Run: pip install rich prompt_toolkit")
        sys.exit(1)

    db = _get_db()
    agent = db.get_agent(agent_id)
    if not agent:
        print(f"Error: agent '{agent_id}' not found.")
        agents = db.get_agents() or []
        if agents:
            print("Available agents:")
            for a in agents:
                print(f"  {a['id']:<24} {a.get('name') or ''}")
        sys.exit(1)

    base = _api_base_url()
    try:
        requests.get(f"{base}/api/health", timeout=3).raise_for_status()
    except requests.RequestException:
        print(f"Error: Evonic server is not reachable at {base}. Start it with: ./evonic start")
        sys.exit(1)

    model = (agent.get("model_id") and db.get_model_by_id(agent["model_id"])) or db.get_default_model()
    Terminal(agent, base, config.cli_api_token(), (model or {}).get("name") or "default").start()
