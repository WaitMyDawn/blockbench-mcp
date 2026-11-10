"""Scoped geometry drafts over a confirmed, versioned selection."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw

from .document import BlockbenchProject, finite_number, new_uuid
from .errors import BBError, ValidationError
from .geometry import world_cubes
from .preview import render_png

EPSILON = 1e-6
GEOMETRY_FIELDS = {"name", "from_", "to", "origin", "rotation"}


def _inside(points: list[list[float]], minimum: list[float], maximum: list[float]) -> bool:
    return all(
        minimum[axis] - EPSILON <= point[axis] <= maximum[axis] + EPSILON
        for point in points for axis in range(3)
    )


@dataclass
class EditScope:
    id: str
    uuids: list[str]
    parents: list[str | None]
    minimum: list[float]
    maximum: list[float]

    @classmethod
    def read(cls, project: BlockbenchProject, payload: dict[str, Any]) -> EditScope:
        scope_id, uuids = payload.get("id"), payload.get("uuids")
        if not isinstance(scope_id, str) or not scope_id or not isinstance(uuids, list) or not uuids:
            raise ValidationError("编辑范围必须包含范围 ID 和已确认的方块 UUID")
        if any(not isinstance(uid, str) for uid in uuids) or len(set(uuids)) != len(uuids):
            raise ValidationError("范围内的方块 UUID 无效或重复")
        world = {cube.uuid: corners for cube, corners in world_cubes(project, include_non_exported=True)}
        if any(uid not in world for uid in uuids):
            raise ValidationError("范围内的方块不在当前工程大纲中，请同步后重新选取")
        points = [point for uid in uuids for point in world[uid]]
        bounds = payload.get("bounds")
        if bounds is None:
            minimum = [min(point[axis] for point in points) for axis in range(3)]
            maximum = [max(point[axis] for point in points) for axis in range(3)]
        else:
            minimum, maximum = (bounds.get(key) for key in ("min", "max")) if isinstance(bounds, dict) else (None, None)
            if any(not isinstance(value, list) or len(value) != 3 for value in (minimum, maximum)):
                raise ValidationError("空间范围需要三维 min 和 max 坐标")
            minimum = [finite_number(value, "范围 min") for value in minimum]
            maximum = [finite_number(value, "范围 max") for value in maximum]
            if any(lo >= hi for lo, hi in zip(minimum, maximum)) or not _inside(points, minimum, maximum):
                raise ValidationError("所选方块跨出空间范围，请重新确认范围")
        parents = []
        for uid in uuids:
            parent = project.parent_group_of(uid)
            parents.append(parent.uuid if parent else None)
        return cls(scope_id, uuids, list(dict.fromkeys(parents)), minimum, maximum)

    def summary(self, project: BlockbenchProject) -> dict[str, Any]:
        return {
            "scope_id": self.id,
            "cubes": [project.element_info(project.element(uid)) for uid in self.uuids],
            "allowed_parents": [
                {"uuid": uid, "name": project.group(uid).name if uid is not None else None}
                for uid in self.parents
            ],
            "bounds": {"min": self.minimum, "max": self.maximum},
        }

    def stage(self, project: BlockbenchProject, operations: list[dict[str, Any]]) -> ScopeDraft:
        if not operations:
            raise ValidationError("至少提供一项局部编辑操作")
        draft = deepcopy(project)
        changed: set[str] = set()
        for index, operation in enumerate(operations):
            try:
                changed.add(self._apply(draft, operation))
            except BBError as exc:
                raise ValidationError(f"operations[{index}]：{exc}") from exc
        for cube, corners in world_cubes(draft, include_non_exported=True):
            if cube.uuid in changed and not _inside(corners, self.minimum, self.maximum):
                raise ValidationError(f"方块 {cube.name!r} ({cube.uuid}) 超出已确认的空间范围")

        before = {cube.uuid: cube for cube in project.elements}
        after = {cube.uuid: cube for cube in draft.elements}
        diff = {
            "added": [draft.element_info(cube) for uid, cube in after.items() if uid not in before],
            "updated": [
                {"before": project.element_info(before[uid]), "after": draft.element_info(cube)}
                for uid, cube in after.items() if uid in before and before[uid] != cube
            ],
            "deleted": [project.element_info(cube) for uid, cube in before.items() if uid not in after],
        }
        if not any(diff.values()):
            raise ValidationError("这批操作没有改变模型")
        return ScopeDraft(new_uuid(), draft, diff)

    def _apply(self, project: BlockbenchProject, operation: dict[str, Any]) -> str:
        op = operation.get("op")
        keys = set(operation) - {"op"}
        if op == "add":
            if not {"name", "from_", "to", "parent"} <= keys or keys - (GEOMETRY_FIELDS | {"parent"}):
                raise ValidationError("add 需要 name、from_、to、parent，可附带 origin 和 rotation")
            parent = operation["parent"]
            if parent not in self.parents:
                raise ValidationError("新增方块必须使用范围内的父骨骼 UUID 或已允许的 null 根节点")
        elif op in ("update", "delete"):
            allowed = GEOMETRY_FIELDS | {"uuid"} if op == "update" else {"uuid"}
            if keys - allowed or operation.get("uuid") not in self.uuids:
                raise ValidationError("update/delete 只接受已确认范围内的方块 UUID 和允许的字段")
        else:
            raise ValidationError("op 必须为 add、update 或 delete")
        if "name" in keys and not isinstance(operation["name"], str):
            raise ValidationError("name 必须是字符串")
        for field in keys & (GEOMETRY_FIELDS - {"name"}):
            value = operation[field]
            if not isinstance(value, list) or len(value) != 3:
                raise ValidationError(f"{field} 必须是三个有限数值")
            for component in value:
                finite_number(component, field)
        if op == "add":
            return project.add_element(**{key: operation[key] for key in keys}).uuid
        uid = operation["uuid"]
        if op == "delete":
            project.remove_element(uid)
        else:
            project.update_element(uid, **{key: operation[key] for key in keys - {"uuid"}})
        return uid


@dataclass
class ScopeDraft:
    id: str
    project: BlockbenchProject
    diff: dict[str, Any]

    def preview(self, base: BlockbenchProject, yaw: float, pitch: float) -> bytes:
        frame = [
            point for model in (base, self.project)
            for _, corners in world_cubes(model, include_non_exported=True) for point in corners
        ]
        size = 320
        image = Image.new("RGB", (2 * size, size + 22), "white")
        for index, (caption, model) in enumerate((("Before", base), ("After", self.project))):
            x = index * size
            png = render_png(
                model, width=size, height=size, yaw=yaw, pitch=pitch,
                frame_points=frame, include_non_exported=True,
            )
            with Image.open(BytesIO(png)) as panel:
                image.paste(panel, (x, 22))
            ImageDraw.Draw(image).text((x + 8, 5), caption, fill="black")
        output = BytesIO()
        image.save(output, "PNG")
        return output.getvalue()
