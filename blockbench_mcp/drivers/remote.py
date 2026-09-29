"""Blockbench 插件桥的 Python 客户端（二期）。

约定：Blockbench 内加载 plugin/blockbench_mcp_bridge.js 后，在本地端口
(默认 18765) 提供三个端点：
    GET  /health                -> {"ok": true, "blockbench_version": "..."}
    POST /command               -> {"method": "probe"|"open"|"eval"|..., "params": {...}}
    GET  /screenshot            -> image/png 字节流

本模块只依赖标准库：健康检查、截图、命令、以及"拉起 Blockbench 并等就绪"。
`ensure_running()` 会先探 /health，不通才启动 D:\\Blockbench\\Blockbench.exe
（可用 exe=... 或环境变量 BLOCKBENCH_EXE 覆盖），启动时会清掉
ELECTRON_RUN_AS_NODE，否则 Electron 会把 Blockbench 当 node 解释器跑。
"""

from __future__ import annotations

import json
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from ..errors import BBError, StateError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765

#: 拉起 Blockbench 时需要清掉的环境变量。
#: Electron 看到 ELECTRON_RUN_AS_NODE=1 会把 Blockbench.exe 当成 node 解释器直接跑，
#: 结果 Blockbench 界面不出现、插件桥也永远不会监听端口——这是"启动失败"最常见的原因。
ELECTRON_ENV_BLOCKLIST = ("ELECTRON_RUN_AS_NODE",)


class RemoteDriver:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 5.0):
        self._host = host
        self._port = port
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    def _request(self, path: str, method: str = "GET", payload: dict | None = None,
                 timeout: float | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error") or body
            except (ValueError, AttributeError):
                detail = body
            raise BBError(f"插件桥 HTTP {exc.code}：{detail[:1500]}") from exc
        except TimeoutError as exc:
            raise StateError(f"插件桥请求超时：{method} {path}", "命令可能仍在执行；先检查活动工程，不要自动重复提交修改") from exc
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

    def try_health(self, timeout: float | None = None) -> dict | None:
        """探一次 /health：可达返回 payload，不可达返回 None（不抛异常，供轮询使用）。"""
        probe = RemoteDriver(
            host=self._host,
            port=self._port,
            timeout=self.timeout if timeout is None else timeout,
        )
        try:
            return probe.health()
        except (BBError, StateError, ValueError, json.JSONDecodeError):
            return None

    def screenshot(self, out_path: str) -> str:
        """请求插件截取当前 Blockbench 视口并保存 PNG。"""
        status, body = self._request("/screenshot")
        if status != 200 or not body.startswith(b"\x89PNG"):
            raise BBError("插件桥截图失败", "确认 Blockbench 处于建模模式且有可见视口")
        with open(out_path, "wb") as fh:
            fh.write(body)
        return out_path

    def command(self, method: str, params: dict | None = None) -> dict:
        timeout = self.timeout
        if method in ("open", "reload"):
            ready = float((params or {}).get("ready_timeout_ms", 10000)) / 1000
            timeout = max(timeout, min(30, max(0, ready)) + 7)
        status, body = self._request("/command", method="POST", payload={"method": method, "params": params or {}}, timeout=timeout)
        if status != 200:
            raise BBError(f"插件桥命令失败：HTTP {status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BBError("插件桥命令返回无效 JSON") from exc
        if not isinstance(payload, dict):
            raise BBError("插件桥命令返回值必须是对象")
        return payload

    def capture(self, out_path: str, params: dict | None = None) -> dict:
        """A single bridge operation returns pixels and their viewport provenance."""
        result = self.command("capture", params)
        if not result.get("ok"):
            raise BBError(result.get("error") or "插件桥原子截图失败")
        try:
            png = base64.b64decode(result["png_base64"], validate=True)
        except (KeyError, ValueError) as exc:
            raise BBError("插件桥截图返回的 PNG 数据无效") from exc
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BBError("插件桥原子截图没有返回 PNG")
        with open(out_path, "wb") as fh:
            fh.write(png)
        return result["viewport"]

    # ------------------------------------------------------------------ 拉起
    def ensure_running(
        self,
        *,
        executable: str | os.PathLike[str] | None = None,
        wait_seconds: float = 30.0,
        poll_interval: float = 0.5,
        allow_launch: bool = True,
    ) -> dict[str, Any]:
        """确保 Blockbench 插件桥可用：先探 /health，不通则拉起 Blockbench 并轮询就绪。

        Args:
            executable: Blockbench 可执行文件路径；省略时按 ``BLOCKBENCH_EXE`` 环境变量、
                常见安装路径、PATH 依次查找。
            wait_seconds: 拉起后等待插件桥就绪的最长秒数。
            poll_interval: 就绪轮询间隔（秒）。
            allow_launch: False 时只探测、不拉起（纯只读检查）。

        Returns:
            ``{"action": "already_running"|"launched", "url", "waited_seconds", ...}``；
            ``action="launched"`` 时还带 ``executable`` / ``pid`` / ``cleared_env``。
            若新进程立即退出、但插件桥随后变得可达，会返回 ``action="already_running"``
            并带 ``note``，说明这次启动被交给了既有 Blockbench 实例（单实例锁）。

        Raises:
            StateError: 已经不可达、又找不到可执行文件 / 未允许拉起 / 拉起后超时未就绪。
                失败信息里会带上子进程输出尾部与环境变量清理情况，便于定位启动失败原因。
        """
        current = self.try_health()
        if current is not None:
            return {"action": "already_running", "url": self.base, "waited_seconds": 0.0, "health": current}

        if not allow_launch:
            raise StateError(
                f"Blockbench 插件桥不可达：{self.base}/health",
                "确认 Blockbench 已打开且已加载 plugin/blockbench_mcp_bridge.js",
            )

        exe = resolve_blockbench_executable(executable)
        if exe is None:
            raise StateError(
                "找不到 Blockbench 可执行文件",
                "用 blockbench_ensure_running(exe=...) 指定路径，或设置环境变量 BLOCKBENCH_EXE",
            )

        env, cleared = launch_env(os.environ)
        started = time.monotonic()
        exe_path = str(exe)
        # 子进程输出收到临时文件而不是 DEVNULL：Blockbench 秒退时，这几行常常是唯一
        # 能说明原因的东西（收进 DEVNULL 的话，调用方只看得到一句 exit code 0）。
        launch_log = tempfile.TemporaryFile(mode="w+b")

        def diag() -> str:
            """失败诊断：子进程输出尾部 + Electron 环境变量清理情况。"""
            parts: list[str] = []
            tail = _drain_log_tail(launch_log)
            if tail:
                parts.append("Blockbench 输出尾部：" + tail)
            if cleared:
                parts.append(
                    "已清掉的环境变量 " + ", ".join(cleared)
                    + "（不清掉的话 Electron 会把 Blockbench 当 node 解释器跑、界面不出现）"
                )
            else:
                parts.append("父进程未设置 " + "/".join(ELECTRON_ENV_BLOCKLIST))
            parts.append(f"试过的可执行文件：{exe}")
            return " | ".join(parts)

        try:
            process = subprocess.Popen(  # noqa: S603 - 路径来自显式配置/固定候选，非用户输入拼接
                [exe_path],
                env=env,
                cwd=os.path.dirname(exe_path) or None,
                stdin=subprocess.DEVNULL,
                stdout=launch_log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=_detached_flags(),
            )
        except OSError as exc:
            launch_log.close()
            raise StateError(f"拉起 Blockbench 失败：{exc}", f"检查可执行文件是否可用：{exe}") from exc

        deadline = started + max(0.0, wait_seconds)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                # 单实例锁：新进程把打开请求交给既有实例后立刻退出，此时桥反而会变得可达。
                late = self.try_health()
                if late is not None:
                    launch_log.close()
                    return {
                        "action": "already_running",
                        "note": "新进程立即退出但插件桥随后可用，判定为交给了既有 Blockbench 实例",
                        "url": self.base,
                        "waited_seconds": round(time.monotonic() - started, 2),
                        "pid": process.pid,
                        "cleared_env": cleared,
                        "health": late,
                    }
                detail = diag()
                launch_log.close()
                raise StateError(
                    f"Blockbench 启动后立即退出（exit code {process.returncode}）：{exe}",
                    "常见原因：① 已有 Blockbench 实例在运行（单实例锁会把新进程交回旧实例后退出），"
                    "此时请在 Blockbench 里重新加载插件桥；② 该文件不是桌面版可执行文件；"
                    "③ ELECTRON_RUN_AS_NODE 没清干净。"
                    f" 诊断：{detail}",
                )
            ready = self.try_health()
            if ready is not None:
                launch_log.close()
                return {
                    "action": "launched",
                    "url": self.base,
                    "waited_seconds": round(time.monotonic() - started, 2),
                    "executable": str(exe),
                    "pid": process.pid,
                    "cleared_env": cleared,
                    "health": ready,
                }
            time.sleep(max(0.05, poll_interval))

        detail = diag()
        launch_log.close()
        raise StateError(
            f"Blockbench 已启动（pid {process.pid}）但 {wait_seconds:g}s 内插件桥未就绪：{self.base}/health",
            "确认 plugin/blockbench_mcp_bridge.js 已加载且已授权；"
            "首次加载插件需要手动 File > Plugins > Load from File。"
            f" 诊断：{detail}",
        )


def _detached_flags() -> int:
    """Windows 上让 Blockbench 脱离 MCP Server 进程组，避免父进程退出时被带走。"""
    if sys.platform.startswith("win"):
        return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    return 0


def launch_env(environ: "Mapping[str, str]") -> tuple[dict[str, str], list[str]]:
    """复制一份环境变量给子进程，并清掉会让 Electron "变成" Node 的变量。"""
    env = dict(environ)
    cleared = [key for key in ELECTRON_ENV_BLOCKLIST if env.pop(key, None) is not None]
    return env, cleared


def same_path(
    left: str | os.PathLike[str] | None,
    right: str | os.PathLike[str] | None,
) -> bool:
    """判断两个路径是否指向同一个文件（用于识别「同路径项目已打开」）。

    Windows 下大小写与 ``/``、``\\`` 都不敏感，尾部斜杠也要忽略；不统一这些写法的话，
    ``D:\\a\\b.bbmodel`` 与 ``D:/a/b.bbmodel`` 会被当成两个不同项目，重载判断就会失效。
    """
    if not left or not right:
        return False

    def _norm(value: str | os.PathLike[str]) -> str:
        step = str(value).replace("\\", os.sep).replace("/", os.sep)
        return os.path.normcase(os.path.abspath(step))

    return _norm(left) == _norm(right)


def _drain_log_tail(handle: Any, limit: int = 1500) -> str:
    """读子进程日志的尾部若干字符；读不到就返回空串（诊断失败不能掩盖原本的错误）。"""
    try:
        handle.seek(0)
        raw = handle.read()
    except (OSError, ValueError):
        return ""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    return "..." + text[-limit:] if len(text) > limit else text


def executable_candidates(explicit: str | os.PathLike[str] | None = None) -> list[str]:
    """按优先级给出候选可执行文件路径（不保证存在）。"""
    candidates: list[str] = []
    if explicit:
        candidates.append(str(explicit))
    env_exe = os.environ.get("BLOCKBENCH_EXE")
    if env_exe:
        candidates.append(env_exe)
    if sys.platform.startswith("win"):
        candidates.append(r"D:\Blockbench\Blockbench.exe")
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "Blockbench", "Blockbench.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Blockbench.app/Contents/MacOS/Blockbench")
    else:
        candidates += ["/usr/bin/blockbench", "/opt/Blockbench/blockbench"]
    which = shutil.which("blockbench")
    if which:
        candidates.append(which)
    return candidates


def resolve_blockbench_executable(
    explicit: str | os.PathLike[str] | None = None,
) -> str | None:
    """返回第一个真实存在的候选可执行文件；都不存在返回 None。"""
    for candidate in executable_candidates(explicit):
        path = os.path.abspath(candidate)
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def capture_blockbench_screenshot(out_path: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return RemoteDriver(host=host, port=port).screenshot(out_path)


def ensure_blockbench_running(
    executable: str | os.PathLike[str] | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    wait_seconds: float = 30.0,
) -> dict[str, Any]:
    """便捷入口：等价于 ``RemoteDriver(host, port).ensure_running(executable=...)``。"""
    return RemoteDriver(host=host, port=port).ensure_running(
        executable=executable, wait_seconds=wait_seconds
    )
