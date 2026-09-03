"""渲染预览 / GeckoLib 导出 / 模板 / 插件桥客户端测试。"""

from __future__ import annotations

import json
import os
import socket
import struct
import zlib

import pytest

from blockbench_mcp import examples, preview, tools
from blockbench_mcp.codecs import geckolib
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

