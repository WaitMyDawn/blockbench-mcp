"""MCP 工具注册层。

每个工具语义上对齐 jasonjgardner/blockbench-mcp-plugin 的分域设计，
但执行对象是内存文档模型（FileDriver 一期能力），返回统一结构化 JSON。
"""

from __future__ import annotations

import base64
import os
import struct
import zlib
from functools import wraps
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from PIL import Image as PILImage

from .codecs import bbmodel as bbmodel_codec
from .codecs import bedrock as bedrock_codec
from .codecs import geckolib as geckolib_codec
from .codecs import java as java_codec
from . import examples as examples_lib
from .document import FACE_KEYS, BlockbenchProject, Face, Texture, finite_number, new_uuid
from .errors import BBError
from . import images as images_lib
from . import paint as paint_lib
from . import preview as preview_renderer
from . import quality as quality_checker
from . import animation_analysis
from .result import ok
from .session import session
from . import undo as undo_lib
from .drivers.remote import RemoteDriver, ensure_blockbench_running, same_path

mcp = FastMCP(
    "blockbench-mcp",
    instructions=(
        "Blockbench 模型 MCP Server（文档引擎模式）。典型流程：project_create → "
        "bone_create → cube_create → texture_create → texture_assign → animation_create "
        "→ keyframe_add → export_model。所有修改只作用于内存，需 project_save 或 "
        "export_model 落盘；Blockbench 可直接打开保存的 .bbmodel。"
        "参数化建模（几十上百个方块）优先用 cubes_create_bulk，一次调用建一批，"
        "比逐个 cube_create 少上千次往返。"
    ),
)


def _err(exc: BBError) -> ToolError:
    return ToolError(str(exc))


def _tool_error(message: str, hint: str | None = None) -> ToolError:
    """构造带「建议」的 ToolError。

    注意：FastMCP 的 ToolError 只接受一个参数，``ToolError("msg", "hint")`` 虽然不报错，
    但 ``str()`` 会变成 ``('msg', 'hint')`` 这种元组字符串，提示就废掉了——
    带建议的错误必须走这个 helper 把它拼进消息里。
    """
    return ToolError(f"{message}（建议：{hint}）" if hint else message)


def _project() -> BlockbenchProject:
    return session.require_project()


def _edit(fn):
    """Register edits as atomic, undoable document transactions."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        project = _project()
        dirty = session.dirty
        try:
            with undo_lib.manager.transaction(project, fn.__name__) as state:
                result = fn(*args, **kwargs)
        except BBError as exc:
            session.dirty = dirty
            raise _err(exc) from exc
        except Exception:
            session.dirty = dirty
            raise
        if state["changed"]:
            session.edited()
        else:
            session.dirty = dirty
        block = result[0] if isinstance(result, list) else result
        if isinstance(block, dict) and isinstance(block.get("data"), dict):
            block["data"]["revision"] = session.revision
            if "undo" in block["data"]:
                block["data"]["undo"] = undo_lib.manager.status(project)
        return result
    return wrapped


def _name_or_uuid_guard(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{label} 不能为空")
    return value.strip()


def _solid_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> str:
    """用纯标准库生成纯色 PNG，返回 data URL（避免 Pillow 依赖）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(rgba) * width
    raw = row * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _parse_color(color: str) -> tuple[int, int, int, int]:
    text = color.strip().lstrip("#")
    if len(text) == 6:
        text += "ff"
    if len(text) != 8:
        raise ToolError("颜色格式应为 #RRGGBB 或 #RRGGBBAA")
    try:
        r, g, b, a = (int(text[i : i + 2], 16) for i in (0, 2, 4, 6))
    except ValueError:
        raise ToolError(f"颜色无法解析：{color!r}") from None
    return r, g, b, a


def _prepare_textures(project: BlockbenchProject) -> list[tuple[Texture, PILImage.Image]]:
    """载入全部需要绘制的纹理画布；至少需要一张可编辑纹理。"""
    targets: list[tuple[Texture, PILImage.Image]] = []
    for tex in project.textures:
        if not (tex.source_data or (tex.source_path and os.path.isfile(tex.source_path))):
            continue
        targets.append((tex, paint_lib.load_canvas(tex)))
    if not targets:
        raise ToolError("项目里没有可编辑位图的纹理", "先 texture_create(color=...) 生成纯色画布")
    return targets


def _commit_texture_edits(
    project: BlockbenchProject,
    targets: list[tuple[Texture, PILImage.Image]],
    label: str,
    before: dict[str, str] | None = None,
    doc_before: Any | None = None,
) -> list[str]:
    """把内存画布统一写回纹理并提交一个 undo 事务（像素级 + 文档级快照）。"""
    changed: list[str] = []
    for tex, image in targets:
        tex.source_data = paint_lib.encode_canvas(image)
        if image.size != (tex.width, tex.height):
            tex.width, tex.height = image.size
            # 同步项目级画布尺寸，避免 Blockbench 打开时按旧分辨率显示
            project.texture_width = image.width
            project.texture_height = image.height
        changed.append(tex.name)
    undo_lib.manager.push_edit(
        project,
        before=before or {},
        textures=[t for t, _ in targets],
        label=label,
        doc_before=doc_before,
    )
    session.dirty = True
    return changed


# ---------------------------------------------------------------------------
# 项目管理
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "新建一个 Blockbench 项目（内存中）。format 可选：bedrock（基岩版实体，默认）、"
        "java_block（Java 方块/物品）、optifine_entity（CEM 实体模型）、generic、free。"
        "若要与 Blockbench 的 GeckoLib 插件工程互通（meta.model_format=geckolib_model），"
        "请选 geckolib_model；仅导出 GeckoLib 资源也可用 bedrock + export_model(target=geckolib)。"
        "texture_width/height 为画布纹理尺寸；box_uv=True 表示使用盒式 UV。"
    )
)
def project_create(
    name: str,
    format: str = "bedrock",
    texture_width: int = 64,
    texture_height: int = 64,
    box_uv: bool = False,
    model_identifier: str = "",
) -> dict[str, Any]:
    project = BlockbenchProject(
        name=name,
        format_id=format,
        texture_width=texture_width,
        texture_height=texture_height,
        box_uv=box_uv,
    )
    project.model_identifier = model_identifier
    session.project = project
    session.new_identity()
    session.save_path = None
    session.dirty = False
    undo_lib.manager.reset_if_project_changed(project)
    return ok("project_create", {"project": project.summary()})


@mcp.tool(description="打开 .bbmodel 文件到当前会话（覆盖未保存的内存项目）。返回项目摘要。")
def project_open(path: str) -> dict[str, Any]:
    try:
        project = bbmodel_codec.read(path)
    except BBError as exc:
        raise _err(exc) from exc
    session.project = project
    session.new_identity()
    session.save_path = path
    session.dirty = False
    undo_lib.manager.reset_if_project_changed(project)
    warnings = []
    issues = project.validate()
    if issues:
        warnings.append(f"打开后一致性检查发现 {len(issues)} 个问题（仍已载入）")
    return ok("project_open", {"project": project.summary(), "path": path}, warnings)


@mcp.tool(description="把当前项目保存为 .bbmodel。path 缺省时写回打开时的路径。")
def project_save(path: str | None = None) -> dict[str, Any]:
    project = _project()
    target = path or session.save_path
    if not target:
        raise ToolError("没有可保存的路径", "传入 path，例如 C:/models/my_model.bbmodel")
    token = new_uuid()
    bbmodel_codec.write(project, target, sync_stamp={
        "project_id": session.project_id, "revision": session.revision, "source_token": token,
    })
    session.save_path = target
    session.dirty = False
    session.source_token = token
    return ok("project_save", {"path": target, "project": project.summary(), "revision": session.revision, "source_token": token})


@mcp.tool(description="保存内存工程并可靠同步到 Blockbench，等待工程/纹理/视口就绪，返回实际加载的 project_id/revision/source_token。默认保护视口未保存修改；replace_unsaved=True 才允许替换。插件桥须已运行。")
def project_sync(path: str | None = None, replace_unsaved: bool = False, wait_seconds: float = 10.0,
                 expected_revision: int | None = None) -> dict[str, Any]:
    _project()
    target = path or session.save_path
    if not target:
        raise _tool_error("没有同步路径", "传入 .bbmodel 的保存路径")
    wait = finite_number(wait_seconds, "wait_seconds", minimum=0.1)
    if wait > 30:
        raise _tool_error("wait_seconds 最大为 30 秒")
    if expected_revision is not None and expected_revision != session.revision:
        raise _tool_error(f"工程版本已改变：期望 {expected_revision}，实际 {session.revision}")
    driver = RemoteDriver(timeout=wait + 7)
    try:
        health = driver.health()
        if "methods" in health and "capture" not in health["methods"]:
            raise _tool_error("project_sync 需要插件桥 0.5.0 或更新版本", "更新并热重载 plugin/blockbench_mcp_bridge.js")
        projects = health.get("projects", [health])
        if any(same_path(p.get("save_path"), target) and p.get("saved") is False for p in projects) and not replace_unsaved:
            raise _tool_error("Blockbench 同路径工程有未保存修改，未进行同步", "先保存视口修改，或明确传 replace_unsaved=True")
        project_save(target)
        payload = driver.command("open", {
            "path": os.path.abspath(target), "replace_unsaved": replace_unsaved,
            "ready_timeout_ms": int(wait * 1000), "expect_source_token": session.source_token,
            "expect_project_id": session.project_id, "expect_revision": session.revision,
        })
    except BBError as exc:
        raise _err(exc) from exc
    if (not payload.get("ok") or payload.get("source_token") != session.source_token
            or payload.get("revision") != session.revision or payload.get("project_id") != session.project_id):
        raise _tool_error(f"工程同步未确认：{payload.get('error') or payload}")
    session.synced_revision = session.revision
    return ok("project_sync", {"path": target, "viewport": payload, "revision": session.revision})


@mcp.tool(description="查看当前项目状态：格式、纹理尺寸、各类对象数量。")
def project_status() -> dict[str, Any]:
    project = _project()
    return ok(
        "project_status",
        {
            "project": project.summary(),
            "save_path": session.save_path,
            "dirty": session.dirty,
            "project_id": session.project_id,
            "revision": session.revision,
            "synced_revision": session.synced_revision,
            "outline": project.outline_tree(),
        },
    )


@mcp.tool(description="查看大纲层级：骨骼、其子骨骼与立方体，含尺寸/原点/旋转/父级。")
def project_outline() -> dict[str, Any]:
    project = _project()
    return ok("project_outline", {"outline": project.outline_tree()})


# ---------------------------------------------------------------------------
# 纹理
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "创建纹理。color（如 '#FF8800' 或 8 位 '#RRGGBBAA'）会生成纯色 PNG 并内嵌；"
        "source_path 指向磁盘 PNG 时内嵌该文件。没有 color/source_path 时只登记元数据。"
    )
)
@_edit
def texture_create(
    name: str,
    width: int = 16,
    height: int = 16,
    color: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    project = _project()
    source_data: str | None = None
    if color:
        source_data = _solid_png(max(1, width), max(1, height), _parse_color(color))
        source_path = None
    elif source_path:
        if not os.path.isfile(source_path):
            raise ToolError(f"纹理文件不存在：{source_path}")
        try:
            with open(source_path, "rb") as fh:
                source_data = "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
        except OSError as exc:
            raise ToolError(f"读取纹理失败：{exc}") from exc
    tex = project.add_texture(
        name,
        width=max(1, width),
        height=max(1, height),
        source_path=source_path,
        source_data=source_data,
    )
    session.dirty = True
    return ok("texture_create", {"texture": {"uuid": tex.uuid, "name": tex.name, "size": [tex.width, tex.height]}})


@mcp.tool(description="列出项目全部纹理（uuid/名称/尺寸/是否内嵌）。")
def texture_list() -> dict[str, Any]:
    project = _project()
    return ok(
        "texture_list",
        {
            "textures": [
                {
                    "uuid": t.uuid,
                    "name": t.name,
                    "size": [t.width, t.height],
                    "embedded": bool(t.source_data or t.source_path),
                }
                for t in project.textures
            ]
        },
    )


@mcp.tool(
    description=(
        "给立方体的面贴纹理。faces 传 'all' 或面名列表 north/east/south/west/up/down；"
        "texture 传纹理 uuid 或名称。"
    )
)
@_edit
def texture_assign(
    element: str,
    texture: str,
    faces: list[str] | str = "all",
) -> dict[str, Any]:
    project = _project()
    el = project.element(element)
    tex = project.texture(texture)
    if faces == "all":
        targets = FACE_KEYS
    elif isinstance(faces, str):
        targets = [faces]
    else:
        targets = faces
    applied = []
    for key in targets:
        if key not in FACE_KEYS:
            raise _tool_error(f"未知面 {key!r}", f"可用面：{', '.join(FACE_KEYS)}")
        if key not in el.faces:
            # 缺省把整张画布贴到该面（像素坐标，与 Blockbench 约定一致）
            el.faces[key] = Face(uv=[0.0, 0.0, float(tex.width), float(tex.height)], texture=tex.uuid)
        el.faces[key].texture = tex.uuid
        applied.append(key)
    session.dirty = True
    return ok("texture_assign", {"element": el.name, "texture": tex.name, "faces": applied})


@mcp.tool(
    description=(
        "就地更新纹理：改名/改画布尺寸/更换内嵌 PNG。"
        "color（如 '#RRGGBB'）会生成新的纯色 PNG；source_path 从磁盘 PNG 重新内嵌；"
        "两者都不传则只改元数据；clear_source=True 清除内嵌位图。"
    )
)
@_edit
def texture_update(
    texture: str,
    name: str | None = None,
    width: int | None = None,
    height: int | None = None,
    color: str | None = None,
    source_path: str | None = None,
    clear_source: bool = False,
) -> dict[str, Any]:
    project = _project()
    tex = project.texture(texture)
    source_data: str | None = None
    if color:
        w = max(1, int(width or tex.width))
        h = max(1, int(height or tex.height))
        source_data = _solid_png(w, h, _parse_color(color))
        source_path = None
    elif source_path:
        if not os.path.isfile(source_path):
            raise ToolError(f"纹理文件不存在：{source_path}")
        try:
            with open(source_path, "rb") as fh:
                source_data = "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
        except OSError as exc:
            raise ToolError(f"读取纹理失败：{exc}") from exc
    tex = project.update_texture(
        tex.uuid,
        name=name,
        width=width,
        height=height,
        source_path=source_path,
        source_data=source_data,
        clear_source=clear_source,
    )
    # 同步项目级画布尺寸（.bbmodel 的 resolution），避免 Blockbench 打开时
    # 仍按旧的 project 分辨率显示，导致用户需要手动把 64x64 改成 256x256。
    if width is not None or height is not None:
        project.texture_width = tex.width
        project.texture_height = tex.height
    session.dirty = True
    return ok(
        "texture_update",
        {"texture": {"uuid": tex.uuid, "name": tex.name, "size": [tex.width, tex.height]}},
    )


@mcp.tool(
    description=(
        "删除纹理并清空所有面引用（立方体本身保留）。"
        "注意：被引用的面会变为未贴图，删除前先用 texture_list/quality 确认。"
    )
)
@_edit
def texture_delete(texture: str) -> dict[str, Any]:
    project = _project()
    tex = project.remove_texture(texture)
    session.dirty = True
    return ok("texture_delete", {"deleted": {"uuid": tex.uuid, "name": tex.name}})


@mcp.tool(
    description=(
        "把纹理的内嵌 PNG 原样写到磁盘（path 为 .png 文件路径）。"
        "用于外部工具/图像模型修改后，再通过 texture_update(source_path=...) 导回模型。"
    )
)
def texture_export(texture: str, path: str) -> dict[str, Any]:
    project = _project()
    tex = project.texture(texture)
    data = images_lib.data_url_to_bytes(tex.source_data)
    if data is None and tex.source_path:
        try:
            with open(tex.source_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise ToolError(f"读取纹理源文件失败：{exc}") from exc
    if not data:
        raise ToolError(f"纹理 {tex.name} 没有内嵌位图或可读源文件", "先 texture_create(color=...) 或 source_path 导入")
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        raise ToolError(f"无法写入纹理文件 {path}：{exc}") from exc
    return ok("texture_export", {"texture": tex.name, "path": path, "bytes": len(data)})


# ---------------------------------------------------------------------------
# 几何体
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "创建立方体。from/to 是对角坐标（每个轴 from < to）；parent 为骨骼 uuid/名称，"
        "缺省放在根；origin 默认自动取中心（旋转轴心）。rotation 为 [x,y,z] 度。"
        "texture 缺省时自动使用第一张纹理（非 box_uv 项目）。faces 可精确指定每个面的 UV。"
    )
)
@_edit
def cube_create(
    name: str,
    from_: list[float],
    to: list[float],
    parent: str | None = None,
    origin: list[float] | None = None,
    rotation: list[float] | None = None,
    uv_offset: list[float] | None = None,
    texture: str | None = None,
    faces: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = _project()
    try:
        el = project.add_element(
            name=name,
            from_=from_,
            to=to,
            parent=parent,
            origin=origin,
            rotation=rotation,
            uv_offset=uv_offset,
            texture=texture,
            faces=faces,
        )
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("cube_create", {"element": project.element_info(el)})


@mcp.tool(
    description=(
        "批量创建立方体：一次 MCP 调用建 N 个，用于参数化/脚本化建模。"
        "cubes 是对象数组，每项字段与 cube_create 完全一致："
        "name / from_（也接受 from）/ to / parent / origin / rotation / uv_offset / texture / faces。"
        "stop_on_error=True 时遇到第一个坏项就停下，False 则跳过坏项继续建。"
        "返回 created（每项 index/uuid/name，include_info=True 时给完整 element_info）、"
        "failures（index/name/error）与 total_elements。"
        "atomic=True 时任意失败会回滚整批；默认保留成功项。整批只占一次撤销记录。"
        "把上千个立方体的构建从上千次往返压成一次，是这类程序化建模的主要提速手段。"
    )
)
@_edit
def cubes_create_bulk(
    cubes: list[dict[str, Any]],
    stop_on_error: bool = True,
    include_info: bool = False,
    atomic: bool = False,
) -> dict[str, Any]:
    project = _project()
    if not isinstance(cubes, list) or not cubes:
        raise ToolError("cubes 必须是非空数组")

    created: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, spec in enumerate(cubes):
        if not isinstance(spec, dict):
            failures.append({"index": index, "name": None, "error": "每项必须是对象"})
            if stop_on_error:
                break
            continue
        name = str(spec.get("name") or f"cube_{index}")
        try:
            el = project.add_element(
                name=name,
                from_=spec.get("from_", spec.get("from")),
                to=spec.get("to"),
                parent=spec.get("parent"),
                origin=spec.get("origin"),
                rotation=spec.get("rotation"),
                uv_offset=spec.get("uv_offset"),
                texture=spec.get("texture"),
                faces=spec.get("faces"),
            )
        except (BBError, KeyError, TypeError, ValueError) as exc:
            # 单点失败要能定位到第几项，否则批量接口没法用
            failures.append({"index": index, "name": name, "error": str(exc)})
            if stop_on_error:
                break
            continue
        created.append(
            {"index": index, "element": project.element_info(el)}
            if include_info
            else {"index": index, "uuid": el.uuid, "name": el.name}
        )

    session.dirty = True
    if atomic and failures:
        raise _tool_error(f"批量创建已回滚：index={failures[0]['index']}，{failures[0]['error']}")
    warnings = [f"{len(failures)} 个立方体创建失败，详见 failures"] if failures else None
    return ok(
        "cubes_create_bulk",
        {
            "created": created,
            "created_count": len(created),
            "failed_count": len(failures),
            "failures": failures,
            "total_elements": len(project.elements),
        },
        warnings=warnings,
    )


@mcp.tool(
    description=(
        "修改立方体属性（部分更新）。改名/改包围盒(需同时给 from 与 to)/改轴心/改旋转/改 UV 偏移；"
        "faces 可整体替换逐面 UV（{face:{uv:[x1,y1,x2,y2]}} 或 {uv:{uv,uv_size}}），"
        "texture 可单独给该立方体所有面换纹理。"
    )
)
@_edit
def cube_update(
    cube: str,
    name: str | None = None,
    from_: list[float] | None = None,
    to: list[float] | None = None,
    origin: list[float] | None = None,
    rotation: list[float] | None = None,
    uv_offset: list[float] | None = None,
    texture: str | None = None,
    faces: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = _project()
    try:
        el = project.update_element(
            cube,
            name=name,
            from_=from_,
            to=to,
            origin=origin,
            rotation=rotation,
            uv_offset=uv_offset,
            texture=texture,
            faces=faces,
        )
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("cube_update", {"element": project.element_info(el)})


@mcp.tool(description="删除立方体（只删该元素，不删父骨骼）。")
@_edit
def cube_delete(cube: str) -> dict[str, Any]:
    project = _project()
    try:
        el = project.remove_element(cube)
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("cube_delete", {"deleted": {"uuid": el.uuid, "name": el.name}})


# ---------------------------------------------------------------------------
# 骨骼
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "创建骨骼（组）。动画只能针对骨骼；骨骼原点即动画旋转轴心。"
        "parent 可嵌套（子骨骼跟随父骨骼）。"
    )
)
@_edit
def bone_create(
    name: str,
    parent: str | None = None,
    origin: list[float] | None = None,
    rotation: list[float] | None = None,
) -> dict[str, Any]:
    project = _project()
    try:
        g = project.add_group(name, parent=parent, origin=origin, rotation=rotation)
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("bone_create", {"bone": project.group_info(g)})


@mcp.tool(
    description=(
        "修改骨骼：改名/移动父级(重挂树，会阻止环)/改原点/改旋转。"
        "改名会同步更新已有动画里的骨骼名。"
    )
)
@_edit
def bone_update(
    bone: str,
    name: str | None = None,
    parent: str | None = None,
    origin: list[float] | None = None,
    rotation: list[float] | None = None,
) -> dict[str, Any]:
    project = _project()
    try:
        g = project.update_group(bone, name=name, parent=parent, origin=origin, rotation=rotation)
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("bone_update", {"bone": project.group_info(g)})


@mcp.tool(
    description=(
        "删除骨骼及其整棵子树（子骨骼与其中的立方体一并删除），"
        "相关动画 animator 也会被清理。返回删除数量。"
    )
)
@_edit
def bone_delete(bone: str) -> dict[str, Any]:
    project = _project()
    try:
        removed = project.remove_group(bone)
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("bone_delete", removed)


# ---------------------------------------------------------------------------
# 动画
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "创建动画（Bedrock 风格）。loop: none/loop/hold；length 为动画时长（秒），"
        "添加关键帧时会自动延长；override=True 导出时写 override_previous_animation。"
    )
)
@_edit
def animation_create(
    name: str,
    length: float = 1.0,
    loop: str = "none",
    override: bool = False,
) -> dict[str, Any]:
    project = _project()
    try:
        anim = project.add_animation(name, length=length, loop=loop, override=override)
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok(
        "animation_create",
        {"animation": {"uuid": anim.uuid, "name": anim.name, "length": anim.length, "loop": anim.loop}},
    )


@mcp.tool(
    description=(
        "为某骨骼在指定动画中添加关键帧。channel: rotation/position/scale；values 为 [x,y,z]；"
        "interpolation: linear/step/bezier/catmullrom。同一骨骼同一通道同一时间会覆盖旧帧。"
    )
)
@_edit
def keyframe_add(
    animation: str,
    bone: str,
    channel: str,
    time: float,
    values: list[float],
    interpolation: str = "linear",
) -> dict[str, Any]:
    project = _project()
    try:
        kf = project.add_keyframe(
            animation,
            bone,
            channel,
            time,
            values,
            interpolation=interpolation,
        )
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok(
        "keyframe_add",
        {
            "keyframe": {
                "uuid": kf.uuid,
                "animation": project.animation(animation).name,
                "bone": project.group(bone).name,
                "channel": kf.channel,
                "time": kf.time,
                "values": kf.values,
                "interpolation": kf.interpolation,
            }
        },
    )


@mcp.tool(description="列出全部动画及每根骨骼的关键帧。animation 指定时只看该动画。")
def keyframe_list(animation: str | None = None) -> dict[str, Any]:
    project = _project()
    animations = [project.animation(animation)] if animation else project.animations
    data = []
    for anim in animations:
        entry: dict[str, Any] = {"uuid": anim.uuid, "name": anim.name, "length": anim.length, "bones": {}}
        for bone_uuid, ator in anim.animators.items():
            group = project.find_group_by_uuid(bone_uuid)
            kfs = []
            for kf in sorted(ator.keyframes, key=lambda k: (k.time, k.channel)):
                kfs.append(
                    {
                        "channel": kf.channel,
                        "time": kf.time,
                        "values": kf.values,
                        "interpolation": kf.interpolation,
                    }
                )
            entry["bones"][group.name if group else ator.bone_name] = kfs
        data.append(entry)
    return ok("keyframe_list", {"animations": data})


@mcp.tool(description="批量添加关键帧，一次事务与撤销记录。每项字段同 keyframe_add；任意失败回滚整批并返回失败 index。适合密集动画，避免逐帧调用与快照开销。")
@_edit
def keyframes_add_bulk(keyframes: list[dict[str, Any]], include_ids: bool = False) -> dict[str, Any]:
    project = _project()
    if not keyframes:
        raise _tool_error("keyframes 必须是非空数组")
    created = []
    for index, spec in enumerate(keyframes):
        try:
            kf = project.add_keyframe(
                spec["animation"], spec["bone"], spec["channel"], spec["time"], spec["values"],
                interpolation=spec.get("interpolation", "linear"),
            )
        except (BBError, KeyError, TypeError, ValueError) as exc:
            raise _tool_error(f"关键帧批量编辑已回滚：index={index}，{exc}") from exc
        if include_ids:
            created.append({"index": index, "uuid": kf.uuid})
    return ok("keyframes_add_bulk", {"created_count": len(keyframes), "created": created})


@mcp.tool(description="删除一个关键帧（按骨骼/通道/时间定位）。")
@_edit
def keyframe_remove(
    animation: str,
    bone: str,
    channel: str,
    time: float,
) -> dict[str, Any]:
    project = _project()
    try:
        kf = project.remove_keyframe(animation, bone, channel, time)
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("keyframe_remove", {"removed": {"channel": kf.channel, "time": kf.time}})


# ---------------------------------------------------------------------------
# 纹理绘制（Pillow）
# ---------------------------------------------------------------------------
def _face_uv_rect(
    project: BlockbenchProject,
    element: str,
    face: str,
) -> tuple[Texture, Face, list[float]]:
    """解析 cube×face → (纹理, Face, uv) ；uv 为画布像素坐标矩形。"""
    if face not in FACE_KEYS:
        raise ToolError(f"未知面 {face!r}", f"可用面：{', '.join(FACE_KEYS)}")
    el = project.element(element)
    f = el.faces.get(face)
    if f is None:
        raise ToolError(f"立方体 {el.name} 没有 {face} 面的 UV", "先用 texture_assign 贴纹理或 cube_update 设 UV")
    tex = project.find_texture_by_uuid(f.texture) if f.texture else None
    if tex is None:
        raise ToolError(f"{el.name} 的 {face} 面没有关联纹理")
    return tex, f, [float(v) for v in f.uv]


@mcp.tool(
    description=(
        "在纹理画布上执行绘制。texture 可省略（作用于所有可编辑纹理）。ops 为绘制指令列表："
        "kind=fill(纯色) / linear_gradient(线性渐变) / radial_gradient(径向渐变) / shadow(柔边阴影)。"
        "每条 op 的字段：kind, color=#RRGGBB[AA], blend=set|overlay|multiply|erase, "
        "region={rect:[x,y,w,h] 或 uv:[x1,y1,x2,y2]}（缺省整张画布）。"
        "渐变额外：color1(终点色), angle(线性，0=→,90=↓)；radial 额外 center:[x,y], radius。"
        "shadow 额外：blur(像素), offset:[dx,dy], strength(0~1)。一次调用=一个 undo 事务。"
    )
)
@_edit
def texture_paint(
    ops: list[dict[str, Any]],
    texture: str | None = None,
) -> Any:
    project = _project()
    targets: list[tuple[Texture, PILImage.Image]] = []
    if texture is not None:
        tex = project.texture(texture)
        if not (tex.source_data or (tex.source_path and os.path.isfile(tex.source_path))):
            raise ToolError(f"纹理 {tex.name} 没有可编辑位图", "先 texture_create(color=...) 生成画布")
        targets.append((tex, paint_lib.load_canvas(tex)))
    else:
        targets = _prepare_textures(project)
    if not ops:
        raise ToolError("ops 不能为空", "至少给一条 {kind:'fill', color:'#...'}")
    summary: list[dict[str, Any]] = []
    before = {t.uuid: t.source_data or paint_lib.encode_canvas(image) for t, image in targets}
    for tex, image in targets:
        results = paint_lib.apply_ops(image, ops)
        summary.append({"texture": tex.name, "ops": results})
    changed = _commit_texture_edits(project, targets, label="texture_paint", before=before)
    data = ok(
        "texture_paint",
        {"changed": changed, "ops_summary": summary, "undo": undo_lib.manager.status(project)},
    )
    preview = paint_lib.preview_png(targets[0][1])
    return [data, Image(data=preview, format="png")]


@mcp.tool(
    description=(
        "按立方体某个面精确涂色：先由 Face.uv 换算该面在画布上的像素矩形再绘制。"
        "element 为 cube 名/uuid，face ∈ north/east/south/west/up/down。"
        "shrink 为向内收缩像素数（防邻面串色，建议 0 或 1）。"
        "ops 格式与 texture_paint 相同；不写 region 时自动使用该面的 UV 矩形。"
    )
)
@_edit
def texture_paint_face(
    element: str,
    face: str,
    ops: list[dict[str, Any]],
    texture: str | None = None,
    shrink: int = 0,
) -> Any:
    project = _project()
    tex, _, uv = _face_uv_rect(project, element, face)
    if texture is not None:
        chosen = project.texture(texture)
        if chosen.uuid != tex.uuid:
            raise ToolError(f"面 {element}/{face} 使用纹理 {tex.name}，与传入 {chosen.name} 不一致")
    image = paint_lib.load_canvas(tex)
    before = {tex.uuid: tex.source_data or paint_lib.encode_canvas(image)}
    pad = max(0, int(shrink))
    box = paint_lib.clamp_box(
        image.width,
        image.height,
        uv[0] + pad,
        uv[1] + pad,
        uv[2] - pad,
        uv[3] - pad,
    )
    if box is None:
        raise ToolError("该面 UV 收缩后为空", "检查 shrink 是否过大")
    if not ops:
        raise ToolError("ops 不能为空")
    results = []
    for raw in ops:
        if raw.get("region") is not None:
            raise ToolError("texture_paint_face 不需要 region（自动用面的 UV 矩形）", "去掉 region 字段")
        results.append(paint_lib.apply_op(image, box, paint_lib._parse_op(raw)))
    changed = _commit_texture_edits(
        project,
        [(tex, image)],
        label=f"paint_face:{element}/{face}",
        before=before,
    )
    data = ok(
        "texture_paint_face",
        {
            "changed": changed,
            "element": element,
            "face": face,
            "uv": [box[0], box[1], box[2], box[3]],
            "ops": results,
            "sample": paint_lib.region_pixels(image, box),
            "undo": undo_lib.manager.status(project),
        },
    )
    preview = paint_lib.preview_png(image)
    return [data, Image(data=preview, format="png")]


@mcp.tool(
    description=(
        "按立方体的面组整体上色：side 涂四个侧面(east/north/west/south)，top 涂顶面(up)，"
        "bottom 涂底面(down)。颜色可为 #RRGGBB[AA] 纯色，或 {kind:'linear_gradient',color,color1,angle}"
        " 渐变。自动读取该 cube 每面的 UV 矩形，一次覆盖全部面，避免逐面遗漏。"
        "element 为 cube 名/uuid；blend 默认 set。返回受影响面积与缩略图。"
    )
)
@_edit
def texture_paint_cube(
    element: str,
    side: str | dict[str, Any],
    top: str | dict[str, Any] | None = None,
    bottom: str | dict[str, Any] | None = None,
    blend: str = "set",
) -> Any:
    project = _project()
    el = project.element(element)
    if not el.faces:
        raise ToolError(f"立方体 {el.name} 没有面部 UV", "先用 texture_assign 或 cube_update 设 UV")
    # 收集该 cube 用到的纹理，各载入一次画布
    tex_map: dict[str, tuple[Texture, PILImage.Image]] = {}
    for f in el.faces.values():
        if not f.texture:
            continue
        tex = project.find_texture_by_uuid(f.texture)
        if tex is None:
            continue
        if tex.uuid not in tex_map:
            tex_map[tex.uuid] = (tex, paint_lib.load_canvas(tex))
    if not tex_map:
        raise ToolError(f"立方体 {el.name} 没有关联纹理")

    def base_color(v: str | dict[str, Any]) -> dict[str, Any]:
        return (
            {"kind": "linear_gradient", "color": v.get("color", "#000000"),
             "color1": v.get("color1", v.get("color", "#000000")),
             "angle": v.get("angle", 90.0)} | {"blend": v.get("blend", blend)}
            if isinstance(v, dict) else {"kind": "fill", "color": v, "blend": blend}
        )

    before = {t.uuid: t.source_data or paint_lib.encode_canvas(image) for t, image in tex_map.values()}
    results = []
    assign = {"east": side, "north": side, "west": side, "south": side, "up": top, "down": bottom}
    for face_key, col in assign.items():
        f = el.faces.get(face_key)
        if f is None or col is None or not f.texture:
            continue
        tex = project.find_texture_by_uuid(f.texture)
        if tex is None or tex.uuid not in tex_map:
            continue
        tex_obj, image = tex_map[tex.uuid]
        box = paint_lib.clamp_box(image.width, image.height, *[float(v) for v in f.uv])
        if box is None:
            continue
        results.append(paint_lib.apply_op(image, box, paint_lib._parse_op(base_color(col))))
    changed = _commit_texture_edits(
        project,
        list(tex_map.values()),
        label=f"paint_cube:{el.name}",
        before=before,
    )
    data = ok(
        "texture_paint_cube",
        {
            "changed": changed,
            "element": el.name,
            "ops": results,
            "undo": undo_lib.manager.status(project),
        },
    )
    first_tex, first_img = next(iter(tex_map.values()), (None, None))
    preview = paint_lib.preview_png(first_img) if first_img is not None else b""
    return [data, Image(data=preview, format="png")]


@mcp.tool(
    description=(
        "列出指定立方体各面（或单面）的 UV 像素矩形、尺寸、纹理名与是否越界。"
        "绘制前先用它确认要涂的区域在画布哪里。"
    )
)
def texture_face_map(
    element: str,
    face: str | None = None,
) -> dict[str, Any]:
    project = _project()
    el = project.element(element)
    faces = [face] if face else FACE_KEYS
    rows = []
    for key in faces:
        f = el.faces.get(key)
        if f is None:
            rows.append({"face": key, "uv": None, "texture": None})
            continue
        tex = project.find_texture_by_uuid(f.texture) if f.texture else None
        x0, y0, x1, y1 = [float(v) for v in f.uv]
        inside = tex is not None and 0 <= x0 < tex.width and 0 <= y0 < tex.height and x1 <= tex.width and y1 <= tex.height
        rows.append(
            {
                "face": key,
                "uv": [x0, y0, x1, y1],
                "size": [abs(x1 - x0), abs(y1 - y0)],
                "texture": tex.name if tex else None,
                "out_of_bounds": not inside,
            }
        )
    return ok("texture_face_map", {"element": el.name, "faces": rows})


@mcp.tool(
    description=(
        "校验全部立方体面 UV：越界、非整数、负面积、跨纹理区域重叠。"
        "返回问题清单与画布占用概览，供涂色前排查。"
    )
)
def texture_validate_uv() -> dict[str, Any]:
    project = _project()
    problems = []
    occupancy: dict[str, dict[str, Any]] = {}
    used_textures: set[str] = set()
    for el in project.elements:
        for key, f in el.faces.items():
            tex = project.find_texture_by_uuid(f.texture) if f.texture else None
            if tex is None:
                problems.append({"kind": "no_texture", "element": el.name, "face": key})
                continue
            used_textures.add(tex.uuid)
            uv = [float(v) for v in f.uv]
            x0, y0, x1, y1 = uv
            if x0 != int(x0) or y0 != int(y0) or x1 != int(x1) or y1 != int(y1):
                problems.append({"kind": "non_integer_uv", "element": el.name, "face": key, "uv": uv})
            if x1 <= x0 or y1 <= y0:
                problems.append({"kind": "negative_area", "element": el.name, "face": key, "uv": uv})
            if x0 < 0 or y0 < 0 or x1 > tex.width or y1 > tex.height:
                problems.append(
                    {
                        "kind": "out_of_bounds",
                        "element": el.name,
                        "face": key,
                        "uv": uv,
                        "texture": tex.name,
                        "size": [tex.width, tex.height],
                    }
                )
            rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            entry = occupancy.setdefault(
                tex.uuid,
                {"name": tex.name, "size": [tex.width, tex.height], "regions": []},
            )
            entry["regions"].append(
                {"element": el.name, "face": key, "rect": [rect[0], rect[1], rect[2], rect[3]]}
            )
    for tex in project.textures:
        if tex.uuid not in used_textures:
            problems.append(
                {"kind": "texture_unused", "texture": tex.name, "size": [tex.width, tex.height]}
            )
    return ok("texture_validate_uv", {"problems": problems, "problem_count": len(problems), "canvas": list(occupancy.values())})


# ---------------------------------------------------------------------------
# Undo / Redo
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "回退一次纹理绘制（像素级）：撤销最近一次 texture_paint/texture_paint_face。"
        "仅影响纹理像素；结构性误操作请用 project_undo。"
    )
)
@_edit
def texture_undo() -> dict[str, Any]:
    project = _project()
    try:
        result = undo_lib.manager.pixel_undo_once(project)
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("texture_undo", {**result, "undo": undo_lib.manager.status(project)})


@mcp.tool(description="重做一次被 texture_undo 撤销的纹理绘制。")
@_edit
def texture_redo() -> dict[str, Any]:
    project = _project()
    try:
        result = undo_lib.manager.pixel_redo_once(project)
    except BBError as exc:
        raise _err(exc) from exc
    session.dirty = True
    return ok("texture_redo", {**result, "undo": undo_lib.manager.status(project)})


@mcp.tool(
    description=(
        "回退一次文档级操作：恢复最近一次成功操作（绘制/立方体编辑等）前的完整项目快照。"
        "像素级问题优先用 texture_undo；该工具处理结构误操作。"
    )
)
def project_undo() -> dict[str, Any]:
    project = _project()
    try:
        result = undo_lib.manager.doc_undo_once(project)
    except BBError as exc:
        raise _err(exc) from exc
    session.edited()
    return ok("project_undo", {**result, "undo": undo_lib.manager.status(project)})


@mcp.tool(description="重做一次被 project_undo 撤销的文档级操作。")
def project_redo() -> dict[str, Any]:
    project = _project()
    try:
        result = undo_lib.manager.doc_redo_once(project)
    except BBError as exc:
        raise _err(exc) from exc
    session.edited()
    return ok("project_redo", {**result, "undo": undo_lib.manager.status(project)})


@mcp.tool(description="查看当前 undo/redo 栈状态（像素级与文档级各多少步）。")
def undo_status() -> dict[str, Any]:
    project = _project()
    return ok("undo_status", undo_lib.manager.status(project))


@mcp.tool(description="清空全部 undo/redo 历史（像素级与文档级）。")
def undo_clear() -> dict[str, Any]:
    project = _project()
    undo_lib.manager.clear_all()
    undo_lib.manager.reset_if_project_changed(project)
    return ok("undo_clear", undo_lib.manager.status(project))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "导出模型。target: bbmodel（写 .bbmodel 项目）/ bedrock（几何体 .geo.json，"
        "如有动画则同目录写 .animation.json）/ java_block（Java 方块/物品模型 JSON，"
        "resource_root 控制纹理资源根，如 minecraft:item）/ cem（OptiFine JEM）/ "
        "geckolib（Java 模组 GeckoLib：按 assets/<modid>/geo/... 布局写模型+动画+纹理）。"
        "path 传文件路径；bedrock 也可传目录自动命名。"
        "bounds_mode=animated 时用原生动画采样计算动态可见范围（需 project_sync 与桥接 0.6.0）；sample_rate 控制频率，bounds_margin 为模型单位余量。默认 static。"
    )
)
def export_model(
    target: str,
    path: str,
    resource_root: str = "minecraft:block",
    modid: str = "mymod",
    category: str = "item",
    model_name: str | None = None,
    bounds_mode: str = "static",
    sample_rate: float = 12,
    bounds_margin: float = 16,
) -> dict[str, Any]:
    project = _project()
    target = target.lower()
    try:
        if bounds_mode not in ("static", "animated"):
            raise _tool_error("bounds_mode 必须为 static 或 animated")
        if bounds_mode == "animated" and target not in ("bedrock", "geckolib"):
            raise _tool_error("animated bounds 只适用于 bedrock/geckolib")
        bounds_report = animation_analysis.review_native(project, sample_rate=sample_rate, margin=bounds_margin) if bounds_mode == "animated" else None
        render_bounds = bounds_report["render_bounds"] if bounds_report else None
        if target == "bbmodel":
            bbmodel_codec.write(project, path)
            result = {"path": path, "format": "bbmodel"}
        elif target == "bedrock":
            if os.path.isdir(path):
                base = os.path.join(path, (project.model_identifier or project.name).removeprefix("geometry.") or "model")
                geo_path = base + ".geo.json"
                anim_path = base + ".animation.json"
                data = bedrock_codec.export_combined(project, geo_path, anim_path if project.animations else None, render_bounds)
                result = data
            else:
                anim_path = None
                if project.animations:
                    anim_path = os.path.splitext(path)[0] + ".animation.json"
                result = bedrock_codec.export_combined(project, path, anim_path, render_bounds)
        elif target in ("java_block", "java"):
            result = java_codec.export_java_block(project, path, resource_root=resource_root)
        elif target in ("cem", "optifine_entity", "jem"):
            result = java_codec.export_cem(project, path)
        elif target == "geckolib":
            result = geckolib_codec.export_geckolib(
                project,
                path,
                modid=modid,
                category=category,
                name=model_name,
                render_bounds=render_bounds,
            )
        else:
            raise ToolError(
                f"未知导出目标 {target!r}",
                "可选：bbmodel / bedrock / java_block / cem / geckolib",
            )
    except BBError as exc:
        raise _err(exc) from exc
    except OSError as exc:
        raise ToolError(f"导出失败：{exc}") from exc
    if bounds_report:
        result["animated_bounds"] = {k: bounds_report[k] for k in ("sample_count", "sample_rate", "bounds", "render_bounds", "bounds_margin", "limitations")}
    session.dirty = False if target == "bbmodel" and path == session.save_path else session.dirty
    return ok("export_model", result)


@mcp.tool(
    description=(
        "加载内置模板或样例库。代码模板：sword_geckolib（自研长剑）。"
        "文件样例来自 examples/catalog.json（完整 .bbmodel，非硬编码），"
        "例如 redeemer（你的 GeckoLib 步枪，ARR）与 polar_bear（MIT）。"
        "加载后可用 project_status/validate_quality 查看并继续修改。"
    )
)
def project_load_example(
    template: str = "sword_geckolib",
    name: str | None = None,
) -> dict[str, Any]:
    try:
        project = examples_lib.load(template, name=name)
        entry = examples_lib.sample_info(template)
    except (KeyError, FileNotFoundError) as exc:
        raise ToolError(
            f"未知模板 {template!r}",
            f"可用：{', '.join(examples_lib.available_ids())}",
        ) from exc
    session.project = project
    session.new_identity()
    session.save_path = None
    session.dirty = False
    return ok(
        "project_load_example",
        {
            "template": template,
            "project": project.summary(),
            "outline": project.outline_tree(),
            "license": (entry or {}).get("license"),
            "license_spdx": (entry or {}).get("license_spdx"),
        },
    )


@mcp.tool(
    description=(
        "无视觉建模质检：返回 0-100 评分与问题清单（退化立方体/命名重复/空骨骼/"
        "未贴图/UV 越界/同层穿插/动画引用缺失）。只查可证明的结构问题，不做审美评价；"
        "结合 render_ascii 使用可在不支持看图的模型上完成自检闭环。"
    )
)
def validate_quality(max_issues: int = 60) -> dict[str, Any]:
    project = _project()
    report = quality_checker.quality_report(project, max_issues=max(1, int(max_issues)))
    return ok("validate_quality", report)


# ---------------------------------------------------------------------------
# 渲染预览（视觉反馈闭环）
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "把当前模型渲染成 PNG 供视觉验收。返回文本 JSON（含 image_path）并同时附带图片内容，"
        "支持看图的客户端直接查看。建议每做 2-3 步就调用一次：shape 不对先改几何，再继续。"
        "camera 用 yaw/pitch 控制视角（默认斜 45°/28°）。"
    )
)
def render_preview(
    path: str | None = None,
    width: int = 640,
    height: int = 640,
    yaw: float = 45.0,
    pitch: float = 28.0,
) -> Any:
    project = _project()
    png = preview_renderer.render_png(
        project,
        width=max(64, int(width)),
        height=max(64, int(height)),
        yaw=float(yaw),
        pitch=float(pitch),
    )
    target = path
    if not target:
        folder = os.path.dirname(session.save_path) if session.save_path else os.getcwd()
        stem = project.name or "model"
        target = os.path.join(folder, f"{stem}_preview.png")
    try:
        with open(target, "wb") as fh:
            fh.write(png)
    except OSError as exc:
        raise ToolError(f"无法写入预览文件 {target}：{exc}") from exc
    data = ok(
        "render_preview",
        {
            "image_path": target,
            "bytes": len(png),
            "size": [width, height],
            "camera": {"yaw": yaw, "pitch": pitch},
            "hint": "检查形状后继续建模；若不理想，先 cube_update/cube_delete 修正再渲染",
        },
    )
    return [data, Image(data=png, format="png")]


@mcp.tool(
    description=(
        "生成前/侧/顶三个 ASCII 视图（不支持看图的模型也能读懂形状）："
        "每格用元素首字母标记，Y 向上。适合快速检查部件分布与比例。"
    )
)
def render_ascii(cells: int = 36) -> dict[str, Any]:
    project = _project()
    views = preview_renderer.ascii_views(project, cells=max(12, min(int(cells), 64)))
    return ok("render_ascii", {"views": views, "legend": "每个字符 = 对应部件的首字母，空格 = 无几何体"})


@mcp.tool(
    description=(
        "把纹理画布放大导出成 PNG（双通道：文件 + 图片），供视觉模型检视贴图布局。"
        "scale 为放大倍数（1-16），grid=True 画出原始像素格线便于数格子。"
        "无内嵌位图时返回带提示的占位图。"
    )
)
def render_texture(
    texture: str | None = None,
    path: str | None = None,
    scale: int = 6,
    grid: bool = True,
) -> Any:
    project = _project()
    png, name = preview_renderer.render_texture_sheet(
        project,
        texture,
        scale=max(1, min(int(scale), 16)),
        grid=bool(grid),
    )
    if not name:
        raise ToolError("项目里没有可用纹理", "先 texture_create(color=...) 或 texture_create(source_path=...)")
    target = path
    if not target:
        folder = os.path.dirname(session.save_path) if session.save_path else os.getcwd()
        target = os.path.join(folder, f"{name}_sheet.png")
    try:
        with open(target, "wb") as fh:
            fh.write(png)
    except OSError as exc:
        raise ToolError(f"无法写入纹理预览 {target}：{exc}") from exc
    data = ok(
        "render_texture",
        {
            "texture": name,
            "image_path": target,
            "bytes": len(png),
            "scale": max(1, min(int(scale), 16)),
            "grid": bool(grid),
            "hint": "核对贴图布局；如需改贴图先用 texture_export 导出，外部改好后 texture_update(source_path=...) 导回",
        },
    )
    return [data, Image(data=png, format="png")]


@mcp.tool(
    description=(
        "查询 Blockbench 插件桥（二期）健康状态：Blockbench 是否打开、插件是否加载、"
        "当前打开的项目与格式。需要先完成：Blockbench 打开 -> File > Plugins > Load from File "
        "选择 plugin/blockbench_mcp_bridge.js。"
    )
)
def blockbench_health() -> dict[str, Any]:
    try:
        payload = RemoteDriver().health()
    except BBError as exc:
        raise _err(exc) from exc
    return ok("blockbench_health", payload)


@mcp.tool(
    description=(
        "确保 Blockbench 插件桥可用：先探 /health，通了直接返回；不通则拉起 Blockbench"
        "（默认 D:\\Blockbench\\Blockbench.exe，可用 exe 参数或环境变量 BLOCKBENCH_EXE 指定），"
        "启动时清掉 ELECTRON_RUN_AS_NODE（否则 Electron 会把 Blockbench 当 node 跑、界面不出现），"
        "然后轮询 /health 直到就绪。用来避免'截图/打开前还得手动先开 Blockbench'。"
        "注意：首次仍需在 Blockbench 里手动加载一次插件桥；wait_seconds 控制等待上限。"
    )
)
def blockbench_ensure_running(
    exe: str | None = None,
    wait_seconds: float = 30.0,
) -> dict[str, Any]:
    try:
        payload = ensure_blockbench_running(exe, wait_seconds=max(1.0, float(wait_seconds)))
    except BBError as exc:
        raise _err(exc) from exc
    return ok("blockbench_ensure_running", payload)


@mcp.tool(
    description=(
        "请求 Blockbench 对当前视口截图（双通道：文件 + 图片），用于真实材质/光照验收。"
        "path 不传时写 session 保存目录。返回值里的 viewport 带上视口当前项目的 "
        "save_path / elements（立方体数）/ blockbench_version —— 用来证明这张图到底是哪一版模型。"
        "expect_path / expect_elements 可选：与视口实际状态不符时直接报错，"
        "避免把旧模型的截图当成验收依据。"
        "要求 Blockbench 已打开且已加载插件桥；若目标 .bbmodel 由 MCP 保存后未在 Blockbench 打开，"
        "先 blockbench_command(open, {path}) 刷新；Blockbench 没开就先 blockbench_ensure_running。"
    )
)
def blockbench_screenshot(
    path: str | None = None,
    expect_path: str | None = None,
    expect_elements: int | None = None,
    expect_revision: int | None = None,
) -> Any:
    project = _project()
    if not path:
        folder = os.path.dirname(session.save_path) if session.save_path else os.getcwd()
        path = os.path.join(folder, f"{project.name}_blockbench.png")
    driver = RemoteDriver()
    try:
        # Capture provenance and pixels in the same native operation.
        params = {"expect_path": expect_path, "expect_elements": expect_elements}
        params = {key: value for key, value in params.items() if value is not None}
        if expect_revision is not None:
            params.update(expect_revision=expect_revision, expect_project_id=session.project_id)
            if session.source_token:
                params["expect_source_token"] = session.source_token
        viewport = driver.capture(path, params)
        written = path
        with open(written, "rb") as fh:
            png = fh.read()
    except BBError as exc:
        raise _err(exc) from exc
    except OSError as exc:
        raise ToolError(f"无法读写截图 {path}：{exc}") from exc

    viewport.setdefault("project", viewport.get("name"))
    mismatch: list[str] = []
    if expect_path and not same_path(viewport["save_path"], expect_path):
        mismatch.append(f"期望 save_path={expect_path}，视口实际 {viewport['save_path'] or '(空)'}")
    if expect_elements is not None and viewport["elements"] != expect_elements:
        mismatch.append(f"期望 elements={expect_elements}，视口实际 {viewport['elements']}")
    if expect_revision is not None and (viewport.get("revision") != expect_revision or viewport.get("project_id") != session.project_id):
        mismatch.append("视口工程版本与当前 MCP 会话不匹配")
    if mismatch:
        raise _tool_error(
            "截图与期望不符，视口里很可能还是旧模型："
            + "；".join(mismatch)
            + f"（图已写到 {written}，可直接查看实际内容）",
            "同一个 save_path 的 .bbmodel 在 Blockbench 里已经打开时不会自动重读磁盘；"
            "先 project_sync 同步当前工程，再截图",
        )

    data = ok(
        "blockbench_screenshot",
        {
            "image_path": written,
            "bytes": len(png),
            "viewport": viewport,
            "hint": "查看真实渲染；不满意就在 Blockbench 调视角/贴图后重截，或回到 MCP 工具修改几何",
        },
    )
    return [data, Image(data=png, format="png")]


@mcp.tool(
    description=(
        "向 Blockbench 插件桥发送命令。method 支持："
        "probe() 只读体检（报告真实可用的打开/截图/撤销 API，真机排障首选）；"
        "open(path=...) 打开/重载 .bbmodel（Blockbench.read -> 官方 project codec）。"
        "open 先验证文件，新工程/纹理就绪后才异步关闭同路径旧工程；默认保护未保存修改。"
        "replace_unsaved=true 才允许替换旧工程的未保存修改；"
        "若自证不通过会返回 ok=false + stale=true（否则会静默地让你对着旧模型继续验收）；"
        "force_close=false 可禁止它关掉旧项目（此时遇到同路径项目会直接报 stale）；"
        "reload() 同 open；reload_self() 让插件从磁盘热重载；"
        "undo() 撤销活动项目最近一步；capture() 原子截图；eval(code=...) 执行 Blockbench 脚本（需插件侧白名单开启）。"
        "open 用于把 MCP 刚保存的 .bbmodel 同步进 Blockbench 视口，再截图验收。"
    )
)
def blockbench_command(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if method not in ("probe", "open", "reload", "reload_self", "eval", "undo", "capture", "animation_sample"):
        raise _tool_error(
            f"未知命令 {method!r}",
            "可用：probe / open / reload / reload_self / eval / undo / capture / animation_sample",
        )
    try:
        payload = RemoteDriver().command(method, params or {})
    except BBError as exc:
        raise _err(exc) from exc
    # 插件如实报错时不要包成 ok=true ——那正是「open 报成功但视口是旧模型」的老问题
    if payload.get("ok") is False:
        raise _tool_error(
            f"Blockbench 命令 {method} 未成功：{payload.get('error') or payload.get('result') or payload}",
            payload.get("hint")
            or "open 报 stale 说明视口里仍是旧模型：确认 path 正确，"
            "必要时先关掉 Blockbench 里同路径的项目再重试",
        )
    return ok("blockbench_command", {"method": method, **payload})


@mcp.tool(description="原生 Blockbench 动画采样（需 project_sync 和桥接 0.6.0）。返回真实 Cuboid 动态包围盒、地面穿透与骨骼变换；不修改工程或播放状态。一次最多 128 个 {animation,time} 姿态。")
def animation_sample(samples: list[dict[str, Any]], include_bones: bool = True,
                     include_elements: bool = False, floor: float = 0, floor_tolerance: float = .05,
                     contact_exclude_prefixes: list[str] | None = None) -> dict[str, Any]:
    try:
        return ok("animation_sample", animation_analysis.sample_native(samples, include_bones=include_bones,
                  include_elements=include_elements, floor=floor, floor_tolerance=floor_tolerance,
                  contact_exclude_prefixes=contact_exclude_prefixes))
    except BBError as exc:
        raise _err(exc) from exc


@mcp.tool(description="动态动画验收（原生采样，不改模型）：检查 loop 首尾、指定 from/to 衔接、地面穿透，并计算可用于导出的动态 visible_bounds。animations 默认全部；sample_rate 0.1..60，最多 5000 姿态。报告速度差而不做审美评分。")
def animation_review(animations: list[str] | None = None, transitions: list[dict[str, str]] | None = None,
                     sample_rate: float = 12, bounds_margin: float = 16,
                     floor: float = 0, floor_tolerance: float = .05,
                     contact_exclude_prefixes: list[str] | None = None) -> dict[str, Any]:
    try:
        return ok("animation_review", animation_analysis.review_native(_project(), animations, transitions,
                  sample_rate, bounds_margin, floor, floor_tolerance, contact_exclude_prefixes))
    except BBError as exc:
        raise _err(exc) from exc


@mcp.tool(description="按指定动画/时间截取原生 PNG，并用 hero/front/rear/side/top 相机预设自动取景。采样、取景、版本凭证和截图在同一桥接操作内完成，随后恢复原视角/姿态。先 project_sync；可传 review 的 bounds 固定多帧构图。")
def animation_preview(animation: str, path: str, time: float = 0,
                      camera: str = "hero", fit_bounds: dict[str, list[float]] | None = None) -> Any:
    try:
        params = animation_analysis.native_proof()
        params.update(animation=animation, time=finite_number(time, "time", minimum=0), camera=camera)
        if fit_bounds is not None:
            params["fit_bounds"] = fit_bounds
        viewport = RemoteDriver(timeout=30).capture(path, params)
        with open(path, "rb") as fh:
            png = fh.read()
        return [ok("animation_preview", {"image_path": path, "bytes": len(png), "viewport": viewport}), Image(data=png, format="png")]
    except BBError as exc:
        raise _err(exc) from exc
    except OSError as exc:
        raise ToolError(f"动画预览写入失败：{exc}") from exc


@mcp.tool(description="原生动画 GIF 预览：传入动画名称序列（可以重复，如 takeoff/fly/fly/landing），固定相机与动态构图范围，保持实际时长并恢复每帧前的状态。需 project_sync；fps 1..20、size 64..768，最多 180 帧。文件完整生成后才替换输出。")
def animation_render(animations: list[str], path: str, fps: int = 6, size: int = 512,
                     camera: str = "hero", loop: bool = False) -> dict[str, Any]:
    try:
        return ok("animation_render", animation_analysis.render_native(_project(), animations, path, fps, size, camera, loop))
    except BBError as exc:
        raise _err(exc) from exc
    except OSError as exc:
        raise ToolError(f"动画 GIF 写入失败：{exc}") from exc


def get_server() -> FastMCP:
    """供 main.py / 测试使用。"""
    return mcp
