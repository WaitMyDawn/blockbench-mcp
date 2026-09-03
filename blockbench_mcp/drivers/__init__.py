"""后端驱动层（二期：Blockbench 插件桥）。

一期文档引擎 = 本地操作；RemoteDriver 预留“实时 Blockbench”通道，
工具语义不变，只是执行后端不同。
"""

from .remote import RemoteDriver, capture_blockbench_screenshot

__all__ = ["RemoteDriver", "capture_blockbench_screenshot"]

