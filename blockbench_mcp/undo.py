"""纹理绘制 undo/redo 支持（MCP 会话内，不依赖 Blockbench 插件）。

设计：
- 像素级为主：每次绘制前把受影响纹理的"操作前 PNG"压栈（texture_undo / texture_redo）。
- 文档级为辅：同一操作同时把整个项目文档压栈（project_undo），可回退"误改 UV /
  误删立方体"等结构性操作；容量较小以控制内存。
- 项目身份保护：project_create / project_open 会换新 project 对象，undo 栈按
  project id 记录；一旦检测到项目被替换自动清空历史，避免跨项目误回退。

记录结构（关键：before 必须在任何像素写入前捕获）：
    PixelUndoRecord  { textures: {uuid: 操作前 source_data}, label }
    DocUndoRecord    { snapshot: deepcopy(project), label }
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .document import BlockbenchProject
from .errors import StateError

PIXEL_LIMIT = 50
DOC_LIMIT = 8


@dataclass
class PixelUndoRecord:
    """一条像素级编辑记录：携带操作前/操作后两个状态，undo/redo 均无推导。"""

    textures: dict[str, tuple[str, str]] = field(default_factory=dict)  # uuid -> (before, after)
    label: str = ""


@dataclass
class DocUndoRecord:
    """文档级记录：同时携带操作前/操作后快照，undo/redo 无需重建。"""

    before: Any = None
    after: Any = None
    label: str = ""


class UndoManager:
    def __init__(self) -> None:
        self._project_id: int | None = None
        self.pixel_undo: list[PixelUndoRecord] = []
        self.pixel_redo: list[PixelUndoRecord] = []
        self.doc_undo: list[DocUndoRecord] = []
        self.doc_redo: list[DocUndoRecord] = []

    # ---------- 项目切换保护 ----------
    def reset_if_project_changed(self, project: BlockbenchProject) -> bool:
        """项目对象被替换时清空全部历史；返回是否发生了重置。"""
        pid = id(project)
        if self._project_id is None or self._project_id != pid:
            self.clear_all()
            self._project_id = pid
            return True
        return False

    def clear_all(self) -> None:
        self.pixel_undo.clear()
        self.pixel_redo.clear()
        self.doc_undo.clear()
        self.doc_redo.clear()

    # ---------- 提交一次操作事务 ----------
    def push_edit(
        self,
        project: BlockbenchProject,
        *,
        before: dict[str, str],
        textures: list[Any],
        label: str = "",
        doc_before: Any = None,
    ) -> None:
        """在一个成功操作后调用。

        before  : {texture_uuid: 操作前 source_data}，必须在任何像素写入前捕获；
        textures: 当前（操作后）的纹理对象列表，用于校验受影响集合。
        doc_before: 操作前 deepcopy(project)；不传则回退到当前状态（等价于无文档级回退）。
        """
        if self.reset_if_project_changed(project):
            return  # 项目刚切换：此操作即为新文档的第一笔，无旧状态可回退
        record = PixelUndoRecord(label=label)
        for tex in textures:
            record.textures[tex.uuid] = (before.get(tex.uuid, tex.source_data or ""), tex.source_data or "")
        if record.textures:
            self.pixel_undo.append(record)
            self.pixel_undo = self.pixel_undo[-PIXEL_LIMIT:]
            self.pixel_redo.clear()
        snapshot_before = doc_before if doc_before is not None else copy.deepcopy(project)
        snapshot_after = copy.deepcopy(project)  # 此时为操作后状态
        doc = DocUndoRecord(before=snapshot_before, after=snapshot_after, label=label)
        self.doc_undo.append(doc)
        self.doc_undo = self.doc_undo[-DOC_LIMIT:]
        self.doc_redo.clear()

    def _apply_pixel_state(
        self,
        project: BlockbenchProject,
        states: dict[str, str],
    ) -> list[str]:
        restored: list[str] = []
        for uuid_, data_url in states.items():
            tex = project.find_texture_by_uuid(uuid_)
            if tex is None:
                continue
            if data_url:
                tex.source_data = data_url
            restored.append(tex.name)
        return restored

    def _restore_doc_snapshot(self, project: BlockbenchProject, snap: BlockbenchProject) -> bool:
        try:
            project.name = snap.name
            project.texture_width = snap.texture_width
            project.texture_height = snap.texture_height
            project.box_uv = snap.box_uv
            project.model_identifier = snap.model_identifier
            project.elements = snap.elements
            project.groups = snap.groups
            project.textures = snap.textures
            project.animations = snap.animations
            project.root_children = snap.root_children
            project.extra_root_fields = copy.deepcopy(snap.extra_root_fields)
            project.visible_box = list(snap.visible_box)
            project.parent = snap.parent
            project.credit = snap.credit
            return True
        except Exception:  # noqa: BLE001 - 快照恢复失败视为不可用
            return False

    # ---------- 像素级 undo / redo ----------
    def pixel_undo_once(self, project: BlockbenchProject) -> dict[str, Any]:
        self.reset_if_project_changed(project)
        if not self.pixel_undo:
            raise StateError("没有可回退的像素操作", "先执行一次 texture_paint_* 再调用 texture_undo")
        record = self.pixel_undo.pop()
        states = {uuid_: pair[0] for uuid_, pair in record.textures.items()}
        restored = self._apply_pixel_state(project, states)
        redo_record = PixelUndoRecord(label=record.label)
        redo_record.textures = {
            uuid_: (before_after[0], before_after[1]) for uuid_, before_after in record.textures.items()
        }
        self.pixel_redo.append(redo_record)
        return {"restored": restored, "label": record.label}

    def pixel_redo_once(self, project: BlockbenchProject) -> dict[str, Any]:
        self.reset_if_project_changed(project)
        if not self.pixel_redo:
            raise StateError("没有可重做的像素操作")
        record = self.pixel_redo.pop()
        states = {uuid_: pair[1] for uuid_, pair in record.textures.items()}
        restored = self._apply_pixel_state(project, states)
        self.pixel_undo.append(record)
        return {"restored": restored, "label": record.label}

    # ---------- 文档级 undo / redo ----------
    def doc_undo_once(self, project: BlockbenchProject) -> dict[str, Any]:
        self.reset_if_project_changed(project)
        if not self.doc_undo:
            raise StateError("没有可回退的文档级操作")
        record = self.doc_undo.pop()
        if not self._restore_doc_snapshot(project, record.before):
            raise StateError("文档快照恢复失败")
        self.doc_redo.append(DocUndoRecord(before=record.before, after=record.after, label=record.label))
        return {"label": record.label}

    def doc_redo_once(self, project: BlockbenchProject) -> dict[str, Any]:
        self.reset_if_project_changed(project)
        if not self.doc_redo:
            raise StateError("没有可重做的文档级操作")
        record = self.doc_redo.pop()
        if not self._restore_doc_snapshot(project, record.after):
            raise StateError("文档快照恢复失败")
        self.doc_undo.append(record)
        return {"label": record.label}

    def status(self, project: BlockbenchProject) -> dict[str, Any]:
        self.reset_if_project_changed(project)
        return {
            "pixel_undo": len(self.pixel_undo),
            "pixel_redo": len(self.pixel_redo),
            "doc_undo": len(self.doc_undo),
            "doc_redo": len(self.doc_redo),
            "limits": {"pixel": PIXEL_LIMIT, "doc": DOC_LIMIT},
        }


manager = UndoManager()
