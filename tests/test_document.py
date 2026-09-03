"""文档模型行为测试。"""

from __future__ import annotations

import pytest

from blockbench_mcp.document import BlockbenchProject
from blockbench_mcp.errors import NotFoundError, ValidationError


def test_project_defaults(project: BlockbenchProject) -> None:
    assert project.format_id == "bedrock"
    assert project.summary()["counts"] == {
        "elements": 0,
        "groups": 0,
        "textures": 0,
        "animations": 0,
        "keyframes": 0,
    }


def test_bad_format_rejected() -> None:
    with pytest.raises(ValidationError):
        BlockbenchProject(name="x", format_id="nope")


def test_cube_with_invalid_box_rejected(project: BlockbenchProject) -> None:
    with pytest.raises(ValidationError):
        project.add_element("bad", [0, 0, 0], [0, 1, 1])


def test_hierarchy_and_reparent(project: BlockbenchProject) -> None:
    root = project.add_group("root")
    a = project.add_group("a", parent=root.name)
    b = project.add_group("b", parent=root.name)
    el = project.add_element("box", [0, 0, 0], [1, 1, 1], parent=a.name)

    assert root.children == [a.uuid, b.uuid]
    assert a.children == [el.uuid]
    assert project.parent_group_of(el.uuid) is a

    project.reparent_group(b, a)
    assert root.children == [a.uuid]
    assert a.children == [el.uuid, b.uuid]

    with pytest.raises(ValidationError):
        project.reparent_group(a, b)  # 环


def test_delete_group_cascades(project: BlockbenchProject) -> None:
    root = project.add_group("root")
    a = project.add_group("a", parent=root.name)
    project.add_element("box", [0, 0, 0], [1, 1, 1], parent=a.name)
    project.add_group("leaf", parent=a.name)

    removed = project.remove_group(a.name)
    assert removed == {"removed_groups": 2, "removed_elements": 1}
    assert len(project.elements) == 0
    assert len(project.groups) == 1


def test_keyframe_overwrite_and_length(project: BlockbenchProject) -> None:
    project.add_group("bone")
    anim = project.add_animation("idle", length=0.0)
    project.add_keyframe(anim.name, "bone", "rotation", 0.0, [0, 0, 0])
    project.add_keyframe(anim.name, "bone", "rotation", 0.5, [0, 45, 0])
    project.add_keyframe(anim.name, "bone", "rotation", 0.5, [0, 90, 0])  # 覆盖
    ator = anim.animators[project.groups[0].uuid]
    assert len(ator.keyframes) == 2
    # 关键帧只延长动画长度，不会因为覆盖而缩短
    assert anim.length == pytest.approx(0.5)


def test_unknown_object_raises_with_hint(project: BlockbenchProject) -> None:
    with pytest.raises(NotFoundError) as exc:
        project.element("missing")
    assert "project_status" in str(exc.value)


def test_faces_layout(project: BlockbenchProject) -> None:
    project.add_texture("t", 16, 16)
    el = project.add_element("box", [0, 0, 0], [8, 8, 8], texture="t")
    assert set(el.faces) == {"north", "east", "south", "west", "up", "down"}
    # east 在 0..sx，north 在 sx..2sx，v 从 sz 开始（与 Blockbench 样例一致）
    assert el.faces["east"].uv == [0, 8, 8, 16]
    assert el.faces["north"].uv == [8, 8, 16, 16]
    assert el.faces["up"].uv == [8, 0, 16, 8]
    assert all(face.texture is not None for face in el.faces.values())
