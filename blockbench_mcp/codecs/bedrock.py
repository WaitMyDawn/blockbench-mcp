"""Bedrock 实体几何体(.geo.json)与动画(.animation.json)导出。

坐标转换严格对照 Blockbench 源码 js/formats/bedrock/bedrock.js：
* 几何体 x 轴镜像：origin.x = -(from.x + size.x)
* 旋转与骨骼的 x/y 分量取反（Z 不变）
* 非 BoxUV 时每个面导出 {uv:[x,y], uv_size:[w,h]}，顶/底面翻转
"""

from __future__ import annotations

import math
from typing import Any

from ..document import BlockbenchProject, Element, Keyframe
from ..errors import ExportError
from ..geometry import world_cubes


def _num(v: Any) -> float:
    """数值或可解析字符串/Molang。纯数值直接返回。"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return 0.0
    return 0.0


def _negate_value(value: float | str) -> float | str:
    """invertMolang：数值取负；Molang 字符串包一层负号。"""
    if isinstance(value, str):
        try:
            result = -float(value)
            return 0.0 if result == 0 else result
        except ValueError:
            return f"-({value})"
    result = -float(value)
    return 0.0 if result == 0 else result


def _flip(channel: str, values: list[Any]) -> list[Any]:
    out = list(values)
    if channel in ("position", "rotation"):
        out[0] = _negate_value(out[0])
    if channel == "rotation":
        out[1] = _negate_value(out[1])
    return out


def _timecode(t: float) -> str:
    s = f"{round(t, 4):g}"
    return s if "." in s else s + ".0"


def _cube_geometry(cube: Element) -> dict[str, Any] | None:
    if not cube.export:
        return None
    size = cube.size()
    template: dict[str, Any] = {
        "origin": [-round(cube.from_[0] + size[0], 4), _round(cube.from_[1]), _round(cube.from_[2])],
        "size": [_round(size[0]), _round(size[1]), _round(size[2])],
    }
    rot = cube.rotation
    if any(abs(x) > 1e-9 for x in rot):
        template["pivot"] = [-_round(cube.origin[0]), _round(cube.origin[1]), _round(cube.origin[2])]
        template["rotation"] = [
            -_round(rot[0]),
            -_round(rot[1]),
            _round(rot[2]),
        ]
    if cube.box_uv:
        template["uv"] = [_round(cube.uv_offset[0]), _round(cube.uv_offset[1])]
    else:
        uv_map: dict[str, Any] = {}
        for key, face in cube.faces.items():
            if face.texture is None:
                continue
            w = abs(face.uv[2] - face.uv[0])
            h = abs(face.uv[3] - face.uv[1])
            u, v = face.uv[0], face.uv[1]
            if key in ("up", "down"):
                u += w
                v += h
                w, h = -w, -h
            entry: dict[str, Any] = {"uv": [_round(u), _round(v)], "uv_size": [_round(w), _round(h)]}
            if face.rotation:
                entry["uv_rotation"] = face.rotation
            uv_map[key] = entry
        if uv_map:
            template["uv"] = uv_map
    return template


def _round(v: float) -> float:
    r = round(v, 4)
    return int(r) if r.is_integer() else r


def geometry_json(project: BlockbenchProject, render_bounds: dict | None = None) -> dict[str, Any]:
    """生成 Bedrock 实体几何体 JSON。"""
    bones: list[dict[str, Any]] = []

    def collect_groups(uids: list[str], parent_bone: str | None, emitted: list[str]) -> None:
        for uid in uids:
            g = project.find_group_by_uuid(uid)
            if g is None:
                continue
            cubes = [c for c in g.children if project.find_element_by_uuid(c) is not None]
            child_groups = [c for c in g.children if project.find_group_by_uuid(c) is not None]
            if not g.export or (not cubes and not child_groups):
                continue
            bone: dict[str, Any] = {"name": g.name}
            if parent_bone:
                bone["parent"] = parent_bone
            bone["pivot"] = [-_round(g.origin[0]), _round(g.origin[1]), _round(g.origin[2])]
            if any(abs(x) > 1e-9 for x in g.rotation):
                bone["rotation"] = [-_round(g.rotation[0]), -_round(g.rotation[1]), _round(g.rotation[2])]
            cube_list: list[dict[str, Any]] = []
            for child_uid in g.children:
                el = project.find_element_by_uuid(child_uid)
                if el:
                    geo = _cube_geometry(el)
                    if geo:
                        cube_list.append(geo)
            if cube_list:
                bone["cubes"] = cube_list
            bones.append(bone)
            emitted.append(g.name)
            collect_groups(child_groups, g.name, emitted)

    # 根级元素包进 bb_main（与 Blockbench 一致）
    root_elements = [uid for uid in project.root_children if project.find_element_by_uuid(uid)]
    root_groups = [uid for uid in project.root_children if project.find_group_by_uuid(uid)]
    if root_elements:
        main_bone: dict[str, Any] = {"name": "bb_main", "pivot": [0, 0, 0]}
        cubes = []
        for uid in root_elements:
            el = project.find_element_by_uuid(uid)
            geo = _cube_geometry(el)
            if geo:
                cubes.append(geo)
        if cubes:
            main_bone["cubes"] = cubes
        bones.append(main_bone)
    collect_groups(root_groups, None, [])

    description: dict[str, Any] = {
        "identifier": geometry_identifier(project),
        "texture_width": project.texture_width,
        "texture_height": project.texture_height,
    }
    w, h, offset = _visible_box(project)
    description["visible_bounds_width"] = w
    description["visible_bounds_height"] = h
    description["visible_bounds_offset"] = [0.0, offset, 0.0]
    if render_bounds is not None:
        from ..document import finite_number, _as_vec3
        description.update(
            visible_bounds_width=finite_number(render_bounds["visible_bounds_width"], "visible_bounds_width", minimum=0.001),
            visible_bounds_height=finite_number(render_bounds["visible_bounds_height"], "visible_bounds_height", minimum=0.001),
            visible_bounds_offset=_as_vec3(render_bounds["visible_bounds_offset"], "visible_bounds_offset"),
        )

    entity: dict[str, Any] = {"description": description}
    if bones:
        entity["bones"] = bones
    return {
        "format_version": _geometry_format_version(project),
        "minecraft:geometry": [entity],
    }


def geometry_identifier(project: BlockbenchProject) -> str:
    ident = project.model_identifier or project.name or "unknown"
    ident = ident.removeprefix("geometry.")
    return "geometry." + ident


def _geometry_format_version(project: BlockbenchProject) -> str:
    # 简单 MVP：只要没用到高级特性，就用兼容性最好的 1.12.0
    return "1.12.0"


def _visible_box(project: BlockbenchProject) -> tuple[float, float, float]:
    """Static world-space bounds, including cube and ancestor rotations (16 units/block)."""
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for _, corners in world_cubes(project):
        for x, y, z in corners:
            xs.append(-x)
            ys.append(y)
            zs.append(z)
    if not xs:
        return 1.0, 1.0, 0.0
    radius = max(abs(v) for v in xs + zs) + 8
    width = max(1.0, math.ceil(radius * 2 / 16))
    y_min = math.floor((min(ys) - 1e-6) / 16)
    y_max = math.ceil((max(ys) + 1e-6) / 16)
    height = max(1.0, float(y_max - y_min))
    center = (y_min + y_max) / 2.0
    return width, height, center


def _compile_keyframe(channel: str, kf: Keyframe, keyframes: list[Keyframe]) -> Any:
    """按 keyframe.js compileBedrockKeyframe 语义导出单个关键帧。"""
    values = list(kf.values)
    idx = keyframes.index(kf)
    previous = keyframes[idx - 1] if idx > 0 else None
    if kf.interpolation == "bezier":
        raise ExportError("Bedrock/GeckoLib 暂不支持导出 Bezier 关键帧", "改用 linear/step/catmullrom，或先在 Blockbench 烘焙曲线")
    if kf.interpolation == "catmullrom":
        include_pre = (previous is None and kf.time > 0) or (previous is not None and previous.interpolation != "catmullrom")
        result = {"post": _flip(channel, values), "lerp_mode": "catmullrom"}
        if include_pre:
            result["pre"] = _flip(channel, values)
        return result
    if previous is not None and previous.interpolation == "step":
        return {"pre": _flip(channel, previous.values), "post": _flip(channel, values)}
    if len(keyframes) == 1 and kf.interpolation != "catmullrom":
        value = _flip(channel, values)
        if channel == "scale" and len(set(value)) == 1:
            return value[0]
        return value
    return _flip(channel, values)


def animation_json(project: BlockbenchProject) -> dict[str, Any]:
    """生成 Bedrock .animation.json（format_version 1.8.0）。"""
    animations: dict[str, Any] = {}
    for anim in project.animations:
        ani: dict[str, Any] = {}
        if anim.loop == "hold":
            ani["loop"] = "hold_on_last_frame"
        elif anim.loop == "loop":
            ani["loop"] = True
        if anim.length:
            ani["animation_length"] = round(anim.length, 4)
        if anim.override:
            ani["override_previous_animation"] = True
        bones: dict[str, Any] = {}
        for bone_uuid, ator in anim.animators.items():
            group = project.find_group_by_uuid(bone_uuid)
            name = group.name if group else ator.bone_name
            if not ator.keyframes:
                continue
            bone_tag: dict[str, Any] = {}
            for channel in ("rotation", "position", "scale"):
                kfs = ator.keyframes_of(channel)
                if not kfs:
                    continue
                per_time: dict[str, Any] = {}
                for kf in kfs:
                    per_time[_timecode(kf.time)] = _compile_keyframe(channel, kf, kfs)
                if len(per_time) == 1:
                    key, value = next(iter(per_time.items()))
                    if kfs[0].interpolation != "catmullrom" and not isinstance(value, dict):
                        per_time = value  # 单帧压缩成裸数组
                bone_tag[channel] = per_time
            if bone_tag:
                bones[name] = bone_tag
        if bones:
            ani["bones"] = bones
        animations[anim.name] = ani
    return {"format_version": "1.8.0", "animations": animations}


def export_geometry(project: BlockbenchProject, path: str, render_bounds: dict | None = None) -> dict[str, Any]:
    data = geometry_json(project, render_bounds)
    _write_json(data, path)
    return {"path": path, "format_version": data["format_version"], "geometry": geometry_identifier(project)}


def export_animation(project: BlockbenchProject, path: str) -> dict[str, Any]:
    data = animation_json(project)
    _write_json(data, path)
    names = list(data["animations"].keys())
    return {"path": path, "format_version": data["format_version"], "animations": names}


def export_combined(project: BlockbenchProject, geo_path: str, anim_path: str | None, render_bounds: dict | None = None) -> dict[str, Any]:
    """几何体 + 动画（可选）一起导出，供 Bedrock 资源包使用。"""
    # Validate all animation semantics before writing any part of the resource pair.
    if anim_path and project.animations:
        animation_json(project)
    geo = export_geometry(project, geo_path, render_bounds)
    anim_result = None
    if anim_path and project.animations:
        anim_result = export_animation(project, anim_path)
    return {"geometry": geo, "animation": anim_result}


def _write_json(data: dict[str, Any], path: str) -> None:
    import json

    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ExportError(f"无法写入 {path}", str(exc)) from exc
