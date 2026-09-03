"""Bedrock / Java Block / CEM 导出测试。"""

from __future__ import annotations

import json

from blockbench_mcp.codecs import bedrock, java
from blockbench_mcp.document import BlockbenchProject


def _make_project() -> BlockbenchProject:
    p = BlockbenchProject(name="sword", format_id="bedrock", texture_width=64, texture_height=64)
    root = p.add_group("root", origin=[8, 0, 8])
    blade = p.add_group("blade", parent=root.name, origin=[8, 20, 8], rotation=[0, 15, 0])
    p.add_texture("sword_tex", 64, 64)
    p.add_element("blade_geo", [-1, 20, -1], [1, 34, 1], parent=blade.name, texture="sword_tex")
    anim = p.add_animation("swing", length=1.0, loop="loop")
    p.add_keyframe(anim.uuid, blade.name, "rotation", 0.0, [0, 0, 0])
    p.add_keyframe(anim.uuid, blade.name, "rotation", 1.0, [0, 90, 0])
    return p


def test_bedrock_geometry_x_flip() -> None:
    p = _make_project()
    geo = bedrock.geometry_json(p)
    assert geo["format_version"] == "1.12.0"
    bones = geo["minecraft:geometry"][0]["bones"]
    names = [b["name"] for b in bones]
    assert names == ["root", "blade"]
    blade = bones[1]
    assert blade["pivot"] == [-8, 20, 8]  # x 取反
    assert blade["rotation"] == [0, -15, 0]  # x/y 取反
    cube = blade["cubes"][0]
    assert cube["origin"] == [-1.0, 20, -1]  # from.x=-1,size=2 → -(-1+2)=-1
    assert cube["size"] == [2, 14, 2]


def test_bedrock_geometry_uv_and_bounds() -> None:
    p = _make_project()
    geo = bedrock.geometry_json(p)
    desc = geo["minecraft:geometry"][0]["description"]
    assert desc["identifier"] == "geometry.sword"
    assert desc["visible_bounds_width"] >= 1


def test_bedrock_animation_export() -> None:
    p = _make_project()
    data = bedrock.animation_json(p)
    assert data["format_version"] == "1.8.0"
    swing = data["animations"]["swing"]
    assert swing["loop"] is True
    rot = swing["bones"]["blade"]["rotation"]
    assert rot["0.0"] == [0, 0, 0]
    assert rot["1.0"] == [0, -90, 0]  # y 取反


def test_java_block_export() -> None:
    p = _make_project()
    model, warnings = java.java_block_json(p, resource_root="minecraft:item")
    assert warnings == []
    assert model["texture_size"] == [64, 64]
    assert model["textures"]["layer0"] == "minecraft:item/sword_tex"
    el = model["elements"][0]
    assert el["from"] == [-1, 20, -1]
    assert el["faces"]["north"]["texture"] == "#layer0"


def test_java_block_multiaxis_warns() -> None:
    p = BlockbenchProject(name="x", format_id="java_block", texture_width=16, texture_height=16)
    p.add_texture("t", 16, 16)
    p.add_element("c", [0, 0, 0], [2, 2, 2], rotation=[10, 20, 0], texture="t")
    model, warnings = java.java_block_json(p)
    assert len(warnings) == 1
    assert model["elements"][0]["rotation"]["axis"] == "x"


def test_cem_export() -> None:
    p = _make_project()
    data = java.cem_json(p)
    assert data["textureSize"] == [64, 64]
    assert data["texture"] == "sword_tex"
    model = data["models"][0]
    assert model["part"] == "root"
    assert model["translate"] == [-8, 0, -8]
