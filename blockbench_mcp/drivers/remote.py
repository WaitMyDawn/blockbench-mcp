"""Blockbench 插件桥的 Python 客户端（二期骨架）。

约定：Blockbench 内加载 plugin/blockbench_mcp_bridge.js 后，在本地端口
(默认 18765) 提供三个端点：
    GET  /health                -> {"ok": true, "blockbench_version": "..."}
    POST /command               -> {"method": "open"|"eval"|..., "params": {...}}
    GET  /screenshot            -> image/png 字节流
当前插件尚未实现完整命令集；本模块先实现健康检查与截图下载，
供二期联调（需要 Blockbench 打开 + 插件已加载）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..errors import BBError, StateError


class RemoteDriver:
    def __init__(self, host: str = "127.0.0.1", port: int = 18765, timeout: float = 5.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    def _request(self, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.URLError as exc:
            raise StateError(
                "无法连接 Blockbench 插件桥",
                "确认 Blockbench 已打开且已加载 plugin/blockbench_mcp_bridge.js（默认端口 18765）",
            ) from exc

    def health(self) -> dict:
        status, body = self._request("/health")
        if status != 200:
            raise BBError(f"插件桥健康检查失败：HTTP {status}")
        return json.loads(body.decode("utf-8"))

    def screenshot(self, out_path: str) -> str:
        """请求插件截取当前 Blockbench 视口并保存 PNG。"""
        status, body = self._request("/screenshot")
        if status != 200 or not body.startswith(b"\x89PNG"):
            raise BBError("插件桥截图失败", "确认 Blockbench 处于建模模式且有可见视口")
        with open(out_path, "wb") as fh:
            fh.write(body)
        return out_path

    def command(self, method: str, params: dict | None = None) -> dict:
        status, body = self._request("/command", method="POST", payload={"method": method, "params": params or {}})
        if status != 200:
            raise BBError(f"插件桥命令失败：HTTP {status}")
        return json.loads(body.decode("utf-8"))


def capture_blockbench_screenshot(out_path: str, host: str = "127.0.0.1", port: int = 18765) -> str:
    return RemoteDriver(host=host, port=port).screenshot(out_path)

