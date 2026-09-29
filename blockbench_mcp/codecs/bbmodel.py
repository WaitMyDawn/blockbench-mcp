""".bbmodel 5.0 编解码。

字段语义对照 Blockbench 源码 js/formats/bbmodel.js 与真实 5.0 样例：
* 顶层：meta / name / resolution / elements / groups / outliner / textures / animations
* 5.0 起 groups 与 outliner 分离；outliner 只用 uuid 建树
* face.texture 在文件里是纹理数组下标（编译期），读入后转 uuid
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from typing import Any

from ..document import (
    Animator,
    BlockbenchProject,
    Face,
    Group,
    Keyframe,
    SUPPORTED_FORMATS,
    finite_number,
    _as_vec3,
    Texture,
    vec3_to_json,
)
from ..errors import CodecError


def _decode_text(raw: str) -> dict[str, Any]:
    """尝试解析 JSON 文本；兼容 <lz> 前缀的自动备份。"""
    text = raw.lstrip("\ufeff")
    if text.startswith("<lz>"):
        try:
            from lzstring import LZString  # 仅开发/调试依赖，未安装时给出提示

            payload = LZString().decompressFromUTF16(text[4:])
            if not payload:
                raise CodecError("备份文件解压结果为空", "该备份可能使用 LZUTF8 编码，建议用 Blockbench 另存为普通 .bbmodel")
            text = payload
        except ImportError:
            raise CodecError(
                "这是 Blockbench 的 <lz> 压缩备份，需要 lzstring 才能读取",
                "pip install lzstring 后重试，或让用户在 Blockbench 里另存为普通 .bbmodel",
            ) from None
        except (TypeError, ValueError) as exc:
            raise CodecError("备份文件解压失败", str(exc)) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodecError(f".bbmodel 不是合法的 JSON：{exc}", "检查文件是否损坏") from exc


def _texture_index(project: BlockbenchProject, uuid_: str | None) -> int | None:
    if uuid_ is None:
        return None
    for i, t in enumerate(project.textures):
        if t.uuid == uuid_:
            return i
    return None


def project_to_dict(project: BlockbenchProject) -> dict[str, Any]:
    """把内存项目序列化为 bbmodel 5.0 字典。"""
    out: dict[str, Any] = {
        "meta": {
            "format_version": project.FORMAT_VERSION,
            "model_format": project.format_id,
            "box_uv": project.box_uv,
        },
        "name": project.name,
        "resolution": {"width": project.texture_width, "height": project.texture_height},
        "visible_box": vec3_to_json(project.visible_box),
    }
    if project.model_identifier:
        out["model_identifier"] = project.model_identifier
    if project.credit:
        out["credit"] = project.credit

    # 纹理：优先内嵌 PNG data URL，保证打开即可见
    textures: list[dict[str, Any]] = []
    for tex in project.textures:
        item: dict[str, Any] = {
            "uuid": tex.uuid,
            "name": tex.name,
            "folder": tex.folder,
            "namespace": tex.namespace,
            "id": tex.id,
            "width": tex.width,
            "height": tex.height,
            "uv_width": tex.width,
            "uv_height": tex.height,
            "particle": tex.particle,
            "use_as_default": tex is project.textures[0],
            "pbr_channel": tex.pbr_channel,
            "frame_time": 1,
            "frame_order_type": "loop",
            "frame_interpolate": False,
            "visible": True,
            "internal": True,
            "saved": False,
        }
        if tex.source_data:
            item["source"] = tex.source_data
        elif tex.source_path and os.path.isfile(tex.source_path):
            try:
                with open(tex.source_path, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("ascii")
                item["source"] = f"data:image/png;base64,{b64}"
            except OSError as exc:
                raise CodecError(f"读取纹理文件失败：{tex.source_path}", str(exc)) from exc
        textures.append(item)
    if textures:
        out["textures"] = textures

    # 立方体
    elements: list[dict[str, Any]] = []
    for el in project.elements:
        elem: dict[str, Any] = {
            "uuid": el.uuid,
            "name": el.name,
            "type": "cube",
            "box_uv": el.box_uv,
            "from": vec3_to_json(el.from_),
            "to": vec3_to_json(el.to),
            "origin": vec3_to_json(el.origin),
            "autouv": 0,
            "color": 0,
            "render_order": "default",
            "locked": False,
            "allow_mirror_modeling": True,
        }
        if not el.visibility:
            elem["visibility"] = False
        if not el.export:
            elem["export"] = False
        if not vec3_to_json(el.rotation) == [0, 0, 0]:
            elem["rotation"] = vec3_to_json(el.rotation)
        if any(abs(v) > 1e-9 for v in el.uv_offset):
            elem["uv_offset"] = vec3_to_json(el.uv_offset)
        faces: dict[str, Any] = {}
        for key, face in el.faces.items():
            fitem: dict[str, Any] = {
                "uv": [float(x) for x in face.uv],
                "texture": _texture_index(project, face.texture),
            }
            if face.rotation:
                fitem["rotation"] = face.rotation
            faces[key] = fitem
        elem["faces"] = faces
        elements.append(elem)
    out["elements"] = elements

    # 骨骼/组
    groups: list[dict[str, Any]] = []
    for g in project.groups:
        item: dict[str, Any] = {
            "uuid": g.uuid,
            "name": g.name,
            "origin": vec3_to_json(g.origin),
            "rotation": vec3_to_json(g.rotation),
            "export": g.export,
            "locked": False,
            "visibility": g.visibility,
            "color": g.color,
            "children": [],
            "reset": False,
            "shade": True,
            "mirror_uv": False,
            "autouv": 0,
            "isOpen": g.is_open,
        }
        if project.format_id == "bedrock":
            item["bedrock_binding"] = ""
        groups.append(item)
    out["groups"] = groups

    # 大纲树（只存 uuid + isOpen + children）
    def build_tree(uids: list[str]) -> list[dict[str, Any]]:
        tree: list[dict[str, Any]] = []
        for uid in uids:
            child_group = project.find_group_by_uuid(uid)
            if child_group:
                tree.append(
                    {
                        "uuid": uid,
                        "isOpen": child_group.is_open,
                        "children": build_tree(child_group.children),
                    }
                )
            else:
                tree.append(uid)  # 元素直接存 uuid 字符串
        return tree

    out["outliner"] = build_tree(project.root_children)

    # 动画
    if project.animations:
        animations: list[dict[str, Any]] = []
        for anim in project.animations:
            animators: dict[str, Any] = {}
            for bone_uuid, ator in anim.animators.items():
                keyframes: list[dict[str, Any]] = []
                for kf in ator.keyframes:
                    keyframes.append(
                        {
                            "channel": kf.channel,
                            "time": round(kf.time, 4),
                            "interpolation": kf.interpolation,
                            "uuid": kf.uuid,
                            "color": -1,
                            "data_points": [
                                {"x": kf.values[0], "y": kf.values[1], "z": kf.values[2]}
                            ],
                        }
                    )
                keyframes.sort(key=lambda k: k["time"])
                animators[bone_uuid] = {
                    "name": ator.bone_name,
                    "type": "bone",
                    "rotation_global": False,
                    "quaternion_interpolation": False,
                    "keyframes": keyframes,
                }
            animations.append(
                {
                    "uuid": anim.uuid,
                    "name": anim.name,
                    "loop": anim.loop if anim.loop in ("loop", "hold") else False,
                    "override": anim.override,
                    "length": anim.length,
                    "snapping": anim.snapping,
                    "anim_time_update": "",
                    "blend_weight": "",
                    "start_delay": "",
                    "loop_delay": "",
                    "animators": animators,
                }
            )
        out["animations"] = animations

    # 保留从文件读入的未知顶层字段
    for key, value in project.extra_root_fields.items():
        if key not in out:
            out[key] = value
    return out


def project_from_dict(data: dict[str, Any], *, path: str | None = None) -> BlockbenchProject:
    """从 bbmodel 字典重建项目（对字段缺失宽容）。"""
    meta = data.get("meta") or {}
    format_id = meta.get("model_format") or "generic"
    if format_id not in SUPPORTED_FORMATS:
        format_id = "generic"
    resolution = data.get("resolution") or {}
    project = BlockbenchProject(
        name=data.get("name") or "untitled",
        format_id=format_id,
        texture_width=int(resolution.get("width") or 16),
        texture_height=int(resolution.get("height") or 16),
        box_uv=bool(meta.get("box_uv")),
    )
    project.model_identifier = data.get("model_identifier") or ""
    vb = data.get("visible_box")
    if isinstance(vb, list) and len(vb) == 3:
        project.visible_box = _as_vec3(vb, "visible_box")
    project.credit = data.get("credit") or ""
    project.parent = data.get("parent") or ""

    # 未知顶层字段原样保留
    owned = {
        "meta", "name", "resolution", "elements", "groups", "outliner", "textures",
        "animations", "model_identifier", "visible_box", "credit",
    }
    project.extra_root_fields = {k: v for k, v in data.items() if k not in owned}

    tex_by_uuid: dict[str, Texture] = {}
    for raw in data.get("textures") or []:
        source = raw.get("source") if isinstance(raw.get("source"), str) else None
        tex = Texture(
            uuid=raw.get("uuid"),
            name=raw.get("name") or "texture",
            width=int(raw.get("width") or raw.get("uv_width") or 16),
            height=int(raw.get("height") or raw.get("uv_height") or 16),
            folder=raw.get("folder") or "",
            namespace=raw.get("namespace") or "",
            id=raw.get("id") or "0",
            particle=bool(raw.get("particle")),
            source_path=raw.get("relative_path") or raw.get("path") or None,
            source_data=source,
            pbr_channel=raw.get("pbr_channel") or "color",
        )
        if not tex.uuid:
            import uuid as uuid_lib

            tex.uuid = str(uuid_lib.uuid4())
        project.textures.append(tex)
        tex_by_uuid[tex.uuid] = tex

    for raw in data.get("elements") or []:
        el = project.add_element(
            name=raw.get("name") or "cube",
            from_=[float(x) for x in (raw.get("from") or [0, 0, 0])],
            to=[float(x) for x in (raw.get("to") or [1, 1, 1])],
            texture=None,
            origin=[float(x) for x in (raw.get("origin") or [0, 0, 0])],
            rotation=[float(x) for x in (raw.get("rotation") or [0, 0, 0])],
            uv_offset=[float(x) for x in (raw.get("uv_offset") or [0, 0])],
            faces={},
            allow_degenerate=True,
        )
        project._detach(el.uuid)  # add_element 会挂到根；解析阶段先脱离，稍后按 outliner 接线
        el.uuid = raw.get("uuid") or el.uuid
        el.box_uv = bool(raw.get("box_uv", project.box_uv))
        el.visibility = raw.get("visibility", True)
        el.export = raw.get("export", True)
        el.shade = raw.get("shade", True)
        el.faces = {}
        for key, fraw in (raw.get("faces") or {}).items():
            texture_ref = fraw.get("texture") if isinstance(fraw, dict) else None
            texture_uuid = None
            if isinstance(texture_ref, int) and 0 <= texture_ref < len(project.textures):
                texture_uuid = project.textures[texture_ref].uuid
            elif isinstance(texture_ref, str):
                texture_uuid = tex_by_uuid.get(texture_ref, texture_ref)
            uv = fraw.get("uv") or [0, 0, 0, 0]
            if len(uv) == 2:
                uv = [uv[0], uv[1], uv[0], uv[1]]
            el.faces[key] = Face(
                uv=[finite_number(x, "uv") for x in uv[:4]],
                texture=texture_uuid,
                rotation=int((fraw or {}).get("rotation") or 0),
            )
        project.elements.remove(el)
        project.elements.append(el)

    group_by_uuid: dict[str, Group] = {}
    for raw in data.get("groups") or []:
        g = Group(
            uuid=raw.get("uuid"),
            name=raw.get("name") or "group",
            origin=_as_vec3(raw.get("origin") or [0, 0, 0], "origin"),
            rotation=_as_vec3(raw.get("rotation") or [0, 0, 0], "rotation"),
            visibility=bool(raw.get("visibility", True)),
            export=bool(raw.get("export", True)),
            color=int(raw.get("color") or 0),
            is_open=bool(raw.get("isOpen", True)),
        )
        if not g.uuid:
            import uuid as uuid_lib

            g.uuid = str(uuid_lib.uuid4())
        project.groups.append(g)
        group_by_uuid[g.uuid] = g

    def parse_tree(nodes: list[Any], container: list[str]) -> None:
        for node in nodes:
            if isinstance(node, str):
                if node not in group_by_uuid and not project.find_element_by_uuid(node):
                    continue  # 引用丢失时跳过
                container.append(node)
            elif isinstance(node, dict):
                uid = node.get("uuid")
                target = group_by_uuid.get(uid) or project.find_group_by_uuid(uid)
                # 4.10 及更早：组对象直接内联在 outliner 里（无独立 groups 数组）
                if target is None and node.get("name"):
                    target = Group(
                        uuid=uid,
                        name=node["name"],
                        origin=_as_vec3(node.get("origin") or [0, 0, 0], "origin"),
                        rotation=_as_vec3(node.get("rotation") or [0, 0, 0], "rotation"),
                        visibility=bool(node.get("visibility", True)),
                        export=bool(node.get("export", True)),
                        color=int(node.get("color") or 0),
                        is_open=bool(node.get("isOpen", True)),
                    )
                    if not target.uuid:
                        import uuid as uuid_lib

                        target.uuid = str(uuid_lib.uuid4())
                    project.groups.append(target)
                    group_by_uuid[target.uuid] = target
                if not target:
                    continue
                container.append(target.uuid)
                parse_tree(node.get("children") or [], target.children)

    parse_tree(data.get("outliner") or [], project.root_children)

    # 不在 outliner 里的元素/组补到根部（与 Blockbench 行为一致）
    known = set(project.root_children)
    for g in project.groups:
        known.update(g.children)
    for g in project.groups:
        if g.uuid not in known:
            project.root_children.append(g.uuid)
    for e in project.elements:
        if e.uuid not in known:
            project.root_children.append(e.uuid)

    for raw in data.get("animations") or []:
        loop = raw.get("loop")
        anim = project.add_animation(
            name=raw.get("name") or "animation",
            length=float(raw.get("length") or 1.0),
            loop="loop" if loop in ("loop", True) else "hold" if loop == "hold" else "none",
            override=bool(raw.get("override")),
        )
        anim.uuid = raw.get("uuid") or anim.uuid
        anim.snapping = int(raw.get("snapping") or 24)
        for bone_uuid, ator_raw in (raw.get("animators") or {}).items():
            group = group_by_uuid.get(bone_uuid)
            ator = Animator(
                bone_uuid=bone_uuid,
                bone_name=(group.name if group else (ator_raw.get("name") or bone_uuid)),
            )
            for kf_raw in ator_raw.get("keyframes") or []:
                dp = (kf_raw.get("data_points") or [{}])[0]

                def num(v: Any) -> float:
                    try:
                        number = float(v)
                    except (TypeError, ValueError):
                        return 0.0
                    return finite_number(number, "keyframe.values")

                ator.keyframes.append(
                    Keyframe(
                        uuid=kf_raw.get("uuid"),
                        channel=kf_raw.get("channel") or "rotation",
                        time=finite_number(kf_raw.get("time") or 0.0, "time", minimum=0),
                        values=[num(dp.get("x")), num(dp.get("y")), num(dp.get("z"))],
                        interpolation=kf_raw.get("interpolation") or "linear",
                    )
                )
            if ator.keyframes:
                anim.animators[bone_uuid] = ator
    return project


def dumps(project: BlockbenchProject) -> str:
    return json.dumps(project_to_dict(project), ensure_ascii=False, indent=2, allow_nan=False)


def loads(text: str) -> BlockbenchProject:
    return project_from_dict(_decode_text(text))


def read(path: str) -> BlockbenchProject:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return loads(fh.read())
    except OSError as exc:
        raise CodecError(f"无法读取文件 {path}", str(exc)) from exc


def write(project: BlockbenchProject, path: str, *, sync_stamp: dict | None = None) -> None:
    """Serialize first and atomically replace, so bridge readers never see a partial file."""
    data = project_to_dict(project)
    if sync_stamp is not None:
        data["mcp_sync"] = sync_stamp
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        folder = os.path.dirname(os.path.abspath(path))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=folder, delete=False) as fh:
            temporary = fh.name
            fh.write(text)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise CodecError(f"无法写入文件 {path}", str(exc)) from exc
    finally:
        if temporary is not None:
            os.unlink(temporary)
