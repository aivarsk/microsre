import http.server
import os
import shutil
import tempfile
import threading
import time
import unittest


def isolate_xdg():
    """Point config/state dirs at a fresh temp dir for the whole test module."""
    tmp = tempfile.mkdtemp(prefix="microsre-test-")
    unittest.addModuleCleanup(shutil.rmtree, tmp, True)
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_STATE_HOME"] = os.path.join(tmp, "state")


class Receiver:
    """A real HTTP server that records every request it gets."""

    def __init__(self, headers=None):
        self.requests = []
        self._lock = threading.Lock()
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                with receiver._lock:
                    receiver.requests.append((self.path, dict(self.headers), self.rfile.read(length)))
                payload = b'{"status": "ok"}' if self.path.startswith("/ingest") else b""
                self.send_response(200)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, predicate, timeout=10):
        """True once predicate(recorded requests) holds, e.g. any(lambda rs: ...)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if predicate(self.requests):
                    return True
            time.sleep(0.1)
        return False

    def first_request(self, timeout=10):
        if self.wait_for(lambda requests: bool(requests), timeout):
            with self._lock:
                return self.requests[0]
        return None

    def close(self):
        self.server.shutdown()
        self.server.server_close()
