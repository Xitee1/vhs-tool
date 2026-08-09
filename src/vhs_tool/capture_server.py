"""Client for the cxadc_vhs_server HTTP API (namazso/cxadc_vhs_server).

The server exposes a small control API returning JSON (/, /start, /stats,
/stop) and the raw capture streams (/cxadc?N, /linear). Control requests go
through http.client — no curl/jq needed. The data streams are read by curl
piped straight into the encoders: Python must never sit in the data path
(it cannot keep up with 40 MSPS), so this module only *builds* the curl argv
for a stream and leaves the process wiring to the caller.

ServerConnection isolates "how to reach the server" behind two operations
(JSON request, curl argv for a stream path). UnixSocketConnection — a locally
spawned server on a Unix socket — is the only transport today; a TCP/remote
transport can be added later without touching the capture logic.
"""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .common import ToolError

READY_TIMEOUT = 5.0  # seconds to wait for a freshly spawned server to listen
REQUEST_TIMEOUT = 10.0  # per control request


class ServerConnection:
    """How to reach a cxadc_vhs_server — JSON control calls + curl argv for streams."""

    def describe(self) -> str:
        """Human-readable endpoint description for log messages."""
        raise NotImplementedError

    def get_json(self, path: str) -> dict:
        """GET a control endpoint (e.g. '/stop') and parse the JSON response."""
        raise NotImplementedError

    def curl_cmd(self, path: str, output: str) -> list[str]:
        """curl argv streaming `path` to `output` ('-' = stdout, for pipe chains)."""
        raise NotImplementedError

    def ping(self) -> bool:
        """True once the server answers on '/' (any response counts)."""
        raise NotImplementedError


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client connection over an AF_UNIX socket (host header is a dummy)."""

    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class UnixSocketConnection(ServerConnection):
    def __init__(self, socket_path: Path | str, timeout: float = REQUEST_TIMEOUT):
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def describe(self) -> str:
        return f"unix:{self.socket_path}"

    def _request(self, path: str) -> tuple[int, bytes]:
        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def get_json(self, path: str) -> dict:
        try:
            status, body = self._request(path)
        except (OSError, http.client.HTTPException) as exc:
            raise ToolError(f"Cannot reach capture server ({self.describe()}): {exc}") from exc
        if status != 200:
            raise ToolError(f"Capture server returned HTTP {status} for {path}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Capture server returned invalid JSON for {path}: {exc}") from exc

    def curl_cmd(self, path: str, output: str) -> list[str]:
        return [
            "curl", "-s", "-X", "GET",
            "--unix-socket", self.socket_path,
            "--output", output,
            f"http://localhost{path}",
        ]  # fmt: skip

    def ping(self) -> bool:
        try:
            self._request("/")
            return True
        except (OSError, http.client.HTTPException):
            return False


class LocalServer:
    """A cxadc_vhs_server subprocess listening on a Unix socket in a temp dir.

    Context manager; __exit__ is only the safety net that reaps the server on
    an exception — the normal path calls terminate() explicitly *after* the
    stream chains have drained (stop → curl EOF → encoders → server).
    """

    def __init__(self, binary: str):
        self.binary = binary
        self._tempdir: tempfile.TemporaryDirectory | None = None
        self._process: subprocess.Popen | None = None
        self.connection: UnixSocketConnection | None = None

    def version(self) -> str:
        """`cxadc_vhs_server version` output (e.g. '1.4.0'), for the sidecar."""
        try:
            result = subprocess.run(
                [self.binary, "version"], capture_output=True, text=True, check=False
            )
            return result.stdout.strip() or "unknown"
        except OSError:
            return "unknown"

    def __enter__(self) -> UnixSocketConnection:
        self._tempdir = tempfile.TemporaryDirectory(prefix="vhs-tool-capture-")
        socket_path = Path(self._tempdir.name) / "server.sock"
        try:
            # Own session: a terminal Ctrl-C must reach only the vhs-tool
            # process (which stops gracefully via /stop), never the server.
            self._process = subprocess.Popen(
                [self.binary, f"unix:{socket_path}"], start_new_session=True
            )
        except OSError as exc:
            self._cleanup()
            raise ToolError(f"Cannot start capture server {self.binary}: {exc}") from exc
        self.connection = UnixSocketConnection(socket_path)
        try:
            self._wait_ready()
        except BaseException:
            self.terminate()
            self._cleanup()
            raise
        return self.connection

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            returncode = self._process.poll()
            if returncode is not None:
                raise ToolError(f"Capture server exited with code {returncode} during startup")
            if self.connection.ping():
                return
            if time.monotonic() > deadline:
                raise ToolError(f"Capture server unreachable ({self.connection.describe()})")
            time.sleep(0.1)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def poll(self) -> int | None:
        """Server exit code if it died, else None."""
        return self._process.poll() if self._process else None

    def terminate(self, timeout: float = 10.0) -> None:
        """SIGTERM the server (SIGKILL after `timeout`). Safe to call twice."""
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    def _cleanup(self) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None

    def __exit__(self, exc_type, exc, tb) -> None:
        self.terminate()
        self._cleanup()
