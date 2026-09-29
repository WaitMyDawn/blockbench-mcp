"""插件桥客户端：健康探测与“按需拉起 Blockbench”的行为测试（不依赖真机 GUI）。"""

from __future__ import annotations

import json
import io
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from blockbench_mcp.drivers import remote
from blockbench_mcp.drivers.remote import RemoteDriver
from blockbench_mcp.errors import StateError
from blockbench_mcp.errors import BBError
import urllib.error


class _HealthHandler(BaseHTTPRequestHandler):
    """只实现 /health 的假插件桥，用来验证 ensure_running 的"已就绪"分支。"""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"ok": True, "blockbench_version": "fake", "phase": "test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音测试输出
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_ensure_running_returns_already_running() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        driver = RemoteDriver(port=server.server_address[1], timeout=2.0)
        result = driver.ensure_running()
    finally:
        server.shutdown()
        server.server_close()

    assert result["action"] == "already_running"
    assert result["health"]["blockbench_version"] == "fake"
    assert result["waited_seconds"] == 0.0


def test_ensure_running_without_launch_permission() -> None:
    driver = RemoteDriver(port=_free_port(), timeout=0.5)
    with pytest.raises(StateError) as exc:
        driver.ensure_running(allow_launch=False)
    assert "不可达" in str(exc.value)


def test_ensure_running_reports_early_exit(tmp_path) -> None:
    """拉起一个“立刻退出”的可执行文件时，要快速报错而不是空等整个超时。"""
    driver = RemoteDriver(port=_free_port(), timeout=0.5)
    with pytest.raises(StateError) as exc:
        # 用当前解释器当“假 Blockbench”：stdin 被接到 DEVNULL，会立即退出
        driver.ensure_running(executable=sys.executable, wait_seconds=10.0, poll_interval=0.05)
    assert "退出" in str(exc.value)


def test_launch_env_clears_electron_run_as_node() -> None:
    env, cleared = remote.launch_env({"ELECTRON_RUN_AS_NODE": "1", "PATH": "/usr/bin"})
    assert "ELECTRON_RUN_AS_NODE" not in env
    assert cleared == ["ELECTRON_RUN_AS_NODE"]
    assert env["PATH"] == "/usr/bin"

    env2, cleared2 = remote.launch_env({"PATH": "/usr/bin"})
    assert env2 == {"PATH": "/usr/bin"}
    assert cleared2 == []


def test_resolve_executable_prefers_explicit_path(tmp_path) -> None:
    fake = tmp_path / "Blockbench.exe"
    fake.write_text("", encoding="utf-8")
    assert remote.resolve_blockbench_executable(fake) == os.path.abspath(fake)
    assert remote.resolve_blockbench_executable(str(fake)) == os.path.abspath(fake)

    candidates = remote.executable_candidates(str(fake))
    # 显式路径必须排在默认候选（如 D:\Blockbench\Blockbench.exe）之前
    assert candidates[0] == str(fake)


def test_same_path_ignores_separators_case_and_trailing_slash() -> None:
    """同一路径的不同写法必须判等，否则"同路径项目已打开"会漏判、重载失效。"""
    assert remote.same_path(r"D:\models\glaive.bbmodel", "D:/models/glaive.bbmodel")
    assert remote.same_path(r"D:\models\Glaive.bbmodel", r"d:\MODELS\glaive.bbmodel")
    assert remote.same_path("/tmp/models/a.bbmodel", "/tmp/models/a.bbmodel/")
    assert not remote.same_path("/tmp/models/a.bbmodel", "/tmp/models/b.bbmodel")
    assert not remote.same_path(None, "/tmp/models/a.bbmodel")
    assert not remote.same_path("", "/tmp/models/a.bbmodel")


def test_ensure_running_error_carries_diagnostics(tmp_path) -> None:
    """启动失败时要把子进程输出尾部带出来，否则只剩一句 exit code 0，无法定位。"""
    driver = RemoteDriver(port=_free_port(), timeout=0.5)
    with pytest.raises(StateError) as exc:
        driver.ensure_running(executable=sys.executable, wait_seconds=10.0, poll_interval=0.05)
    message = str(exc.value)
    assert "诊断：" in message
    assert "试过的可执行文件" in message


def test_http_error_preserves_remote_diagnostic(monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("http://127.0.0.1/command", 500, "error", {},
                                     io.BytesIO(b'{"ok":false,"error":"render failed"}'))
    monkeypatch.setattr(remote.urllib.request, "urlopen", fail)
    with pytest.raises(BBError, match="render failed") as exc:
        RemoteDriver().command("capture")
    assert not isinstance(exc.value, StateError)


def test_open_timeout_covers_read_and_native_readiness(monkeypatch):
    driver = RemoteDriver(timeout=1)
    requests = []
    def request(*args, **kwargs):
        requests.append(kwargs)
        return 200, b'{"ok":true}'
    monkeypatch.setattr(driver, "_request", request)
    driver.command("open", {"ready_timeout_ms": 10000})
    assert requests[-1]["timeout"] >= 17


def test_invalid_capture_does_not_overwrite_prior_file(tmp_path, monkeypatch):
    driver = RemoteDriver()
    target = tmp_path / "view.png"
    target.write_bytes(b"old image")
    monkeypatch.setattr(driver, "command", lambda *a: {"ok": True, "png_base64": "YmFk"})
    with pytest.raises(BBError, match="PNG"):
        driver.capture(str(target))
    assert target.read_bytes() == b"old image"
