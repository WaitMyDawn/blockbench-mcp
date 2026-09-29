"""纯 Python 软渲染器：把内存模型正交投影成 PNG。

用途：让 Codex/其他 Agent “看见”当前模型，形成 建模→渲染→检查→修改
的反馈闭环。零第三方依赖（PNG 输出与 document/_solid_png 相同思路），
画质目标是“形状验收”，不是最终材质效果。

渲染约定（与 Blockbench/Minecraft 一致的近似）：
* Y 轴向上；骨骼/元素旋转采用 Blockbench 默认 ZYX 欧拉顺序，子变换先于父变换
* 正交相机：yaw 绕 Y、pitch 绕 X；painter 算法按面深度从远到近绘制
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from typing import Any

from .document import BlockbenchProject, Element
from . import images as images_lib
from .geometry import rotate_point, world_cubes

Vec = list[float]


def _pivot_rotate(origin: Vec, rotation: Vec, p: Vec) -> Vec:
    """绕轴心旋转：T(origin) * R * T(-origin)。"""
    return rotate_point(origin, rotation, p)


def _collect_cubes(project: BlockbenchProject) -> list[tuple[Element, list[Vec]]]:
    """沿骨骼树收集立方体及每个角点的世界坐标。"""
    return world_cubes(project)


_FACES: list[tuple[str, tuple[int, int, int, int]]] = [
    ("north", (0, 2, 6, 4)),
    ("south", (1, 3, 7, 5)),
    ("west", (0, 1, 3, 2)),
    ("east", (4, 5, 7, 6)),
    ("up", (2, 3, 7, 6)),
    ("down", (0, 1, 5, 4)),
]

_FACE_SHADE: dict[str, float] = {
    "up": 1.15,
    "down": 0.55,
    "north": 0.85,
    "south": 0.95,
    "east": 1.0,
    "west": 0.75,
}


@dataclass
class _Face3D:
    polygon: list[tuple[float, float, float]]
    depth: float
    color: tuple[int, int, int]


def _hash_color(name: str) -> tuple[int, int, int]:
    """按元素名给稳定配色，按面方向加明暗，便于区分部件。"""
    seed = zlib.crc32(name.encode("utf-8"))
    return (
        80 + (seed % 140),
        80 + ((seed >> 8) % 140),
        90 + ((seed >> 16) % 140),
    )


def _shade(rgb: tuple[int, int, int], face: str) -> tuple[int, int, int]:
    shade = _FACE_SHADE.get(face, 1.0)
    return tuple(max(0, min(255, int(c * shade))) for c in rgb)  # type: ignore[return-value]


def _load_texture_pixels(
    project: BlockbenchProject,
) -> dict[str, tuple[int, int, list[list[tuple[int, int, int, int]]]] | None]:
    """解码项目内所有内嵌纹理；无法解码的记 None。"""
    cache: dict[str, tuple[int, int, list[list[tuple[int, int, int, int]]]] | None] = {}
    for tex in project.textures:
        data = images_lib.data_url_to_bytes(tex.source_data)
        if data is None and tex.source_path:
            try:
                with open(tex.source_path, "rb") as fh:
                    data = fh.read()
            except OSError:
                data = None
        if not data:
            cache[tex.uuid] = None
            continue
        try:
            cache[tex.uuid] = images_lib.decode_png_rgba(data)
        except (ValueError, OSError, zlib.error):
            cache[tex.uuid] = None
    return cache


def _face_color(
    project: BlockbenchProject,
    el: Element,
    face_key: str,
    pixels_cache: dict[str, tuple[int, int, list[list[tuple[int, int, int, int]]]] | None],
) -> tuple[int, int, int]:
    """优先取该面贴图矩形内的平均色，否则退回元素名哈希色。"""
    face = el.faces.get(face_key)
    if face and face.texture:
        entry = pixels_cache.get(face.texture)
        if entry is not None:
            w, h, rows = entry
            try:
                base = images_lib.sample_rect_average(rows, w, h, face.uv)
                return _shade(base, face_key)
            except (ValueError, IndexError):
                pass
    return _shade(_hash_color(el.name), face_key)


def ascii_views(project: BlockbenchProject, cells: int = 36) -> dict[str, list[str]]:
    """生成 前/侧/顶 三个正交 ASCII 视图，供不支持看图的模型使用。

    用“该方向每根骨骼的立方体是否覆盖该格”来画轮廓，
    Y 向上；不同部件用不同字母（首字符），便于读结构。
    """

    items: list[tuple[str, Vec, Vec]] = []
    for el, pts in world_cubes(project):
        lo = [min(p[i] for p in pts) for i in range(3)]
        hi = [max(p[i] for p in pts) for i in range(3)]
        items.append((el.name[:1].upper() or "#", lo, hi))
    if not items:
        return {"front": ["(空模型)"], "side": ["(空模型)"], "top": ["(空模型)"]}

    all_lo = [min(i[1][a] for i in items) for a in range(3)]
    all_hi = [max(i[2][a] for i in items) for a in range(3)]
    span = [max(all_hi[a] - all_lo[a], 1e-6) for a in range(3)]

    def grid(ax1: int, ax2: int, ax3: int) -> list[str]:
        # ax3 是"深度"（忽略）；生成 ax1(横) x ax2(纵, 上为正) 的字符图
        g: list[list[str]] = [[" "] * cells for _ in range(cells)]
        for ch, lo, hi in items:
            def cell(v: float, axis: int) -> int:
                return int((v - all_lo[axis]) / span[axis] * (cells - 1))

            x0, x1 = cell(lo[ax1], ax1), cell(hi[ax1], ax1)
            y0, y1 = cell(lo[ax2], ax2), cell(hi[ax2], ax2)
            for y in range(min(y0, y1), max(y0, y1) + 1):
                for x in range(min(x0, x1), max(x0, x1) + 1):
                    yy = cells - 1 - y  # Y 轴向上
                    if 0 <= yy < cells and 0 <= x < cells:
                        g[yy][x] = ch
        return ["".join(row) for row in g]

    return {
        "front": grid(0, 1, 2),  # 面向 -Z：X 横 Y 纵
        "side": grid(2, 1, 0),  # 面向 +X：Z 横 Y 纵
        "top": grid(0, 2, 1),  # 俯视：X 横 Z 纵（Z 向下）
    }


def render_png(
    project: BlockbenchProject,
    *,
    width: int = 640,
    height: int = 640,
    yaw: float = 45.0,
    pitch: float = 28.0,
    supersample: int = 2,
) -> bytes:
    """渲染模型为 PNG（形状验收用途）。"""
    cubes = _collect_cubes(project)
    if not cubes:
        cubes = []
    pixels_cache = _load_texture_pixels(project)

    # ---- 相机：先 yaw（绕 Y）再 pitch（绕 X），正交投影，深度用于排序
    def view(p: Vec) -> tuple[float, float, float]:
        yaw_r, pitch_r = math.radians(yaw), math.radians(pitch)
        x, y, z = p
        x1 = x * math.cos(yaw_r) + z * math.sin(yaw_r)
        z1 = -x * math.sin(yaw_r) + z * math.cos(yaw_r)
        y2 = y * math.cos(pitch_r) - z1 * math.sin(pitch_r)
        z2 = y * math.sin(pitch_r) + z1 * math.cos(pitch_r)
        return x1, y2, z2

    faces3d: list[_Face3D] = []
    for el, corners in cubes:
        projected = [view(c) for c in corners]
        for face_key, indices in _FACES:
            pts = [projected[i] for i in indices]
            depth = sum(p[2] for p in pts) / 4.0
            faces3d.append(
                _Face3D(
                    polygon=[(p[0], p[1]) for p in pts],
                    depth=depth,
                    color=_face_color(project, el, face_key, pixels_cache),
                )
            )
    faces3d.sort(key=lambda f: f.depth, reverse=True)  # 远 → 近

    # ---- 缩放：根据所有顶点包围盒适配画布
    all_pts = [view(c) for _, corners in cubes for c in corners]
    if all_pts:
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
        scale = (min(width, height) * 0.82) / span
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
    else:
        scale, cx, cy = 1.0, 0.0, 0.0

    # ---- 超采样光栅化：先渲染 s 倍画布，再降采样得到抗锯齿效果
    s = max(1, int(supersample))
    w, h = width * s, height * s

    def to_screen(p: tuple[float, float]) -> tuple[float, float]:
        """视图坐标 → 超采样缓冲区像素坐标。

        两个必须对齐的点：
        * 视图空间 Y 轴向上，而像素行号向下，所以 Y 取负（否则画面上下颠倒）；
        * 投影结果必须落在 s 倍缓冲区上，缩放与画布中心都要乘 s
          （否则模型只画在左上角 1/s 大小，降采样后整体缩小）。
        """
        return ((p[0] - cx) * scale * s + w / 2, -(p[1] - cy) * scale * s + h / 2)

    buf: list[bytearray] = [bytearray([255, 255, 255]) * w for _ in range(h)]

    def fill_polygon(poly_screen: list[tuple[float, float]], color: tuple[int, int, int]) -> None:
        min_x = max(0, int(min(x for x, _ in poly_screen)))
        max_x = min(w - 1, int(max(x for x, _ in poly_screen)))
        min_y = max(0, int(min(y for _, y in poly_screen)))
        max_y = min(h - 1, int(max(y for _, y in poly_screen)))
        if max_x < min_x or max_y < min_y:
            return
        r, g, b = color
        # 归一化绕序：按缓冲区坐标计算有向面积，统一用“逆时针”判定
        area = 0.0
        for i in range(len(poly_screen)):
            x1, y1 = poly_screen[i]
            x2, y2 = poly_screen[(i + 1) % len(poly_screen)]
            area += x1 * y2 - x2 * y1
        reverse = area < 0
        for py in range(min_y, max_y + 1):
            row = buf[py]
            y_center = py + 0.5
            for px in range(min_x, max_x + 1):
                x_center = px + 0.5
                inside = True
                for i in range(len(poly_screen)):
                    x1, y1 = poly_screen[i]
                    x2, y2 = poly_screen[(i + 1) % len(poly_screen)]
                    cross = (x2 - x1) * (y_center - y1) - (y2 - y1) * (x_center - x1)
                    if (cross < 0) != reverse:
                        inside = False
                        break
                if inside:
                    idx = px * 3
                    row[idx] = r
                    row[idx + 1] = g
                    row[idx + 2] = b

    def outline_polygon(poly_screen: list[tuple[float, float]], color: tuple[int, int, int]) -> None:
        r, g, b = color
        for i in range(len(poly_screen)):
            x1, y1 = poly_screen[i]
            x2, y2 = poly_screen[(i + 1) % len(poly_screen)]
            steps = max(1, int(math.hypot(x2 - x1, y2 - y1) * 0.5))
            for t in range(steps + 1):
                px = int(round(x1 + (x2 - x1) * t / steps))
                py = int(round(y1 + (y2 - y1) * t / steps))
                if 0 <= px < w and 0 <= py < h:
                    idx = px * 3
                    buf[py][idx] = r
                    buf[py][idx + 1] = g
                    buf[py][idx + 2] = b

    for f in faces3d:
        screen = [to_screen(p) for p in f.polygon]
        fill_polygon(screen, f.color)
    for f in faces3d:
        screen = [to_screen(p) for p in f.polygon]
        outline_polygon(screen, (30, 30, 30))

    # ---- 降采样 + 编码 PNG
    final: list[bytearray] = [bytearray([255, 255, 255]) * width for _ in range(height)]
    for py in range(height):
        for px in range(width):
            acc = [0, 0, 0]
            for sy in range(s):
                for sx in range(s):
                    idx = (px * s + sx) * 3
                    row = buf[py * s + sy]
                    acc[0] += row[idx]
                    acc[1] += row[idx + 1]
                    acc[2] += row[idx + 2]
            n = s * s
            idx = px * 3
            final[py][idx] = acc[0] // n
            final[py][idx + 1] = acc[1] // n
            final[py][idx + 2] = acc[2] // n
    return _encode_rgb_png(final, width, height)


def render_texture_sheet(
    project: BlockbenchProject,
    texture_name: str | None = None,
    *,
    scale: int = 6,
    grid: bool = True,
) -> tuple[bytes, str | None]:
    """把纹理放大成便于视觉模型检视的画布 PNG。

    scale 表示每个原始像素放大倍数；grid=True 时画出原始像素格线。
    返回 (png_bytes, texture_name)；纹理不存在或无内嵌位图时返回
    带提示的占位图（name=None）。
    """
    tex = project.texture(texture_name) if texture_name else (project.textures[0] if project.textures else None)
    if tex is None:
        return _placeholder_texture_png("NO TEXTURE"), None
    data = images_lib.data_url_to_bytes(tex.source_data)
    if data is None and tex.source_path:
        try:
            with open(tex.source_path, "rb") as fh:
                data = fh.read()
        except OSError:
            data = None
    if not data:
        return _placeholder_texture_png("NO IMAGE"), tex.name
    try:
        w, h, rows = images_lib.decode_png_rgba(data)
    except (ValueError, OSError, zlib.error) as exc:
        return _placeholder_texture_png(f"DECODE FAIL: {exc}"), tex.name
    s = max(1, int(scale))
    out_w, out_h = w * s, h * s
    canvas: list[bytearray] = [bytearray([230, 230, 230]) * out_w for _ in range(out_h)]
    for y in range(h):
        row = rows[y]
        for x in range(w):
            r, g, b, a = row[x]
            # alpha 与浅灰背景混合
            k = a / 255.0
            pr = int(round(230 * (1 - k) + r * k))
            pg = int(round(230 * (1 - k) + g * k))
            pb = int(round(230 * (1 - k) + b * k))
            for dy in range(s):
                yy = y * s + dy
                for dx in range(s):
                    idx = (x * s + dx) * 3
                    row_buf = canvas[yy]
                    row_buf[idx] = pr
                    row_buf[idx + 1] = pg
                    row_buf[idx + 2] = pb
    if grid:
        for yy in range(out_h):
            row_buf = canvas[yy]
            for xx in range(out_w):
                if xx % s == 0 or yy % s == 0:
                    idx = xx * 3
                    row_buf[idx] = 90
                    row_buf[idx + 1] = 90
                    row_buf[idx + 2] = 90
    return _encode_rgb_png(canvas, out_w, out_h), tex.name


def _placeholder_texture_png(label: str) -> bytes:
    """无可用位图时的 256x128 占位画布（白底黑字不可用，纯灰块）。"""
    w, h = 256, 128
    canvas: list[bytearray] = [bytearray([245, 245, 245]) * w for _ in range(h)]
    # 用深灰十字直观提示“占位”，不依赖字体
    for yy in range(h):
        row = canvas[yy]
        for xx in range(w):
            if yy < 12 or yy >= h - 12 or xx < 12 or xx >= w - 12:
                idx = xx * 3
                row[idx] = 190
                row[idx + 1] = 190
                row[idx + 2] = 190
    return _encode_rgb_png(canvas, w, h)


def _encode_rgb_png(rows: list[bytearray], width: int, height: int) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
