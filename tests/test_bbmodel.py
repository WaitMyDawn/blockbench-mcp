""".bbmodel 5.0 编解码测试。"""

from __future__ import annotations

import json

from blockbench_mcp.codecs import bbmodel
from blockbench_mcp.document import BlockbenchProject


def _make_project() -> BlockbenchProject:
    p = BlockbenchProject(name="sword", format_id="bedrock", texture_width=64, texture_height=64)
    root = p.add_group("root", origin=[8, 0, 8])
    blade = p.add_group("blade", parent=root.name, origin=[8, 20, 8])
    p.add_texture("skin", 64, 64)
    p.add_element("blade_geo", [-1, 20, -1], [1, 34, 1], parent=blade.name, texture="skin")
    anim = p.add_animation("swing", length=1.0, loop="loop")
    p.add_keyframe(anim.uuid, blade.name, "rotation", 0.0, [0, 0, 0])
    p.add_keyframe(anim.uuid, blade.name, "rotation", 1.0, [0, 90, 0])
    return p


def test_roundtrip_preserves_structure() -> None:
    p = _make_project()
    data = bbmodel.project_to_dict(p)
    assert data["meta"] == {"format_version": "5.0", "model_format": "bedrock", "box_uv": False}
    assert data["resolution"] == {"width": 64, "height": 64}

    p2 = bbmodel.project_from_dict(data)
    assert p2.summary()["counts"] == p.summary()["counts"]
    assert [n["name"] for n in p2.outline_tree()] == ["root"]
    blade = p2.group("blade")
    el = p2.elements[0]
    assert p2.parent_group_of(el.uuid) is blade
    # face.texture 已从文件里的下标转回 uuid
    face_texture = next(iter(el.faces.values())).texture
    assert face_texture == p2.textures[0].uuid


def test_write_read_file(tmp_path) -> None:
    p = _make_project()
    path = tmp_path / "demo.bbmodel"
    bbmodel.write(p, str(path))
    p2 = bbmodel.read(str(path))
    assert p2.name == "sword"
    assert len(p2.animations) == 1


def test_preserves_unknown_root_fields() -> None:
    p = _make_project()
    data = bbmodel.project_to_dict(p)
    data["timeline_setups"] = {"x": 1}
    p2 = bbmodel.project_from_dict(data)
    assert p2.extra_root_fields["timeline_setups"] == {"x": 1}
    again = bbmodel.project_to_dict(p2)
    assert again["timeline_setups"] == {"x": 1}


def test_parse_real_style_minimal_bedrock() -> None:
    """模拟真实 5.0 文件的形状（face.texture 为下标、bedrock_binding 等）。"""
    fixture = {
        "meta": {"format_version": "5.0", "model_format": "bedrock", "box_uv": False},
        "name": "bear",
        "model_identifier": "geometry.bear",
        "resolution": {"width": 128, "height": 64},
        "elements": [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "name": "head",
                "box_uv": False,
                "from": [-1, 2, -4],
                "to": [1, 4, -2],
                "origin": [0, 0, 0],
                "faces": {"north": {"uv": [7, 7, 14, 14], "texture": 0}},
                "type": "cube",
            }
        ],
        "groups": [
            {
                "uuid": "22222222-2222-2222-2222-222222222222",
                "name": "head",
                "origin": [0, 3, 0],
                "rotation": [0, 0, 0],
                "bedrock_binding": "",
                "children": [],
                "visibility": True,
                "export": True,
            }
        ],
        "outliner": [{"uuid": "22222222-2222-2222-2222-222222222222", "isOpen": True, "children": ["11111111-1111-1111-1111-111111111111"]}],
        "textures": [
            {"uuid": "33333333-3333-3333-3333-333333333333", "name": "bear", "width": 128, "height": 64}
        ],
        "animations": [],
    }
    p = bbmodel.project_from_dict(fixture)
    assert p.model_identifier == "geometry.bear"
    assert len(p.elements) == 1
    assert p.elements[0].faces["north"].texture == "33333333-3333-3333-3333-333333333333"
    assert p.groups[0].children == ["11111111-1111-1111-1111-111111111111"]


def test_geckolib_model_and_zero_thickness_roundtrip() -> None:
    """Redeemer.bbmodel 同型：geckolib_model 格式 + 零厚度面也能打开。"""
    fixture = {
        "meta": {"format_version": "5.0", "model_format": "geckolib_model", "box_uv": False},
        "name": "redeemer",
        "resolution": {"width": 64, "height": 64},
        "elements": [
            {
                "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "name": "flat_panel",
                "type": "cube",
                "box_uv": False,
                "from": [3, 4, -0.125],
                "to": [3.55, 4, 0.125],  # Y 方向零厚度
                "origin": [0, 0, 0],
                "faces": {"north": {"uv": [0, 0, 2, 0], "texture": 0}},
            }
        ],
        "groups": [],
        "outliner": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
        "textures": [{"uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "name": "t", "width": 16, "height": 16}],
        "animations": [],
    }
    p = bbmodel.project_from_dict(fixture)
    assert p.format_id == "geckolib_model"
    assert len(p.elements) == 1
    assert p.validate() == []
    data = bbmodel.project_to_dict(p)
    assert data["meta"]["model_format"] == "geckolib_model"
    p2 = bbmodel.project_from_dict(data)
    assert p2.elements[0].name == "flat_panel"
