"""当前会话：一次 stdio 连接内只维护一个"打开的项目"。"""

from __future__ import annotations

from .document import BlockbenchProject
from .errors import StateError


class Session:
    def __init__(self) -> None:
        self.project: BlockbenchProject | None = None
        self.save_path: str | None = None
        self.dirty: bool = False

    def require_project(self) -> BlockbenchProject:
        if self.project is None:
            raise StateError(
                "当前没有打开的项目",
                "先调用 project_create 新建，或 project_open 打开 .bbmodel 文件",
            )
        return self.project

    def reset(self) -> None:
        self.project = None
        self.save_path = None
        self.dirty = False


session = Session()

