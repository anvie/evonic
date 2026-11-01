"""/stop severs a streaming LLM request so the provider stops generating."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.llm_client import LLMClient, llm_cancel_event

state = {"closed": threading.Event()}


class _Slow(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for _ in range(200):
                frame = {"choices": [{"delta": {"reasoning_content": "hmm "}}]}
                self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            state["closed"].set()


def test_cancel_closes_stream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = LLMClient(model_config={
            "model_name": "m", "base_url": f"http://127.0.0.1:{srv.server_port}/v1",
            "api_key": "k", "provider": "openai", "enabled": True})
        cancel = threading.Event()
        llm_cancel_event.set(cancel)
        threading.Timer(0.5, cancel.set).start()
        start = time.monotonic()
        result = client.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            stream_callback=lambda k, t: None)
        assert result["error_type"] == "stopped"
        assert time.monotonic() - start < 3
        assert state["closed"].wait(3), "server never saw the connection drop"
    finally:
        llm_cancel_event.set(None)
        srv.shutdown()
