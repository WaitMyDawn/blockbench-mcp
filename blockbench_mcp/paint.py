"""纹理像素绘制核心（Pillow 实现）。

本模块只负责"给定一张 RGBA 画布，如何画"，不持有会话状态：
- load_canvas:      把 Texture 的 source_data / source_path 解码成 PIL RGBA 图像
- encode_texture:   把 PIL 图像编码回 data URL（供写回 Texture.source_data）
- blend_rgba:       set / overlay / multiply / erase 四类混合
- make_gradient:    linear / radial 渐变源图（纯色/双色插值）
- apply_op:         单一绘制 op（fill / linear_gradient / radial_gradient / shadow）
- apply_ops:        一次事务内按顺序执行多个 op（供 MCP 工具组合调用）
- region_pixels:    返回区域中心的代表色（让调用方核对画到了哪）

所有像素坐标遵循 Blockbench 语义：uv/rect 为纹理像素坐标，y 向下。
"""

from __future__ import annotations

import base64
import io
import math
import os
import random
from dataclasses import dataclass
from typing import Any, Callable

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .document import Texture
from .errors import ValidationError


# ---------------------------------------------------------------------------
# 1) 画布载入 / 编码
# ---------------------------------------------------------------------------


def load_canvas(texture: Texture) -> Image.Image:
    """从纹理内嵌位图或磁盘源文件载入 RGBA 画布。

    Pillow 负责解码，因此支持 16-bit / 交错 / 调色板等常见 PNG 变体。
    """
    png: bytes | None = None
    if texture.source_data:
        data = texture.source_data
        if data.startswith("data:"):
            head, _, payload = data.partition(",")
            if "base64" not in head:
                raise ValidationError("纹理位图不是 base64 data URL，无法解析")
            png = base64.b64decode(payload)
        else:
            raise ValidationError("纹理位图格式无法识别（应为 data:image/png;base64,...）")
    elif texture.source_path and os.path.isfile(texture.source_path):
        with open(texture.source_path, "rb") as fh:
            png = fh.read()
    if not png:
        raise ValidationError(
            f"纹理 {texture.name} 没有内嵌位图或可读源文件",
            "先 texture_create(color=...) / texture_create(source_path=...) 生成位图",
        )
    try:
        image = Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001 - Pillow 解码失败统一转业务错误
        raise ValidationError(f"纹理 {texture.name} 的 PNG 无法解码：{exc}") from exc
    if image.size != (texture.width, texture.height):
        # 画布声明尺寸与实际位图不一致：以位图为准，并同步元数据由调用方处理
        pass
    return image


def encode_canvas(image: Image.Image) -> str:
    """把 RGBA 图像编码成内嵌 PNG 的 data URL。"""
    buffer = io.BytesIO()
    image.convert("RGBA").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# 2) 颜色
# ---------------------------------------------------------------------------


def parse_color(color: str | tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """解析 #RGB / #RRGGBB / #RRGGBBAA 或 (r,g,b[,a])，统一为 RGBA。"""
    if isinstance(color, (tuple, list)):
        rgba = tuple(int(c) for c in color)
        if len(rgba) == 3:
            rgba = (*rgba, 255)
        if len(rgba) != 4:
            raise ValidationError(f"颜色元组长度应为 3 或 4：{color!r}")
        if not all(0 <= c <= 255 for c in rgba):
            raise ValidationError(f"颜色通道需在 0..255：{color!r}")
        return rgba  # type: ignore[return-value]
    text = str(color).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) == 6:
        text += "ff"
    if len(text) != 8:
        raise ValidationError("颜色应为 #RGB / #RRGGBB / #RRGGBBAA 或 (r,g,b[,a])")
    try:
        return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4, 6))  # type: ignore[return-value]
    except ValueError as exc:
        raise ValidationError(f"颜色无法解析：{color!r}") from exc


# ---------------------------------------------------------------------------
# 3) 混合
# ---------------------------------------------------------------------------


def _normalize_rgba(rgba: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return (int(rgba[0]), int(rgba[1]), int(rgba[2]), int(rgba[3]))


def _blend_rect(
    base: Image.Image,
    box: tuple[int, int, int, int],
    paint: Image.Image,
    mode: str,
) -> None:
    """把 paint（已与 box 同尺寸）按 mode 混合到 base 的 box 区域。

    set     : 直接用 paint 替换（保留其 alpha）。
    overlay : 标准 alpha 混合：paint 按自身 alpha 覆盖 base。
    multiply: 颜色乘色（paint RGB × base RGB），paint 的 alpha 控制混合强度。
    erase   : 清除 box 内像素的 alpha（保留 RGB 便于重画预览）。

    说明：multiply 需要 RGB 通道精确乘色。Pillow 的 Image.composite 在浮点
    mask=1.0 时有舍入误差（例：255×0 → 254），因此 alpha=255 时直接使用
    ImageChops.multiply；部分透明时按"源色 × 源 alpha"构造 tint 蒙版做混合。
    """
    region = base.crop(box)
    if mode == "set":
        region = paint.convert("RGBA")
    elif mode == "overlay":
        region = Image.alpha_composite(region, paint.convert("RGBA"))
    elif mode == "multiply":
        p = paint.convert("RGBA")
        rgb_source = p.convert("RGB")
        product = ImageChops.multiply(region.convert("RGB"), rgb_source).convert("RGBA")
        alpha_band = p.getchannel("A")
        if alpha_band.getextrema() == (255, 255):
            region = product  # 全不透明：精确乘色
        else:
            # tint = 源色 × (alpha/255)，作为混合蒙版（0 处=原色，255 处=乘积色）
            tint = ImageChops.multiply(rgb_source, alpha_band.convert("RGB")).convert("L")
            region = Image.composite(product, region, tint)
    elif mode == "erase":
        erased = region.getchannel("A").point(lambda _: 0)
        region.putalpha(erased)
    else:
        raise ValidationError(f"未知混合模式 {mode!r}", "可用：set / overlay / multiply / erase")
    base.paste(region, box)


# ---------------------------------------------------------------------------
# 4) 渐变源图
# ---------------------------------------------------------------------------


def _lerp_color(
    c0: tuple[int, int, int, int],
    c1: tuple[int, int, int, int],
    t: float,
) -> tuple[int, int, int, int]:
    """线性插值两个 RGBA 颜色。t 建议先 clamp 到 [0,1]。"""
    t = max(0.0, min(1.0, t))
    return tuple(int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(4))  # type: ignore[return-value]


def _sample_stops(stops: list[tuple[float, tuple[int, int, int, int]]], t: float) -> tuple[int, int, int, int]:
    """按 position 在 stops 中插值取色。stops 需已按 pos 升序。"""
    if not stops:
        return (0, 0, 0, 255)
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if p0 <= t <= p1:
            seg = (t - p0) / (p1 - p0) if p1 > p0 else 0.0
            return _lerp_color(c0, c1, seg)
    return stops[-1][1]


def make_multi_gradient(
    width: int,
    height: int,
    stops: list[tuple[float, tuple[int, int, int, int]]],
    angle: float = 90.0,
) -> Image.Image:
    """多段线性渐变（2..N 个色标），用于圆润体积/材质过渡。"""
    if len(stops) < 2:
        raise ValidationError("multi_gradient 至少需要 2 个色标 stops")
    order = sorted(stops, key=lambda s: s[0])
    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    theta = math.radians(float(angle))
    dx, dy = math.cos(theta), math.sin(theta)
    half_len = abs(width * dx) + abs(height * dy) or 1.0
    for y in range(height):
        for x in range(width):
            t = 0.5 + ((x - width / 2.0) * dx + (y - height / 2.0) * dy) / half_len
            pixels[x, y] = _sample_stops(order, t)
    return image


def make_gradient(
    width: int,
    height: int,
    *,
    kind: str,
    color0: tuple[int, int, int, int],
    color1: tuple[int, int, int, int],
    angle: float | None = None,
    center: tuple[float, float] | None = None,
    radius: float | None = None,
) -> Image.Image:
    """生成一张 width×height 的渐变 RGBA 图（供后续混合使用）。

    kind="linear"：沿 angle 方向（度，0=从左到右，90=从上到下）。
    kind="radial"：从 center 到 radius 距离处线性衰减（超出 radius 用 color1）。
    """
    if kind not in ("linear", "radial"):
        raise ValidationError(f"未知渐变类型 {kind!r}", "可用：linear / radial")
    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    if kind == "linear":
        theta = math.radians(float(angle if angle is not None else 0.0))
        # 轴向量 (dx, dy)；单位轴方向
        dx, dy = math.cos(theta), math.sin(theta)
        # 把中心 (0.5w, 0.5h) 置 t=0.5，保证两端都能到达 0/1
        half_len = abs(width * dx) + abs(height * dy)
        if half_len <= 1e-9:
            half_len = 1.0
        for y in range(height):
            for x in range(width):
                t = 0.5 + ((x - width / 2.0) * dx + (y - height / 2.0) * dy) / half_len
                pixels[x, y] = _lerp_color(color0, color1, t)
        return image
    # radial
    cx, cy = center or (width / 2.0, height / 2.0)
    r = float(radius if radius is not None else max(width, height) * 0.5)
    if r <= 1e-9:
        r = 1.0
    for y in range(height):
        for x in range(width):
            dist = math.hypot(x - cx, y - cy)
            pixels[x, y] = _lerp_color(color0, color1, dist / r)
    return image


# ---------------------------------------------------------------------------
# 5) 区域与 op
# ---------------------------------------------------------------------------


def clamp_box(
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> tuple[int, int, int, int] | None:
    """把半开区间 [x0,x1)×[y0,y1) clamp 到画布；完全在外时返回 None。"""
    x0, x1 = sorted((int(round(x0)), int(round(x1))))
    y0, y1 = sorted((int(round(y0)), int(round(y1))))
    x0 = max(0, min(width, x0))
    x1 = max(0, min(width, x1))
    y0 = max(0, min(height, y0))
    y1 = max(0, min(height, y1))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return (x0, y0, x1, y1)


@dataclass
class Op:
    """一条绘制指令：作用在解析好的 box 上。"""

    kind: str  # fill / linear_gradient / radial_gradient / shadow / multi_gradient / noise / soft_stamp
    color: tuple[int, int, int, int]
    blend: str = "overlay"
    color1: tuple[int, int, int, int] | None = None
    stops: list[tuple[float, tuple[int, int, int, int]]] | None = None
    angle: float | None = None
    center: tuple[float, float] | None = None
    radius: float | None = None
    blur: float = 1.0
    offset: tuple[int, int] = (0, 0)
    strength: float = 1.0
    amount: float = 0.1
    seed: int | None = None
    softness: float = 0.5


def _parse_op(raw: dict[str, Any]) -> Op:
    kind = str(raw.get("kind", "fill"))
    allowed = ("fill", "linear_gradient", "radial_gradient", "shadow",
               "multi_gradient", "noise", "soft_stamp")
    if kind not in allowed:
        raise ValidationError(
            f"未知绘制类型 {kind!r}",
            "可用：fill / linear_gradient / radial_gradient / shadow / multi_gradient / noise / soft_stamp",
        )
    stops = None
    if raw.get("stops") is not None:
        stops = [
            (float(pos), parse_color(color)) for pos, color in raw["stops"]
        ]
    return Op(
        kind=kind,
        color=parse_color(raw.get("color", "#000000")),
        blend=str(raw.get("blend", "overlay")),
        color1=parse_color(raw["color1"]) if raw.get("color1") is not None else None,
        stops=stops,
        angle=float(raw["angle"]) if raw.get("angle") is not None else None,
        center=tuple(float(v) for v in raw["center"]) if raw.get("center") is not None else None,
        radius=float(raw["radius"]) if raw.get("radius") is not None else None,
        blur=float(raw.get("blur", 1.0)),
        offset=tuple(int(v) for v in raw.get("offset", [0, 0])),
        strength=float(raw.get("strength", 1.0)),
        amount=float(raw.get("amount", 0.1)),
        seed=int(raw["seed"]) if raw.get("seed") is not None else None,
        softness=float(raw.get("softness", 0.5)),
    )


def apply_op(image: Image.Image, box: tuple[int, int, int, int], op: Op) -> dict[str, Any]:
    """在 image 的 box 区域执行一条 op，返回统计信息。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if op.kind == "fill":
        paint = Image.new("RGBA", (w, h), op.color)
    elif op.kind == "linear_gradient":
        end = op.color1 or op.color
        paint = make_gradient(w, h, kind="linear", color0=op.color, color1=end, angle=op.angle)
    elif op.kind == "radial_gradient":
        end = op.color1 or op.color
        cx = (op.center[0] - x0) if op.center is not None else w / 2.0
        cy = (op.center[1] - y0) if op.center is not None else h / 2.0
        paint = make_gradient(
            w,
            h,
            kind="radial",
            color0=op.color,
            color1=end,
            center=(cx, cy),
            radius=op.radius,
        )
    elif op.kind == "multi_gradient":
        stops = op.stops or [(0.0, op.color), (1.0, op.color1 or op.color)]
        paint = make_multi_gradient(w, h, stops, angle=op.angle if op.angle is not None else 90.0)
    elif op.kind == "noise":
        _apply_noise(image, box, amount=op.amount, seed=op.seed)
        return {"op": op.kind, "box": [x0, y0, x1, y1], "amount": op.amount}
    elif op.kind == "soft_stamp":
        cx = (op.center[0] - x0) if op.center is not None else w / 2.0
        cy = (op.center[1] - y0) if op.center is not None else h / 2.0
        _apply_soft_stamp(
            image,
            box,
            op.color,
            radius=op.radius if op.radius is not None else max(w, h) * 0.5,
            softness=op.softness,
            center=(cx, cy),
            blend=op.blend,
        )
        return {"op": op.kind, "box": [x0, y0, x1, y1], "radius": op.radius}
    elif op.kind == "shadow":
        _apply_shadow(image, box, op)
        return {"op": op.kind, "box": [x0, y0, x1, y1], "blur": op.blur}
    _blend_rect(image, box, paint, op.blend)
    return {"op": op.kind, "box": [x0, y0, x1, y1], "blend": op.blend}


def _apply_noise(
    image: Image.Image,
    box: tuple[int, int, int, int],
    amount: float = 0.1,
    seed: int | None = None,
) -> None:
    """毛纹噪点：在区域里按随机幅度扰动 RGB 明度，保留 alpha，做出毛发颗粒感。"""
    x0, y0, x1, y1 = box
    region = image.crop(box)
    rng = random.Random(seed)
    px = region.load()
    a = max(0.0, amount)
    for y in range(region.height):
        for x in range(region.width):
            r, g, b, al = px[x, y]
            if al == 0:
                continue
            factor = 1.0 + (rng.random() - 0.5) * a
            px[x, y] = (
                int(max(0, min(255, r * factor))),
                int(max(0, min(255, g * factor))),
                int(max(0, min(255, b * factor))),
                al,
            )
    image.paste(region, box)


def _apply_soft_stamp(
    image: Image.Image,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int, int],
    radius: float,
    softness: float = 0.5,
    center: tuple[float, float] | None = None,
    blend: str = "overlay",
) -> None:
    """软笔刷：以 center 为圆心的径向衰减圆点，做高光/斑块。softness 越大边缘越硬。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx, cy = center or (w / 2.0, h / 2.0)
    r = max(1.0, float(radius))
    stamp = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sp = stamp.load()
    falloff = 1.0 + max(0.0, softness) * 3.0
    for y in range(h):
        for x in range(w):
            d = math.hypot(x - cx, y - cy)
            if d >= r:
                continue
            t = 1.0 - d / r
            alpha = int(round(255 * (t ** falloff)))
            if alpha > 0:
                sp[x, y] = (color[0], color[1], color[2], min(255, alpha))
    _blend_rect(image, box, stamp, blend)


def _apply_shadow(image: Image.Image, box: tuple[int, int, int, int], op: Op) -> None:
    """柔边阴影：区域外扩 alpha 剪影 → 高斯模糊 → 位移 → 按强度合成。

    shadow 需要"区域外"的像素（模糊扩散 + 偏移），因此先扩边再在扩边后的
    局部图层上操作，避免裁剪阴影边缘。
    """
    x0, y0, x1, y1 = box
    pad = max(2, int(math.ceil(op.blur * 2.5)) + abs(op.offset[0]) + abs(op.offset[1]) + 1)
    ext = clamp_box(image.width + pad * 2, image.height + pad * 2, x0, y0, x1, y1)
    ex0, ey0, ex1, ey1 = (
        max(0, x0 - pad),
        max(0, y0 - pad),
        min(image.width, x1 + pad),
        min(image.height, y1 + pad),
    )
    ex0, ey0, ex1, ey1 = clamp_box(image.width, image.height, ex0, ey0, ex1, ey1) or box
    layer = Image.new("RGBA", (ex1 - ex0, ey1 - ey0), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rectangle((x0 - ex0, y0 - ey0, x1 - 1 - ex0, y1 - 1 - ey0), fill=(255, 255, 255, 255))
    if op.blur > 0:
        layer = layer.filter(ImageFilter.GaussianBlur(op.blur))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    strength = max(0.0, min(1.0, op.strength))
    r, g, b, a = op.color
    tint = (r, g, b, int(round(a * strength)))
    shadow.paste(tint, (0, 0), layer)
    ox, oy = op.offset
    # 目标区域（原 box + 扩边 + 偏移）上 alpha 合成，越界安全裁剪
    target_box = clamp_box(
        image.width,
        image.height,
        ex0 + ox,
        ey0 + oy,
        ex0 + ox + (ex1 - ex0),
        ey0 + oy + (ey1 - ey0),
    )
    if target_box is None:
        return
    tx0, ty0, tx1, ty1 = target_box
    sx0, sy0 = tx0 - ex0 - ox, ty0 - ey0 - oy
    sx1, sy1 = sx0 + (tx1 - tx0), sy0 + (ty1 - ty0)
    piece = shadow.crop((sx0, sy0, sx1, sy1))
    base_piece = image.crop(target_box)
    image.paste(Image.alpha_composite(base_piece, piece), target_box)


def _parse_box_spec(
    width: int,
    height: int,
    spec: dict[str, Any] | None,
) -> tuple[int, int, int, int] | None:
    """解析 rect=[x,y,w,h] 或 uv=[x1,y1,x2,y2]；缺省表示整张画布。"""
    if not spec:
        return (0, 0, width, height)
    if "rect" in spec:
        rx, ry, rw, rh = (int(v) for v in spec["rect"])
        return clamp_box(width, height, rx, ry, rx + rw, ry + rh)
    if "uv" in spec:
        u0, v0, u1, v1 = (float(v) for v in spec["uv"])
        return clamp_box(width, height, u0, v0, u1, v1)
    raise ValidationError("region 需要 rect=[x,y,w,h] 或 uv=[x1,y1,x2,y2]")


def apply_ops(
    image: Image.Image,
    ops: list[dict[str, Any]],
    spec: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按顺序执行一组绘制 op（同一区域或各自带 rect/uv）。"""
    width, height = image.size
    results: list[dict[str, Any]] = []
    for i, raw in enumerate(ops):
        region_spec = raw.get("region")
        box = _parse_box_spec(width, height, region_spec)
        if box is None:
            raise ValidationError(f"第 {i + 1} 条 op 的区域为空或完全越界", "检查 rect/uv 是否超出画布")
        results.append(apply_op(image, box, _parse_op(raw)))
    return results


def region_pixels(image: Image.Image, box: tuple[int, int, int, int] | None) -> dict[str, Any]:
    """采样区域代表色：中心像素 + 全区域平均（alpha 加权），供调用方核对。"""
    if box is None:
        box = (0, 0, image.width, image.height)
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    center = image.getpixel((cx, cy))
    small = image.crop(box).resize((1, 1), Image.Resampling.BOX)
    average = small.getpixel((0, 0))
    return {
        "center": [x0 + (x1 - x0) // 2, y0 + (y1 - y0) // 2],
        "center_color": "#%02X%02X%02X" % center[:3],
        "average_color": "#%02X%02X%02X" % average[:3],
        "alpha_center": center[3],
    }


def preview_png(image: Image.Image, max_edge: int = 160) -> bytes:
    """生成缩略图 PNG（供 MCP Image 双通道返回）。"""
    scale = max(1, max(image.size) // max_edge) if max(image.size) > max_edge else 1
    thumb = image.resize((max(1, image.width // scale), max(1, image.height // scale)))
    buffer = io.BytesIO()
    thumb.save(buffer, format="PNG")
    return buffer.getvalue()


# 便捷引用：RGB→十六进制（供 face_map 等输出使用）
def hex_of(color: tuple[int, int, int, int]) -> str:
    return "#%02X%02X%02X" % color[:3]
