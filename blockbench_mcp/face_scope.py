"""Repaint drafts constrained to the texels owned by confirmed cube faces."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from math import ceil, floor
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from . import paint
from .document import FACE_KEYS, BlockbenchProject, finite_number, new_uuid
from .edit_scope import ScopeDraft
from .errors import BBError, ValidationError

FaceRef = tuple[str, str]
Rect = tuple[int, int, int, int]


def face_refs(project: BlockbenchProject, raw: Any) -> list[FaceRef]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("请选择至少一个方块面")
    faces = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("uuid"), str) or item.get("face") not in FACE_KEYS:
            raise ValidationError("每个面需要方块 uuid 和有效的 face")
        uid, face = item["uuid"], item["face"]
        cube = project.find_element_by_uuid(uid)
        if cube is None or face not in cube.faces:
            raise ValidationError(f"工程中找不到面 {uid}/{face}")
        faces.append((uid, face))
    if len(set(faces)) != len(faces):
        raise ValidationError("范围中有重复的方块面")
    return faces


def _rect(row: dict[str, Any], size: tuple[int, int], *, inside: bool) -> Rect:
    uv, units = row.get("uv"), row.get("uv_size")
    if not isinstance(uv, list) or len(uv) != 4 or not isinstance(units, list) or len(units) != 2:
        raise ValidationError("原生 UV 矩形或画布单位无效")
    units = [finite_number(value, "UV 画布尺寸", minimum=1e-9) for value in units]
    scaled = [finite_number(value, "UV 坐标") * size[i % 2] / units[i % 2] for i, value in enumerate(uv)]
    if any(value < 0 or value > size[i % 2] for i, value in enumerate(scaled)):
        raise ValidationError(f"{row['uuid']}/{row['face']} 的 UV 超出纹理")
    left, right = sorted((scaled[0], scaled[2]))
    top, bottom = sorted((scaled[1], scaled[3]))
    # A selected face owns whole texels. Even a fractional overlap protects
    # the texel if it can be sampled by an unselected face.
    lower, upper = (ceil, floor) if inside else (floor, ceil)
    return lower(left), lower(top), upper(right), upper(bottom)


def _fill(mask: Image.Image, box: Rect, value: int) -> None:
    if box[0] < box[2] and box[1] < box[3]:
        mask.paste(value, box)


@dataclass
class FaceScope:
    id: str
    faces: dict[FaceRef, tuple[str, Rect]]
    masks: dict[str, Image.Image]

    @classmethod
    def read(cls, project: BlockbenchProject, payload: dict[str, Any]) -> FaceScope:
        refs = face_refs(project, payload.get("faces"))
        selected = set(refs)
        rows = payload.get("footprints")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("uuid"), str)
            or row.get("face") not in FACE_KEYS or not isinstance(row.get("texture"), str)
            for row in rows
        ):
            raise ValidationError("插件未返回完整的原生 UV 映射")
        mapped = {(row.get("uuid"), row.get("face")): row for row in rows}
        if len(mapped) != len(rows) or not selected <= mapped.keys():
            raise ValidationError("选区含未贴图的面，或原生 UV 映射不完整")
        faces = {}
        masks = {}
        for uid in dict.fromkeys(mapped[ref]["texture"] for ref in refs):
            tex = project.find_texture_by_uuid(uid)
            if tex is None or not tex.source_data:
                raise ValidationError("局部重绘需要同步工程中的内嵌纹理")
            size = paint.load_canvas(tex).size
            if size != (tex.width, tex.height):
                raise ValidationError(f"纹理 {tex.name} 的声明尺寸与 PNG 尺寸不同")
            mask = Image.new("L", size)
            protected = []
            for ref, row in mapped.items():
                if row.get("texture") != uid:
                    continue
                box = _rect(row, size, inside=ref in selected)
                if ref in selected:
                    faces[ref] = (uid, box)
                    _fill(mask, box, 255)
                else:
                    protected.append(box)
            for box in protected:
                _fill(mask, box, 0)
            if mask.getbbox() is None:
                raise ValidationError(f"纹理 {tex.name} 没有独占的整像素，检查 UV 重叠或扩大选面范围")
            masks[uid] = mask
        return cls(payload["id"], faces, masks)

    def summary(self, project: BlockbenchProject) -> dict[str, Any]:
        return {
            "scope_id": self.id,
            "faces": [
                {"uuid": uid, "cube": project.element(uid).name, "face": face, "texture": atlas, "rect": list(box)}
                for (uid, face), (atlas, box) in self.faces.items()
            ],
            "textures": [
                {"uuid": uid, "name": project.texture(uid).name, "editable_pixels": mask.histogram()[255]}
                for uid, mask in self.masks.items()
            ],
        }

    def stage(self, project: BlockbenchProject, operations: list[dict[str, Any]]) -> tuple[ScopeDraft, bytes]:
        if not operations:
            raise ValidationError("至少提供一项重绘操作")
        before = {uid: paint.load_canvas(project.texture(uid)) for uid in self.masks}
        canvases = {uid: canvas.copy() for uid, canvas in before.items()}
        for index, operation in enumerate(operations):
            try:
                self._apply(canvases, operation)
            except (BBError, OSError, TypeError, ValueError) as exc:
                raise ValidationError(f"operations[{index}]：{exc}") from exc

        draft = deepcopy(project)
        changes = []
        for uid, after in canvases.items():
            changed = Image.new("L", after.size)
            for band in ImageChops.difference(before[uid], after).split():
                changed = ImageChops.lighter(changed, band)
            if ImageChops.multiply(changed, ImageChops.invert(self.masks[uid])).getbbox():
                raise ValidationError(f"纹理 {project.texture(uid).name} 改动了选面之外或被未选面共用的像素")
            count = after.width * after.height - changed.histogram()[0]
            if count:
                draft.texture(uid).source_data = paint.encode_canvas(after)
                changes.append({"uuid": uid, "name": project.texture(uid).name, "changed_pixels": count})
        if not changes:
            raise ValidationError("这批操作没有改变纹理像素")
        png = _comparison([(before[item["uuid"]], canvases[item["uuid"]]) for item in changes])
        return ScopeDraft(new_uuid(), draft, {"textures": changes}), png

    def _apply(self, canvases: dict[str, Image.Image], operation: dict[str, Any]) -> None:
        op = operation.get("op")
        if op == "paint" and set(operation) == {"op", "uuid", "face", "ops"}:
            uid, face = operation["uuid"], operation["face"]
            target = self.faces.get((uid, face)) if isinstance(uid, str) and isinstance(face, str) else None
            if target is None:
                raise ValidationError("只能绘制已确认范围内的面")
            atlas, box = target
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ValidationError("该面没有可绘制的整像素")
            ops = operation["ops"]
            if not isinstance(ops, list) or not ops or any(not isinstance(item, dict) or "region" in item for item in ops):
                raise ValidationError("paint 需要非空 ops，绘制区域由方块面确定")
            for item in ops:
                paint.apply_op(canvases[atlas], box, paint._parse_op(item))
        elif op == "replace_png" and set(operation) == {"op", "texture", "source_path"}:
            atlas, path = operation["texture"], operation["source_path"]
            if not isinstance(atlas, str) or atlas not in canvases or not isinstance(path, str) or Path(path).suffix.lower() != ".png":
                raise ValidationError("replace_png 需要范围内的纹理 UUID 和 .png 路径")
            with Image.open(path, formats=["PNG"]) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise ValidationError("重绘候选需要单帧 PNG")
                if image.size != canvases[atlas].size:
                    raise ValidationError("重绘 PNG 必须保持原画布尺寸")
                canvases[atlas] = image.convert("RGBA")
        else:
            raise ValidationError("使用 paint（uuid、face、ops）或 replace_png（texture、source_path）")


def _comparison(pairs: list[tuple[Image.Image, Image.Image]]) -> bytes:
    size = 256
    sheet = Image.new("RGB", (2 * size, len(pairs) * size + 22), "#e2e4ed")
    labels = ImageDraw.Draw(sheet)
    for col, title in enumerate(("Before", "After")):
        labels.text((col * size + 8, 5), title, fill="#202735")
    for row, pair in enumerate(pairs):
        for col, atlas in enumerate(pair):
            scale = size / max(atlas.size)
            resized = atlas.resize(tuple(max(1, round(axis * scale)) for axis in atlas.size), Image.Resampling.NEAREST)
            sheet.paste(resized, (col * size, 22 + row * size), resized)
    buffer = BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue()
