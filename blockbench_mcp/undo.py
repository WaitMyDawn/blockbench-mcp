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
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
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
    pixel_before: Any = None
    pixel_after: Any = None


class UndoManager:
    def __init__(self) -> None:
        self._project_id: int | None = None
        self.pixel_undo: list[PixelUndoRecord] = []
        self.pixel_redo: list[PixelUndoRecord] = []
        self.doc_undo: list[DocUndoRecord] = []
        self.doc_redo: list[DocUndoRecord] = []
        self._editing = False
        self._lock = RLock()

    def _pixel_state(self):
        return (list(self.pixel_undo), list(self.pixel_redo))

    def _restore_pixel_state(self, state):
        if state is not None:
            self.pixel_undo, self.pixel_redo = list(state[0]), list(state[1])

    @contextmanager
    def transaction(self, project: BlockbenchProject, label: str):
        """One mutation = one undo step. Exceptions restore document and history."""
        with self._lock:
            self.reset_if_project_changed(project)
            if self._editing:
                raise StateError("不支持嵌套编辑事务")
            before = copy.deepcopy(project)
            pixel_before = self._pixel_state()
            state = {"changed": False}
            self._editing = True
            try:
                yield state
                after = copy.deepcopy(project)
                state["changed"] = before.__dict__ != after.__dict__
                if state["changed"]:
                    # Replacing/removing bitmap sources invalidates old pixel-only history.
                    texture_state = lambda p: [(t.uuid, t.width, t.height, t.source_data, t.source_path) for t in p.textures]
                    if texture_state(before) != texture_state(after) and self._pixel_state() == pixel_before:
                        self.pixel_undo.clear()
                        self.pixel_redo.clear()
                    self.doc_undo.append(DocUndoRecord(before, after, label, pixel_before, self._pixel_state()))
                    self.doc_undo = self.doc_undo[-DOC_LIMIT:]
                    self.doc_redo.clear()
                else:
                    self._restore_pixel_state(pixel_before)
            except Exception:
                self._restore_doc_snapshot(project, before)
                self._restore_pixel_state(pixel_before)
                raise
            finally:
                self._editing = False

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
        self.reset_if_project_changed(project)
        pixel_before = self._pixel_state()
        record = PixelUndoRecord(label=label)
        for tex in textures:
            record.textures[tex.uuid] = (before.get(tex.uuid, tex.source_data or ""), tex.source_data or "")
        if record.textures:
            self.pixel_undo.append(record)
            self.pixel_undo = self.pixel_undo[-PIXEL_LIMIT:]
            self.pixel_redo.clear()
        if self._editing:
            return  # The outer transaction owns the document snapshot and rollback.
        snapshot_before = doc_before if doc_before is not None else copy.deepcopy(project)
        snapshot_after = copy.deepcopy(project)  # 此时为操作后状态
        doc = DocUndoRecord(before=snapshot_before, after=snapshot_after, label=label,
                            pixel_before=pixel_before, pixel_after=self._pixel_state())
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
        # Never alias a historical snapshot: editing after undo must not corrupt history.
        restored = copy.deepcopy(snap.__dict__)
        project.__dict__.clear()
        project.__dict__.update(restored)
        return True

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
        self._restore_pixel_state(record.pixel_before)
        self.doc_redo.append(record)
        return {"label": record.label}

    def doc_redo_once(self, project: BlockbenchProject) -> dict[str, Any]:
        self.reset_if_project_changed(project)
        if not self.doc_redo:
            raise StateError("没有可重做的文档级操作")
        record = self.doc_redo.pop()
        if not self._restore_doc_snapshot(project, record.after):
            raise StateError("文档快照恢复失败")
        self._restore_pixel_state(record.pixel_after)
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
