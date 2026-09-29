"""渲染预览 / GeckoLib 导出 / 模板 / 插件桥客户端测试。"""

from __future__ import annotations

import json
import os
import socket
import struct
import zlib

import pytest

from blockbench_mcp import examples, images as images_lib, preview, tools
from blockbench_mcp.codecs import geckolib
from blockbench_mcp.document import BlockbenchProject
from blockbench_mcp.drivers.remote import RemoteDriver
from blockbench_mcp.errors import StateError


def _png_colors(data: bytes) -> tuple[int, int, set]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    idat = b""
    w = h = 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + ln]
        if tag == b"IHDR":
            w, h, _, ctype = struct.unpack(">IIBB", chunk[:10])
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + ln
    px = zlib.decompress(idat)
    stride = w * 3 + 1
    colors = {
        (px[y * stride + 1 + x * 3], px[y * stride + 2 + x * 3], px[y * stride + 3 + x * 3])
        for y in range(0, h, 6)
        for x in range(0, w, 6)
    }
    return w, h, colors


def test_sword_template_valid() -> None:
    p = examples.load("sword_geckolib")
    assert p.validate() == []
    assert p.summary()["counts"]["elements"] == 6
    assert p.animations[0].name == "slash"
    assert p.textures[0].source_data.startswith("data:image/png;base64,")


def test_render_png_has_content() -> None:
    p = examples.load("sword_geckolib")
    data = preview.render_png(p, width=256, height=256)
    w, h, colors = _png_colors(data)
    assert (w, h) == (256, 256)
    assert colors != {(255, 255, 255)}
    assert len(colors) > 2


def _model_pixels(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int, int]]]]:
    """解码渲染结果，便于逐像素断言。"""
    return images_lib.decode_png_rgba(data)


def _dominant_model_color(
    rows: list[list[tuple[int, int, int, int]]],
    *,
    y_from: int,
    y_to: int,
) -> tuple[int, int, int] | None:
    """统计某段行内出现最多的“非背景色”（排除白底与黑色描边）。"""
    counts: dict[tuple[int, int, int], int] = {}
    for row in rows[y_from:y_to]:
        for r, g, b, _a in row:
            rgb = (r, g, b)
            if rgb == (255, 255, 255) or max(rgb) < 60:  # 背景 / 描边
                continue
            counts[rgb] = counts.get(rgb, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _vertical_span(rows: list[list[tuple[int, int, int, int]]]) -> tuple[int, int]:
    """非背景像素的最小/最大行号。"""
    hits = [
        y
        for y, row in enumerate(rows)
        for r, g, b, _a in row
        if (r, g, b) != (255, 255, 255)
    ]
    return (min(hits), max(hits)) if hits else (-1, -1)


def test_render_png_y_axis_points_up() -> None:
    """回归：视图 Y 轴向上，高处元素必须落在图片上半部分（to_screen 的 Y 符号）。"""
    p = BlockbenchProject(name="axis", format_id="bedrock", texture_width=16, texture_height=16)
    p.add_element("lower", [-2, 0, -2], [2, 4, 2])
    p.add_element("upper", [-2, 10, -2], [2, 14, 2])

    data = preview.render_png(p, width=160, height=160, yaw=0.0, pitch=0.0, supersample=1)
    w, h, rows = _model_pixels(data)
    assert (w, h) == (160, 160)

    # yaw=pitch=0 时只有 north 面可见，颜色由元素名哈希 + north 明暗决定
    expected_upper = preview._shade(preview._hash_color("upper"), "north")
    expected_lower = preview._shade(preview._hash_color("lower"), "north")

    top = _dominant_model_color(rows, y_from=0, y_to=h // 2)
    bottom = _dominant_model_color(rows, y_from=h // 2, y_to=h)
    assert top == expected_upper, "上半部分应该是 Y 更高的元素（当前渲染上下颠倒）"
    assert bottom == expected_lower, "下半部分应该是 Y 更低的元素"


def test_render_png_uses_full_frame_with_supersampling() -> None:
    """回归：超采样缓冲区必须与输出尺寸对齐（否则模型只占左上角 1/s）。"""
    p = examples.load("sword_geckolib")
    spans = {}
    for supersample in (1, 2):
        data = preview.render_png(
            p, width=256, height=256, yaw=35.0, pitch=22.0, supersample=supersample
        )
        w, h, rows = _model_pixels(data)
        top, bottom = _vertical_span(rows)
        assert top >= 0, "渲染结果不应是空图"
        spans[supersample] = (top, bottom)
        # 自动取景目标：内容高度约占画布的 82%
        assert (bottom - top) / h > 0.7, f"supersample={supersample} 时模型没有铺满取景框"

    assert abs(spans[1][0] - spans[2][0]) <= 2 and abs(spans[1][1] - spans[2][1]) <= 2


def test_ascii_views() -> None:
    p = examples.load("sword_geckolib")
    views = preview.ascii_views(p, cells=24)
    assert set(views) == {"front", "side", "top"}
    joined = "\n".join(views["front"])
    assert "B" in joined  # 刀身字母


def test_geckolib_export_layout(tmp_path) -> None:
    p = examples.load("sword_geckolib")
    out = tmp_path / "pack"
    res = geckolib.export_geckolib(p, str(out), modid="mymod", category="item", name="sword")
    assert res["identifier"] == "geometry.sword"
    assert res["animations"] == ["slash"]
    geo = out / "assets" / "mymod" / "geo" / "item" / "sword.geo.json"
    anim = out / "assets" / "mymod" / "animations" / "item" / "sword.animation.json"
    tex = out / "assets" / "mymod" / "textures" / "item" / "sword_tex.png"
    assert geo.exists() and anim.exists() and tex.exists()
    data = json.loads(geo.read_text(encoding="utf-8"))
    assert data["minecraft:geometry"][0]["description"]["identifier"] == "geometry.sword"
    assert tex.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_preview_tool_dual_channel(tmp_path) -> None:
    tools.project_load_example("sword_geckolib")
    target = str(tmp_path / "preview.png")
    result = tools.render_preview(path=target, width=256, height=256)
    assert isinstance(result, list) and len(result) == 2
    text_block, image_block = result
    assert text_block["ok"] is True
    assert os.path.isfile(target)
    image_bytes = image_block.data if hasattr(image_block, "data") else image_block
    assert image_bytes[:8] == b"\x89PNG\r\n\x1a\n"


def test_remote_driver_offline_hint() -> None:
    # 取一个刚释放的本地端口，确保连接被拒绝
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(StateError) as exc:
        RemoteDriver(port=port, timeout=1.0).health()
    assert "插件桥" in str(exc.value)
