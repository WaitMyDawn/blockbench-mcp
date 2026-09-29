"""内存文档模型：不依赖 Blockbench，纯 Python 维护一个 .bbmodel 项目。

设计要点（对应 Blockbench 5.0 的 bbmodel 结构）：
* elements 存所有立方体，groups 存骨骼/组，outliner 只存层级（uuid 引用）
* face.texture 内部存纹理 uuid，序列化时转成纹理数组下标
* 动画 animators 以骨骼 uuid 为键，keyframes 平铺并按 channel/time 去重
"""

from __future__ import annotations

import math
import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import NotFoundError, StateError, ValidationError

Vec3 = list[float]

# Blockbench 认识的内置格式 id（meta.model_format）
SUPPORTED_FORMATS: dict[str, str] = {
    "bedrock": "Minecraft Bedrock Entity",
    "java_block": "Minecraft Java Block/Item",
    "optifine_entity": "OptiFine JEM (CEM)",
    "geckolib_model": "GeckoLib Model (Blockbench plugin)",
    "modded_entity": "Modded Java Entity",
    "skin": "Minecraft Skin",
    "optifine_part": "OptiFine Part",
    "generic": "Generic Model",
    "free": "Free Model",
}

FACE_KEYS = ("north", "east", "south", "west", "up", "down")


def new_uuid() -> str:
    return str(uuid_lib.uuid4())


def finite_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    """Validate numbers before mutating a document; JSON cannot represent NaN/Inf."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError(f"{name} 必须是有限数值，收到 {value!r}") from None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        suffix = f"且不小于 {minimum:g}" if minimum is not None else ""
        raise ValidationError(f"{name} 必须是有限数值{suffix}，收到 {value!r}")
    return number


def _as_vec3(value: Any, name: str) -> Vec3:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValidationError(f"{name} 必须是长度为 3 的数值数组，收到 {value!r}")
    out: Vec3 = []
    for v in value:
        out.append(finite_number(v, name))
    return out


def _as_vec2(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise ValidationError(f"{name} 必须是长度为 2 的数值数组，收到 {value!r}")
    return [finite_number(v, name) for v in value[:2]]


def _vec_all_zero(v: Vec3) -> bool:
    return all(abs(x) < 1e-9 for x in v)


def _round(v: float) -> float:
    """导出时把浮点误差抹掉，便于阅读与比对。"""
    r = round(v, 4)
    return int(r) if r.is_integer() else r


def vec3_to_json(v: Vec3) -> list[Any]:
    return [_round(x) for x in v]


@dataclass
class Face:
    """单个面的 UV 矩形（bbmodel 中为 4 元素 [x1, y1, x2, y2]）。"""

    uv: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    texture: str | None = None  # 纹理 uuid；None 表示不贴图
    rotation: int = 0

    def size(self) -> list[float]:
        return [abs(self.uv[2] - self.uv[0]), abs(self.uv[3] - self.uv[1])]


@dataclass
class Element:
    """立方体元素。from/to 为两个对角点，origin 为旋转轴心。"""

    uuid: str
    name: str
    from_: Vec3
    to: Vec3
    faces: dict[str, Face] = field(default_factory=dict)
    origin: Vec3 = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: Vec3 = field(default_factory=lambda: [0.0, 0.0, 0.0])
    uv_offset: list[float] = field(default_factory=lambda: [0.0, 0.0])
    box_uv: bool = False
    visibility: bool = True
    export: bool = True
    shade: bool = True

    def size(self) -> Vec3:
        return [self.to[i] - self.from_[i] for i in range(3)]


@dataclass
class Group:
    """骨骼/组。children 里的 uuid 指向 Element 或 Group。"""

    uuid: str
    name: str
    children: list[str] = field(default_factory=list)
    origin: Vec3 = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: Vec3 = field(default_factory=lambda: [0.0, 0.0, 0.0])
    visibility: bool = True
    export: bool = True
    color: int = 0
    is_open: bool = True


@dataclass
class Texture:
    """纹理元数据。source_path 存在时序列化为内嵌 PNG data URL。"""

    uuid: str
    name: str
    width: int = 16
    height: int = 16
    folder: str = ""
    namespace: str = ""
    id: str = "0"
    particle: bool = False
    source_path: str | None = None  # 磁盘 PNG 路径
    source_data: str | None = None  # data:image/png;base64,...
    pbr_channel: str = "color"


@dataclass
class Keyframe:
    uuid: str
    channel: str
    time: float
    values: Vec3
    interpolation: str = "linear"  # linear / step / bezier / catmullrom


@dataclass
class Animator:
    """一根骨骼在一个动画里的通道集合。keyframes 平铺存储。"""

    bone_uuid: str
    bone_name: str
    keyframes: list[Keyframe] = field(default_factory=list)

    def keyframes_of(self, channel: str) -> list[Keyframe]:
        return sorted((k for k in self.keyframes if k.channel == channel), key=lambda k: k.time)


@dataclass
class Animation:
    uuid: str
    name: str
    length: float = 1.0
    loop: str = "none"  # none / loop / hold
    override: bool = False
    snapping: int = 24
    animators: dict[str, Animator] = field(default_factory=dict)


def box_uv_faces(
    size: Vec3,
    uv_offset: Vec3 | None = None,
    texture_uuid: str | None = None,
) -> dict[str, Face]:
    """按 Blockbench 的展开习惯生成 6 个面的 UV。

    布局（与 Blockbench 默认一致）：
      顶/底两横条在上，四面侧壁横排在下：
      east | north | west | south   （侧壁，v = sz .. sz+sy）
      top 在 north 上方，bottom 在 west 上方（v = 0 .. sz）
    单位默认按 1 模型单位 = 1 纹理像素，可用 uv_offset 平移。
    """
    sx, sy, sz = size
    ox, oy = uv_offset or [0.0, 0.0]

    def rect(x1: float, y1: float, x2: float, y2: float) -> Face:
        return Face(
            uv=[round(ox + x1, 4), round(oy + y1, 4), round(ox + x2, 4), round(oy + y2, 4)],
            texture=texture_uuid,
        )

    return {
        "east": rect(0.0, sz, sx, sz + sy),
        "north": rect(sx, sz, 2 * sx, sz + sy),
        "west": rect(2 * sx, sz, 3 * sx, sz + sy),
        "south": rect(3 * sx, sz, 4 * sx, sz + sy),
        "up": rect(sx, 0.0, 2 * sx, sz),
        "down": rect(2 * sx, 0.0, 3 * sx, sz),
    }


def parse_faces(data: dict[str, Any]) -> dict[str, Face]:
    faces: dict[str, Face] = {}
    for key in FACE_KEYS:
        raw = (data or {}).get(key)
        if not raw:
            continue
        uv = raw.get("uv") or [0, 0]
        if len(uv) == 2:  # 兼容只有起点的旧文件，补成矩形
            uv = [uv[0], uv[1], uv[0], uv[1]]
        faces[key] = Face(
            uv=[finite_number(x, "uv") for x in uv[:4]],
            texture=raw.get("texture") if isinstance(raw.get("texture"), str) else None,
            rotation=int(raw.get("rotation") or 0),
        )
    return faces


def parse_faces_spec(
    spec: dict[str, Any],
    texture_uuid: str | None,
) -> dict[str, Face]:
    """把 cube_create/cube_update 的 faces 参数解析为 Face 字典。

    接受两种写法：
      {face: {uv: [x1, y1, x2, y2]}}
      {face: {uv: {uv: [x, y], uv_size: [w, h]}}}
    """
    parsed: dict[str, Face] = {}
    for key, raw in spec.items():
        if key not in FACE_KEYS:
            raise ValidationError(f"未知面 {key!r}", f"可用面：{', '.join(FACE_KEYS)}")
        if isinstance(raw, dict) and "uv" in raw and isinstance(raw["uv"], (list, tuple)) and len(raw["uv"]) == 4:
            parsed[key] = Face(uv=[finite_number(x, "uv") for x in raw["uv"][:4]], texture=texture_uuid)
        elif isinstance(raw, dict) and isinstance(raw.get("uv"), dict):
            rect = raw["uv"]
            u = _as_vec2(rect.get("uv", [0, 0]), "uv")
            s = _as_vec2(rect.get("uv_size", [0, 0]), "uv_size")
            parsed[key] = Face(
                uv=[u[0], u[1], finite_number(u[0] + s[0], "uv"), finite_number(u[1] + s[1], "uv")],
                texture=texture_uuid,
            )
        else:
            raise ValidationError(
                f"面 {key!r} 格式无法识别",
                "使用 {uv:[x1,y1,x2,y2]} 或 {uv:{uv,uv_size}}",
            )
    return parsed


def _find_first(items: Iterable[Any], uuid_or_name: str, kind: str) -> Any:
    for item in items:
        if item.uuid == uuid_or_name or item.name == uuid_or_name:
            return item
    raise NotFoundError(
        f"找不到 {kind}：{uuid_or_name!r}",
        f"先调用 project_status 查看现有 {kind} 的 uuid/name 再重试",
    )


class BlockbenchProject:
    """一个完整的 Blockbench 项目（bbmodel 5.0 语义）。"""

    FORMAT_VERSION = "5.0"

    def __init__(
        self,
        name: str = "untitled",
        format_id: str = "bedrock",
        texture_width: int = 64,
        texture_height: int = 64,
        box_uv: bool = False,
    ) -> None:
        if format_id not in SUPPORTED_FORMATS:
            raise ValidationError(
                f"不支持的格式 {format_id!r}",
                f"可用：{', '.join(SUPPORTED_FORMATS)}",
            )
        self.name = name or "untitled"
        self.format_id = format_id
        self.texture_width = max(1, int(texture_width))
        self.texture_height = max(1, int(texture_height))
        self.box_uv = bool(box_uv)
        self.model_identifier = ""
        self.visible_box: Vec3 = [1.0, 1.0, 0.0]
        self.credit = ""
        self.parent = ""  # Java block 的 parent 字段
        self.elements: list[Element] = []
        self.groups: list[Group] = []
        self.textures: list[Texture] = []
        self.animations: list[Animation] = []
        # 顶层层级（Group uuid 或 Element uuid），顺序即大纲顺序
        self.root_children: list[str] = []
        # 从外部文件读入时保留的未知顶层字段，写回时原样保留
        self.extra_root_fields: dict[str, Any] = {}

    # ---------- 查找 ----------
    def element(self, uuid_or_name: str) -> Element:
        return _find_first(self.elements, uuid_or_name, "元素 cube")

    def group(self, uuid_or_name: str) -> Group:
        return _find_first(self.groups, uuid_or_name, "骨骼 bone/group")

    def texture(self, uuid_or_name: str) -> Texture:
        return _find_first(self.textures, uuid_or_name, "纹理 texture")

    def animation(self, uuid_or_name: str) -> Animation:
        return _find_first(self.animations, uuid_or_name, "动画 animation")

    def find_element_by_uuid(self, uid: str) -> Element | None:
        for e in self.elements:
            if e.uuid == uid:
                return e
        return None

    def find_group_by_uuid(self, uid: str) -> Group | None:
        for g in self.groups:
            if g.uuid == uid:
                return g
        return None

    def find_texture_by_uuid(self, uid: str) -> Texture | None:
        for t in self.textures:
            if t.uuid == uid:
                return t
        return None

    def parent_group_of(self, uid: str) -> Group | None:
        for g in self.groups:
            if uid in g.children:
                return g
        return None

    def _detach(self, uid: str) -> None:
        if uid in self.root_children:
            self.root_children.remove(uid)
        for g in self.groups:
            if uid in g.children:
                g.children.remove(uid)

    def _attach(self, uid: str, parent: Group | None, index: int | None = None) -> None:
        if parent is None:
            if index is None:
                self.root_children.append(uid)
            else:
                self.root_children.insert(index, uid)
        else:
            if index is None:
                parent.children.append(uid)
            else:
                parent.children.insert(index, uid)

    # ---------- 纹理 ----------
    def add_texture(
        self,
        name: str,
        width: int = 16,
        height: int = 16,
        *,
        source_path: str | None = None,
        source_data: str | None = None,
        folder: str = "",
        namespace: str = "",
    ) -> Texture:
        if not name:
            raise ValidationError("纹理 name 不能为空")
        tex = Texture(
            uuid=new_uuid(),
            name=name,
            width=max(1, int(width)),
            height=max(1, int(height)),
            source_path=source_path,
            source_data=source_data,
            folder=folder,
            namespace=namespace,
        )
        self.textures.append(tex)
        return tex

    def remove_texture(self, uuid_or_name: str) -> Texture:
        tex = self.texture(uuid_or_name)
        self.textures.remove(tex)
        # 清理元素面上的引用
        for el in self.elements:
            for face in el.faces.values():
                if face.texture == tex.uuid:
                    face.texture = None
        return tex

    def update_texture(
        self,
        uuid_or_name: str,
        *,
        name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        source_path: str | None = None,
        source_data: str | None = None,
        clear_source: bool = False,
    ) -> Texture:
        """就地更新纹理元数据或内嵌位图；返回该纹理。"""
        tex = self.texture(uuid_or_name)
        if name is not None:
            if not str(name).strip():
                raise ValidationError("纹理 name 不能为空")
            tex.name = str(name).strip()
        if width is not None:
            tex.width = max(1, int(width))
        if height is not None:
            tex.height = max(1, int(height))
        if clear_source:
            tex.source_path = None
            tex.source_data = None
        else:
            if source_path is not None:
                tex.source_path = str(source_path)
            if source_data is not None:
                tex.source_data = source_data
        return tex

    # ---------- 立方体 ----------
    def add_element(
        self,
        name: str,
        from_: Vec3,
        to: Vec3,
        *,
        allow_degenerate: bool = False,
        parent: str | None = None,
        origin: Vec3 | None = None,
        rotation: Vec3 | None = None,
        texture: str | None = None,
        uv_offset: Vec3 | None = None,
        faces: dict[str, Any] | None = None,
    ) -> Element:
        f = _as_vec3(from_, "from")
        t = _as_vec3(to, "to")
        size = [t[i] - f[i] for i in range(3)]
        if any(s < -1e-6 for s in size):
            raise ValidationError("立方体尺寸必须为正", "请检查 from/to 是否 from < to")
        if not allow_degenerate and any(s <= 1e-6 for s in size):
            raise ValidationError(
                "立方体某轴尺寸为 0（退化面）",
                "建模工具不接受零厚度；如需从文件读取请走 project_open（自动兼容）",
            )

        parent_group: Group | None = None
        if parent is not None:
            parent_group = self.group(parent)

        texture_uuid: str | None = None
        if texture is not None:
            texture_uuid = self.texture(texture).uuid
        elif not self.box_uv and self.textures:
            texture_uuid = self.textures[0].uuid  # 默认贴到第一张纹理

        el = Element(
            uuid=new_uuid(),
            name=name or "cube",
            from_=f,
            to=t,
            box_uv=self.box_uv,
            origin=_as_vec3(origin, "origin") if origin is not None else [(f[i] + t[i]) / 2 for i in range(3)],
            rotation=_as_vec3(rotation or [0, 0, 0], "rotation"),
            uv_offset=_as_vec2(uv_offset or [0, 0], "uv_offset"),
        )
        if faces:
            el.faces = parse_faces_spec(faces, texture_uuid)
        else:
            el.faces = box_uv_faces(size, uv_offset=el.uv_offset, texture_uuid=texture_uuid)

        self.elements.append(el)
        self._attach(el.uuid, parent_group)
        return el

    def update_element(
        self,
        uuid_or_name: str,
        *,
        name: str | None = None,
        from_: Vec3 | None = None,
        to: Vec3 | None = None,
        origin: Vec3 | None = None,
        rotation: Vec3 | None = None,
        uv_offset: Vec3 | None = None,
        texture: str | None = None,
        faces: dict[str, Any] | None = None,
    ) -> Element:
        el = self.element(uuid_or_name)
        if name is not None:
            el.name = name
        if from_ is not None and to is not None:
            f, t = _as_vec3(from_, "from"), _as_vec3(to, "to")
            if any(t[i] <= f[i] for i in range(3)):
                raise ValidationError("from 必须小于 to")
            el.from_, el.to = f, t
        elif from_ is not None or to is not None:
            raise ValidationError("修改包围盒时必须同时提供 from 与 to")
        if origin is not None:
            el.origin = _as_vec3(origin, "origin")
        if rotation is not None:
            el.rotation = _as_vec3(rotation, "rotation")
        if uv_offset is not None:
            el.uv_offset = _as_vec2(uv_offset, "uv_offset")
        if faces is not None:
            texture_uuid = self.texture(texture).uuid if texture is not None else next(
                (f.texture for f in el.faces.values() if f.texture), None
            )
            el.faces = parse_faces_spec(faces, texture_uuid)
        elif texture is not None:
            texture_uuid = self.texture(texture).uuid
            for face in el.faces.values():
                face.texture = texture_uuid
        return el

    def remove_element(self, uuid_or_name: str) -> Element:
        el = self.element(uuid_or_name)
        self.elements.remove(el)
        self._detach(el.uuid)
        return el

    # ---------- 骨骼 / 组 ----------
    def add_group(
        self,
        name: str,
        *,
        parent: str | None = None,
        origin: Vec3 | None = None,
        rotation: Vec3 | None = None,
    ) -> Group:
        parent_group: Group | None = None
        if parent is not None:
            parent_group = self.group(parent)
        g = Group(
            uuid=new_uuid(),
            name=name or "group",
            origin=_as_vec3(origin, "origin") if origin is not None else [0.0, 0.0, 0.0],
            rotation=_as_vec3(rotation or [0, 0, 0], "rotation"),
        )
        self.groups.append(g)
        self._attach(g.uuid, parent_group)
        return g

    def _subtree_uuids(self, group: Group) -> set[str]:
        out: set[str] = {group.uuid}
        for uid in list(group.children):
            child_group = self.find_group_by_uuid(uid)
            if child_group:
                out |= self._subtree_uuids(child_group)
            else:
                out.add(uid)
        return out

    def reparent_group(self, group: Group, new_parent: Group | None) -> None:
        if new_parent is not None and new_parent.uuid in self._subtree_uuids(group):
            raise ValidationError("不能把骨骼移动到自己的子树里", "换个父骨骼试试")
        self._detach(group.uuid)
        self._attach(group.uuid, new_parent)

    def update_group(
        self,
        uuid_or_name: str,
        *,
        name: str | None = None,
        parent: str | None | object = None,
        origin: Vec3 | None = None,
        rotation: Vec3 | None = None,
    ) -> Group:
        g = self.group(uuid_or_name)
        if name is not None:
            old = g.name
            g.name = name
            # 同步动画 animator 的名字，保证导出名字一致
            for anim in self.animations:
                animator = anim.animators.get(g.uuid)
                if animator and animator.bone_name == old:
                    animator.bone_name = name
        if parent is not None:
            parent_group = self.group(parent) if isinstance(parent, str) else parent
            self.reparent_group(g, parent_group)
        if origin is not None:
            g.origin = _as_vec3(origin, "origin")
        if rotation is not None:
            g.rotation = _as_vec3(rotation, "rotation")
        return g

    def remove_group(self, uuid_or_name: str) -> dict[str, int]:
        g = self.group(uuid_or_name)
        doomed = self._subtree_uuids(g)
        doomed_groups = [x for x in self.groups if x.uuid in doomed]
        doomed_elements = [e for e in self.elements if e.uuid in doomed]
        for el in doomed_elements:
            self.elements.remove(el)
        for grp in doomed_groups:
            self.groups.remove(grp)
        self._detach(g.uuid)
        for anim in self.animations:
            for uid in doomed:
                anim.animators.pop(uid, None)
        return {"removed_groups": len(doomed_groups), "removed_elements": len(doomed_elements)}

    # ---------- 动画 ----------
    def add_animation(
        self,
        name: str,
        *,
        length: float = 1.0,
        loop: str = "none",
        override: bool = False,
    ) -> Animation:
        if loop not in ("none", "loop", "hold"):
            raise ValidationError("loop 只能是 none / loop / hold")
        anim = Animation(
            uuid=new_uuid(),
            name=name or "animation",
            length=finite_number(length, "length", minimum=0),
            loop=loop,
            override=bool(override),
        )
        self.animations.append(anim)
        return anim

    def add_keyframe(
        self,
        animation_uuid_or_name: str,
        bone_uuid_or_name: str,
        channel: str,
        time: float,
        values: Vec3,
        *,
        interpolation: str = "linear",
    ) -> Keyframe:
        anim = self.animation(animation_uuid_or_name)
        group = self.group(bone_uuid_or_name)
        if channel not in ("position", "rotation", "scale"):
            raise ValidationError(f"channel 只能是 position/rotation/scale，收到 {channel!r}")
        if interpolation not in ("linear", "step", "bezier", "catmullrom"):
            raise ValidationError(
                f"interpolation {interpolation!r} 不支持",
                "可用：linear / step / bezier / catmullrom",
            )
        vals = _as_vec3(values, "values")
        t = finite_number(time, "time", minimum=0)
        animator = anim.animators.setdefault(
            group.uuid,
            Animator(bone_uuid=group.uuid, bone_name=group.name),
        )
        animator.bone_name = group.name
        # 同一骨骼同一通道同一时间只保留一个关键帧（与 Blockbench 行为一致）
        animator.keyframes = [
            k for k in animator.keyframes
            if not (k.channel == channel and abs(k.time - t) < 1e-4)
        ]
        kf = Keyframe(uuid=new_uuid(), channel=channel, time=t, values=vals, interpolation=interpolation)
        animator.keyframes.append(kf)
        anim.length = max(anim.length, t)
        return kf

    def remove_keyframe(
        self,
        animation_uuid_or_name: str,
        bone_uuid_or_name: str,
        channel: str,
        time: float,
    ) -> Keyframe:
        time = finite_number(time, "time", minimum=0)
        anim = self.animation(animation_uuid_or_name)
        group = self.group(bone_uuid_or_name)
        animator = anim.animators.get(group.uuid)
        if not animator:
            raise NotFoundError("该骨骼在此动画中没有关键帧", "检查骨骼名/动画名后重试")
        for kf in animator.keyframes:
            if kf.channel == channel and abs(kf.time - time) < 1e-4:
                animator.keyframes.remove(kf)
                return kf
        raise NotFoundError(f"找不到关键帧 {channel}@{time}", "用 keyframe_list 查一下现有时间点")

    def remove_animation(self, uuid_or_name: str) -> Animation:
        anim = self.animation(uuid_or_name)
        self.animations.remove(anim)
        return anim

    # ---------- 摘要 ----------
    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "format": self.format_id,
            "format_label": SUPPORTED_FORMATS[self.format_id],
            "bbmodel_format_version": self.FORMAT_VERSION,
            "texture_size": [self.texture_width, self.texture_height],
            "box_uv": self.box_uv,
            "model_identifier": self.model_identifier,
            "counts": {
                "elements": len(self.elements),
                "groups": len(self.groups),
                "textures": len(self.textures),
                "animations": len(self.animations),
                "keyframes": sum(
                    len(a.keyframes) for a in sum((list(an.animators.values()) for an in self.animations), [])
                ),
            },
        }

    def element_info(self, el: Element) -> dict[str, Any]:
        parent = self.parent_group_of(el.uuid)
        return {
            "uuid": el.uuid,
            "name": el.name,
            "type": "cube",
            "from": vec3_to_json(el.from_),
            "to": vec3_to_json(el.to),
            "size": vec3_to_json(el.size()),
            "origin": vec3_to_json(el.origin),
            "rotation": vec3_to_json(el.rotation),
            "parent": parent.name if parent else None,
            "texture": next((f.texture for f in el.faces.values() if f.texture), None),
        }

    def group_info(self, g: Group) -> dict[str, Any]:
        parent = self.parent_group_of(g.uuid)
        return {
            "uuid": g.uuid,
            "name": g.name,
            "type": "group",
            "origin": vec3_to_json(g.origin),
            "rotation": vec3_to_json(g.rotation),
            "parent": parent.name if parent else None,
            "child_count": len(g.children),
        }

    def outline_tree(self) -> list[dict[str, Any]]:
        def walk(uids: list[str]) -> list[dict[str, Any]]:
            nodes: list[dict[str, Any]] = []
            for uid in uids:
                g = self.find_group_by_uuid(uid)
                if g:
                    node = self.group_info(g)
                    node["children"] = walk(g.children)
                    nodes.append(node)
                    continue
                el = self.find_element_by_uuid(uid)
                if el:
                    nodes.append(self.element_info(el))
            return nodes

        return walk(self.root_children)

    def validate(self) -> list[str]:
        """一致性检查，返回问题列表（空 = 健康）。"""
        issues: list[str] = []
        known = {g.uuid for g in self.groups} | {e.uuid for e in self.elements}

        def check_children(uids: list[str], where: str) -> None:
            for uid in uids:
                if uid not in known:
                    issues.append(f"{where} 引用了不存在的节点 {uid}")
                else:
                    g = self.find_group_by_uuid(uid)
                    if g:
                        check_children(g.children, f"group {g.name}")

        check_children(self.root_children, "root")
        for anim in self.animations:
            for bone_uuid in anim.animators:
                if not self.find_group_by_uuid(bone_uuid):
                    issues.append(f"动画 {anim.name} 引用了不存在的骨骼 {bone_uuid}")
        return issues
