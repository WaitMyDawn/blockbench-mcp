"""当前会话：一次 stdio 连接内只维护一个"打开的项目"。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .document import BlockbenchProject
from .errors import StateError
from .document import new_uuid

if TYPE_CHECKING:
    from .edit_scope import ScopeDraft


class Session:
    def __init__(self) -> None:
        self.project: BlockbenchProject | None = None
        self.save_path: str | None = None
        self.dirty: bool = False
        self.project_id: str = new_uuid()
        self.revision: int = 0
        self.synced_revision: int | None = None
        self.source_token: str | None = None
        self.scope_id: str | None = None
        self.scope_draft: ScopeDraft | None = None
        self.scope_bridge = False

    def new_identity(self) -> None:
        self.project_id = new_uuid()
        self.revision = 0
        self.synced_revision = None
        self.source_token = None
        self.scope_id = None
        self.scope_draft = None
        self.scope_bridge = False

    def edited(self) -> None:
        self.revision += 1
        self.dirty = True

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
        self.new_identity()


session = Session()
