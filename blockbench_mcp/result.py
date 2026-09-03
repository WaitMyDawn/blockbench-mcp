"""结构化工具返回。

每个工具成功时返回:
    {"ok": true, "tool": "...", "data": {...}, "warnings": [...]}
失败时抛出 ToolError（带建议），由 FastMCP 序列化为错误结果，
Codex 能从错误信息中直接知道下一步该怎么做。
"""

from __future__ import annotations

from typing import Any


def ok(tool: str, data: dict[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "tool": tool,
        "data": data,
        "warnings": warnings or [],
    }

