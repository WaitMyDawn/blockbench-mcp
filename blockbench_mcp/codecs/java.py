"""Java 侧导出：Java Block/Item 模型 与 OptiFine CEM (JEM) 实体模型。

* java_block：对照 Blockbench js/formats/java/java_block.js
* optifine_entity (CEM)：对照 js/formats/optifine/optifine_jem.js
"""

from __future__ import annotations

import json
from typing import Any

from ..document import BlockbenchProject, Element, Group
from ..errors import ExportError


def _round(v: float) -> float:
    r = round(v, 4)
    return int(r) if r.is_integer() else r


def _axis_letter(index: int) -> str:
    return ("x", "y", "z")[index]


def _texture_path(tex: Any) -> str:
    """Blockbench getTexturePath：namespace:folder/name。"""
    name = tex.name or "texture"
    parts = [tex.folder, name] if tex.folder else [name]
    path = "/".join(parts)
    if tex.namespace and tex.namespace != "minecraft":
        return f"{tex.namespace}:{path}"
    return path


def _resource_location(tex: Any, resource_root: str) -> str:
    """把纹理名映射成 Java 资源路径，如 minecraft:block/stone。"""
    if tex.namespace and tex.namespace != "minecraft":
        ns = tex.namespace
    elif resource_root.startswith("minecraft:"):
        ns = "minecraft"
    else:
        ns = tex.namespace or "minecraft"
    folder = tex.folder or resource_root.removeprefix(f"{ns}:")
    stem = tex.name.rsplit(".", 1)[0]
    return f"{ns}:{folder}/{stem}" if folder else f"{ns}:{stem}"


# ---------------------------------------------------------------------------
# Java Block / Item Model
# ---------------------------------------------------------------------------
def java_block_json(
    project: BlockbenchProject,
    *,
    resource_root: str = "minecraft:block",
    include_name: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """导出 Java 方块/物品模型。返回 (json, warnings)。"""
    warnings: list[str] = []
    elements: list[dict[str, Any]] = []
    used_textures: list[Any] = []

    def tex_key(tex: Any) -> str:
        if tex not in used_textures:
            used_textures.append(tex)
        return f"layer{used_textures.index(tex)}"

    def compute_cube(el: Element) -> None:
        if not el.export:
            return
        element: dict[str, Any] = {"from": [_round(v) for v in el.from_], "to": [_round(v) for v in el.to]}
        if include_name and el.name != "cube":
            element["name"] = el.name
        if not el.shade:
            element["shade"] = False
        rot = el.rotation
        if any(abs(x) > 1e-9 for x in rot):
            if sum(1 for x in rot if abs(x) > 1e-9) > 1:
                warnings.append(
                    f"元素 {el.name} 多轴旋转 {rot} 无法完整写入经典 Java 模型（每元素仅单轴），"
                    f"已按 {_axis_letter(next(i for i in range(3) if abs(rot[i]) > 1e-9))} 轴导出"
                )
            axis_index = next(i for i in range(3) if abs(rot[i]) > 1e-9)
            element["rotation"] = {
                "origin": [_round(v) for v in el.origin],
                "axis": _axis_letter(axis_index),
                "angle": _round(rot[axis_index]),
            }
        faces: dict[str, Any] = {}
        for key, face in el.faces.items():
            if face.texture is None:
                continue
            tex = project.texture(face.texture)
            tag: dict[str, Any] = {}
            uv = [
                face.uv[0] * 16 / project.texture_width,
                face.uv[1] * 16 / project.texture_height,
                face.uv[2] * 16 / project.texture_width,
                face.uv[3] * 16 / project.texture_height,
            ]
            tag["uv"] = [_round(v) for v in uv]
            if face.rotation:
                tag["rotation"] = face.rotation
            tag["texture"] = "#" + tex_key(tex)
            faces[key] = tag
        if faces:
            element["faces"] = faces
            elements.append(element)

    def walk(uids: list[str]) -> None:
        for uid in uids:
            g = project.find_group_by_uuid(uid)
            if g:
                walk(g.children)
                continue
            el = project.find_element_by_uuid(uid)
            if el:
                compute_cube(el)

    walk(project.root_children)

    textures: dict[str, Any] = {}
    for tex in used_textures:
        textures[tex_key(tex)] = _resource_location(tex, resource_root)
    if len(textures) == 1 and "particle" not in textures:
        pass  # 单纹理时不强制 particle

    model: dict[str, Any] = {}
    if project.parent:
        model["parent"] = project.parent
    if project.credit:
        model["credit"] = project.credit
    if project.texture_width != 16 or project.texture_height != 16:
        model["texture_size"] = [project.texture_width, project.texture_height]
    if textures:
        model["textures"] = textures
    if elements:
        model["elements"] = elements
    return model, warnings


def export_java_block(
    project: BlockbenchProject,
    path: str,
    *,
    resource_root: str = "minecraft:block",
) -> dict[str, Any]:
    model, warnings = java_block_json(project, resource_root=resource_root)
    _write_json(model, path)
    return {
        "path": path,
        "model": {
            "elements": len(model.get("elements", [])),
            "textures": list(model.get("textures", {}).values()),
            "texture_size": model.get("texture_size"),
            "parent": model.get("parent"),
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# OptiFine CEM (JEM)
# ---------------------------------------------------------------------------
def cem_json(project: BlockbenchProject) -> dict[str, Any]:
    """导出 OptiFine JEM 实体模型。"""
    entity: dict[str, Any] = {}
    if project.credit:
        entity["credit"] = project.credit
    entity["textureSize"] = [project.texture_width, project.texture_height]

    default_texture = project.textures[0] if project.textures else None
    if default_texture:
        entity["texture"] = _texture_path(default_texture)
        entity["textureSize"] = [default_texture.width, default_texture.height]

    models: list[dict[str, Any]] = []

    def compile_part(g: Group, texture: Any | None) -> dict[str, Any] | None:
        children = [c for c in g.children if project.find_element_by_uuid(c) or project.find_group_by_uuid(c)]
        if not children:
            return None
        bone: dict[str, Any] = {
            "part": g.name,
            "id": g.name,
            "invertAxis": "xy",
            "translate": [-_round(v) for v in g.origin],
        }
        if any(abs(x) > 1e-9 for x in g.rotation):
            bone["rotate"] = [_round(v) for v in g.rotation]
        boxes: list[dict[str, Any]] = []
        for uid in g.children:
            child_group = project.find_group_by_uuid(uid)
            if child_group:
                sub = compile_part(child_group, texture)
                if sub:
                    bone.setdefault("submodels", []).append(sub)
                continue
            el = project.find_element_by_uuid(uid)
            if not el or not el.export:
                continue
            size = el.size()
            box: dict[str, Any] = {
                "coordinates": [
                    _round(el.from_[0]),
                    _round(el.from_[1]),
                    _round(el.from_[2]),
                    _round(size[0]),
                    _round(size[1]),
                    _round(size[2]),
                ]
            }
            if el.box_uv:
                box["textureOffset"] = [_round(v) for v in el.uv_offset]
            else:
                for key, face in el.faces.items():
                    if face.texture is None:
                        continue
                    box[f"uv{key.capitalize()}"] = [_round(v) for v in face.uv]
            boxes.append(box)
        if boxes:
            bone["boxes"] = boxes
        return bone

    def walk(uids: list[str]) -> None:
        for uid in uids:
            g = project.find_group_by_uuid(uid)
            if not g:
                continue
            part = compile_part(g, None)
            if part:
                models.append(part)

    walk(project.root_children)
    entity["models"] = models
    return entity


def export_cem(project: BlockbenchProject, path: str) -> dict[str, Any]:
    model = cem_json(project)
    _write_json(model, path)
    return {"path": path, "models": len(model.get("models", [])), "texture": model.get("texture")}


def _write_json(data: dict[str, Any], path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ExportError(f"无法写入 {path}", str(exc)) from exc
