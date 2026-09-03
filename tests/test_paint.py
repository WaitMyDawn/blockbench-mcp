"""纹理绘制核心与 undo/redo 测试。"""

from __future__ import annotations

import base64
import io

from PIL import Image

from blockbench_mcp import paint
from blockbench_mcp import tools
from blockbench_mcp import undo
from blockbench_mcp.document import BlockbenchProject, box_uv_faces
from blockbench_mcp.session import session


def _canvas_project(width: int = 16, height: int = 16) -> BlockbenchProject:
    p = BlockbenchProject(name="paint_test", format_id="bedrock", texture_width=width, texture_height=height)
    tex = p.add_texture(
        name="skin",
        width=width,
        height=height,
        source_data=paint.encode_canvas(Image.new("RGBA", (width, height), (200, 150, 100, 255))),
    )
    el = p.add_element("box", [0, 0, 0], [8, 8, 8], texture="skin")
    el.faces = box_uv_faces(el.size(), uv_offset=[0, 0], texture_uuid=tex.uuid)
    return p


def _install(project: BlockbenchProject) -> None:
    session.project = project
    session.save_path = None
    session.dirty = False
    undo.manager.clear_all()
    undo.manager.reset_if_project_changed(project)


# ---------- paint 原语 ----------


def test_parse_color_variants() -> None:
    assert paint.parse_color("#fff") == (255, 255, 255, 255)
    assert paint.parse_color("#FF2200") == (255, 34, 0, 255)
    assert paint.parse_color("#FF220080") == (255, 34, 0, 128)
    assert paint.parse_color((1, 2, 3)) == (1, 2, 3, 255)
    assert paint.parse_color((1, 2, 3, 99)) == (1, 2, 3, 99)


def test_gradient_endpoints() -> None:
    linear = paint.make_gradient(8, 1, kind="linear", color0=(0, 0, 0, 255), color1=(255, 255, 255, 255), angle=0.0)
    assert linear.getpixel((0, 0))[0] < 40  # 起点接近 color0
    assert linear.getpixel((7, 0))[0] > 215  # 终点接近 color1
    radial = paint.make_gradient(8, 8, kind="radial", color0=(0, 0, 0, 255), color1=(255, 255, 255, 255), center=(4, 4), radius=4.0)
    assert radial.getpixel((4, 4))[0] < 20  # 中心 = color0
    assert radial.getpixel((0, 0))[0] > 200  # 边缘 = color1


def test_blend_modes_change_pixels() -> None:
    image = Image.new("RGBA", (8, 8), (200, 150, 100, 255))
    paint.apply_op(image, (0, 0, 4, 4), paint.Op(kind="fill", color=(255, 0, 0, 255), blend="set"))
    assert image.getpixel((1, 1))[:3] == (255, 0, 0)
    paint.apply_op(image, (0, 0, 4, 4), paint.Op(kind="fill", color=(255, 0, 0, 0), blend="overlay"))
    assert image.getpixel((1, 1))[:3] == (255, 0, 0)  # 透明不改变
    paint.apply_op(image, (0, 0, 4, 4), paint.Op(kind="fill", color=(0, 0, 0, 255), blend="multiply"))
    assert image.getpixel((1, 1))[:3] == (0, 0, 0)  # 乘黑得黑
    paint.apply_op(image, (0, 0, 8, 8), paint.Op(kind="fill", color=(0, 0, 0, 255), blend="erase"))
    assert image.getpixel((5, 5))[3] == 0  # erase 清空 alpha


def test_clamp_box_out_of_range_returns_none() -> None:
    assert paint.clamp_box(8, 8, 9, 9, 12, 12) is None
    assert paint.clamp_box(8, 8, -4, 0, 2, 2) == (0, 0, 2, 2)


# ---------- undo/redo（像素级为主，文档级为辅） ----------


def test_paint_undo_redo_roundtrip() -> None:
    project = _canvas_project()
    _install(project)
    tex = project.textures[0]
    result = tools.texture_paint_face(
        element="box",
        face="north",
        ops=[{"kind": "fill", "color": "#FF0000", "blend": "set"}],
    )
    assert isinstance(result, list) and result[0]["ok"] is True
    uv = result[0]["data"]["uv"]
    assert uv == [8, 8, 16, 16]  # 16×16 画布 box 的 north 面
    assert tex.source_data != ""
    assert undo.manager.status(project)["pixel_undo"] == 1

    before_png = tex.source_data
    tools.texture_paint_face(
        element="box",
        face="north",
        ops=[{"kind": "fill", "color": "#0000FF", "blend": "set"}],
    )
    blue_png = tex.source_data
    assert blue_png != before_png

    tools.texture_undo()
    assert undo.manager.status(project)["pixel_redo"] == 1
    tools.texture_redo()
    assert undo.manager.status(project)["pixel_undo"] == 2


def test_project_undo_restores_pixels() -> None:
    project = _canvas_project()
    _install(project)
    tools.texture_paint_face(
        element="box",
        face="up",
        ops=[{"kind": "fill", "color": "#112233", "blend": "set"}],
    )
    after = project.texture("skin").source_data
    tools.project_undo()  # 回到本次绘制之前
    assert project.texture("skin").source_data != after
    tools.project_redo()  # 回到绘制之后
    assert project.texture("skin").source_data == after


def test_face_paint_actually_changes_pixels() -> None:
    project = _canvas_project()
    _install(project)
    tex = project.textures[0]
    tools.texture_paint_face(
        element="box",
        face="north",
        ops=[{"kind": "fill", "color": "#12AB34", "blend": "set"}],
    )
    png = tex.source_data.split(",", 1)[1]
    image = Image.open(io.BytesIO(base64.b64decode(png))).convert("RGBA")
    assert image.getpixel((10, 10))[:3] == (0x12, 0xAB, 0x34)
    assert image.getpixel((2, 2))[:3] == (200, 150, 100)  # 区域外不受影响


def test_paint_all_textures_in_one_transaction() -> None:
    p = _canvas_project()
    extra = p.add_texture(
        name="second",
        width=16,
        height=16,
        source_data=paint.encode_canvas(Image.new("RGBA", (16, 16), (10, 20, 30, 255))),
    )
    _install(p)
    result = tools.texture_paint(
        ops=[{"kind": "fill", "color": "#FF2200", "blend": "set", "region": {"rect": [0, 0, 4, 4]}}]
    )
    assert set(result[0]["data"]["changed"]) == {"skin", "second"}
    assert extra.source_data.startswith("data:image/png;base64,")
    assert undo.manager.status(p)["doc_undo"] == 1


def test_paint_cube_side_top_bottom() -> None:
    p = BlockbenchProject(name="pc", format_id="bedrock", texture_width=32, texture_height=32)
    tex = p.add_texture(
        name="skin",
        width=32,
        height=32,
        source_data=paint.encode_canvas(Image.new("RGBA", (32, 32), (200, 150, 100, 255))),
    )
    el = p.add_element("box", [0, 0, 0], [8, 8, 8], texture="skin")
    el.faces = box_uv_faces(el.size(), uv_offset=[0, 0], texture_uuid=tex.uuid)
    _install(p)
    result = tools.texture_paint_cube(
        element="box",
        side="#FF0000",
        top="#00FF00",
        bottom="#0000FF",
        blend="set",
    )
    assert result[0]["ok"] is True
    assert "box" in result[0]["data"]["element"]
    # side 面(北)应为红，顶面应为绿，底面应为蓝
    png = base64.b64decode(tex.source_data.split(",", 1)[1])
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # box_uv_faces 布局：side 北在 (sx,4sx) y=4..12；top 在 y=0..4
    assert img.getpixel((12, 8))[:3] == (255, 0, 0)   # 侧面通红
    assert img.getpixel((8, 2))[:3] == (0, 255, 0)    # 顶面绿
    assert img.getpixel((20, 2))[:3] == (0, 0, 255)   # 底面蓝（down 在 x16..24）


def test_resize_texture_syncs_project_resolution(tmp_path) -> None:
    """改用 texture_update 放大画布后，项目级 resolution 应同步，避免手动改尺寸。"""
    res = tools.project_create("sized", format="bedrock", texture_width=64, texture_height=64)
    assert res["ok"] is True
    tools.texture_create("skin", width=64, height=64, color="#C89B6C")
    tools.texture_update("skin", width=256, height=256)
    status = tools.project_status()
    assert status["data"]["project"]["texture_size"] == [256, 256]


def test_multi_gradient_midpoint() -> None:
    img = paint.Image.new("RGBA", (8, 1), (0, 0, 0, 255))
    paint.apply_op(
        img,
        (0, 0, 8, 1),
        paint.Op(kind="multi_gradient", color=(0, 0, 0, 255),
                 stops=[(0.0, (0, 0, 0, 255)), (0.5, (128, 128, 128, 255)), (1.0, (255, 255, 255, 255))],
                 angle=0.0, blend="set"),
    )
    assert img.getpixel((0, 0))[0] < 20       # 起点近黑
    assert img.getpixel((4, 0))[0] > 110      # 中点近灰
    assert img.getpixel((7, 0))[0] > 200      # 终点更亮（对称映射端点 t≈0.875）
    assert img.getpixel((0, 0))[0] < img.getpixel((4, 0))[0] < img.getpixel((7, 0))[0]  # 单调递增


def test_noise_perturbs_pixels() -> None:
    img = paint.Image.new("RGBA", (8, 8), (128, 128, 128, 255))
    paint.apply_op(img, (0, 0, 8, 8), paint.Op(kind="noise", color=(0, 0, 0, 255), amount=0.6, seed=7))
    pix = [img.getpixel((x, y))[0] for y in range(8) for x in range(8)]
    assert any(v != 128 for v in pix)  # 有随机扰动
    assert all(0 <= v <= 255 for v in pix)
    assert all(p[3] == 255 for p in [img.getpixel((x, y)) for y in range(8) for x in range(8)])  # alpha 保留


def test_soft_stamp_radial() -> None:
    img = paint.Image.new("RGBA", (16, 16), (200, 150, 100, 255))
    paint.apply_op(
        img,
        (0, 0, 16, 16),
        paint.Op(kind="soft_stamp", color=(255, 255, 255, 255), center=(8, 8),
                 radius=6.0, softness=0.0, blend="overlay"),
    )
    center = img.getpixel((8, 8))
    corner = img.getpixel((0, 0))
    assert center[0] > 200 and center[1] > 150  # 中心亮
    assert corner == (200, 150, 100, 255)       # 角落不受影响
