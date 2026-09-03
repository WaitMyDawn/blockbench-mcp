"""统一异常体系。

所有工具层错误都派生自 BBError，并携带 hint（给 Codex 的修复建议）。
FastMCP 会把 ToolError 转成结构化错误消息返回给调用方。
"""

from __future__ import annotations


class BBError(Exception):
    """Blockbench MCP 领域错误基类。"""

    kind = "bb_error"

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}（建议：{self.hint}）"
        return self.message


class NotFoundError(BBError):
    """按 uuid/name 找不到对象。"""

    kind = "not_found"


class ValidationError(BBError):
    """参数不符合约束。"""

    kind = "validation"


class StateError(BBError):
    """当前会话状态不允许该操作（例如尚未打开项目）。"""

    kind = "state"


class ExportError(BBError):
    """导出或写入文件失败。"""

    kind = "export"


class CodecError(BBError):
    """.bbmodel 读写失败。"""

    kind = "codec"

