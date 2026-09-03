"""内置建模模板与样例库加载。

两类来源：
1. 代码生成模板（自研，无外部素材，见 TEMPLATES）
2. examples/catalog.json 登记的完整 .bbmodel 文件（见 EXAMPLES_DIR/models/）

文件样例不是硬编码：许可证与来源登记在 catalog.json，运行时按需读取。
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any

from .document import BlockbenchProject
from .codecs import bbmodel as _bbmodel

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def _encode_rgba_png(rows: list[bytearray], width: int, height: int) -> str:
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
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    ).decode("ascii")


_SHADE = {
    "up": 1.18,
    "down": 0.62,
    "north": 0.86,
    "south": 0.95,
    "east": 1.0,
    "west": 0.78,
}


def paint_project_texture(project: BlockbenchProject, palette: dict[str, tuple[int, int, int]]) -> None:
    """按 face.uv 矩形画一张过程化纹理（纯标准库，无外部素材）。"""
    if not project.textures:
        return
    tex = project.textures[0]
    w, h = tex.width, tex.height
    buf: list[bytearray] = [bytearray([0, 0, 0, 0]) * w for _ in range(h)]

    for el in project.elements:
        base = palette.get(el.name)
        if base is None:
            continue
        for key, face in el.faces.items():
            if face.texture is None:
                continue
            x1, y1 = int(round(face.uv[0])), int(round(face.uv[1]))
            x2, y2 = int(round(face.uv[2])), int(round(face.uv[3]))
            x0, x1e = min(x1, x2), max(x1, x2)
            y0, y1e = min(y1, y2), max(y1, y2)
            if x0 >= w or y0 >= h:
                continue
            x1e, y1e = min(x1e, w - 1), min(y1e, h - 1)
            shade = _SHADE[key]
            for py in range(max(0, y0), y1e + 1):
                t = (py - y0) / max(y1e - y0, 1)
                grad = 1.32 - 0.62 * t
                r = int(min(255, base[0] * shade * grad))
                g = int(min(255, base[1] * shade * grad))
                b = int(min(255, base[2] * shade * grad))
                row = buf[py]
                for px in range(max(0, x0), x1e + 1):
                    idx = px * 4
                    row[idx] = r
                    row[idx + 1] = g
                    row[idx + 2] = b
                    row[idx + 3] = 255
    tex.source_data = _encode_rgba_png(buf, w, h)


def build_sword_geckolib(name: str = "sword") -> BlockbenchProject:
    """自研 GeckoLib 长剑模板（无外部素材）。"""
    project = BlockbenchProject(
        name=name,
        format_id="bedrock",  # GeckoLib 的 .geo.json 即 bedrock 几何格式
        texture_width=64,
        texture_height=64,
        box_uv=False,
    )
    project.model_identifier = f"geometry.{name}"
    project.add_texture(f"{name}_tex", 64, 64)

    root = project.add_group("root", origin=[8, 8, 8])
    blade_bone = project.add_group("blade", parent=root.name, origin=[8, 24, 8])
    guard_bone = project.add_group("guard", parent=root.name, origin=[8, 15.25, 8])
    handle_bone = project.add_group("handle", parent=root.name, origin=[8, 11, 8])

    parts: list[tuple[str, list[float], list[float], str, tuple[int, int, int], int]] = [
        ("blade_main", [-1.5, 16, -0.5], [1.5, 30, 0.5], blade_bone.name, (205, 208, 220), 0),
        ("blade_tip", [-0.5, 30, -0.5], [0.5, 33, 0.5], blade_bone.name, (225, 228, 238), 16),
        ("guard", [-3.5, 14.5, -0.75], [3.5, 16, 0.75], guard_bone.name, (214, 168, 62), 22),
        ("grip", [-1.0, 10, -1.0], [1.0, 14.5, 1.0], handle_bone.name, (104, 60, 34), 27),
        ("pommel", [-1.5, 8, -1.5], [1.5, 10, 1.5], handle_bone.name, (214, 168, 62), 34),
        ("gem", [-0.5, 14.6, -0.8], [0.5, 15.4, 0.8], guard_bone.name, (205, 40, 40), 39),
    ]
    palette: dict[str, tuple[int, int, int]] = {}
    for pname, frm, to, bone, color, row in parts:
        project.add_element(
            pname,
            frm,
            to,
            parent=bone,
            texture=f"{name}_tex",
            uv_offset=[0.0, float(row)],
        )
        palette[pname] = color

    anim = project.add_animation("slash", length=0.6, loop="none")
    project.add_keyframe(anim.uuid, blade_bone.name, "rotation", 0.0, [0, 0, 0])
    project.add_keyframe(anim.uuid, blade_bone.name, "rotation", 0.3, [-35, 60, 0])
    project.add_keyframe(anim.uuid, blade_bone.name, "rotation", 0.6, [0, 0, 0])
    paint_project_texture(project, palette)
    return project


TEMPLATES: dict[str, Any] = {
    "sword_geckolib": build_sword_geckolib,
}


def _catalog() -> dict[str, Any]:
    path = EXAMPLES_DIR / "catalog.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"samples": []}


def sample_info(sample_id: str) -> dict[str, Any] | None:
    for entry in _catalog().get("samples", []):
        if entry.get("id") == sample_id:
            return entry
    return None


def sample_ids() -> list[str]:
    ids: list[str] = []
    for entry in _catalog().get("samples", []):
        target = EXAMPLES_DIR / entry["file"]
        if target.is_file():
            ids.append(entry["id"])
    return ids


def available_ids() -> list[str]:
    """模板（代码生成）与文件样例（catalog）的全部可用 id。"""
    return sorted(set(TEMPLATES) | set(sample_ids()))


def load(template: str, name: str | None = None) -> BlockbenchProject:
    """加载内置模板或 catalog 里的完整 .bbmodel 样例。"""
    if template in TEMPLATES:
        builder = TEMPLATES[template]
        default = "sword" if template == "sword_geckolib" else template.replace("_geckolib", "")
        return builder(name=name or default)
    entry = sample_info(template)
    if entry is None:
        raise KeyError(template)
    path = EXAMPLES_DIR / entry["file"]
    if not path.is_file():
        raise FileNotFoundError(f"样例文件缺失: {path}")
    project = _bbmodel.read(str(path))
    if name:
        project.name = name
    return project

