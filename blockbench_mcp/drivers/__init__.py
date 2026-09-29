"""后端驱动层（二期：Blockbench 插件桥）。

一期文档引擎 = 本地操作；RemoteDriver 是“实时 Blockbench”通道，
工具语义不变，只是执行后端不同；并负责按需拉起 Blockbench
（`ensure_running`，会清掉 ELECTRON_RUN_AS_NODE）。
"""

from .remote import (
    RemoteDriver,
    capture_blockbench_screenshot,
    ensure_blockbench_running,
    executable_candidates,
    resolve_blockbench_executable,
)

__all__ = [
    "RemoteDriver",
    "capture_blockbench_screenshot",
    "ensure_blockbench_running",
    "executable_candidates",
    "resolve_blockbench_executable",
]
