"""Regression coverage for real failures discovered during the dragon workflow."""
from __future__ import annotations

import copy
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from blockbench_mcp import tools, undo
from blockbench_mcp.codecs import bbmodel, bedrock, geckolib
from blockbench_mcp.document import BlockbenchProject
from blockbench_mcp.errors import ExportError, ValidationError
from blockbench_mcp.geometry import world_cubes
from blockbench_mcp.session import session


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["from", "origin", "uv", "values", "time", "length"])
def test_nonfinite_numbers_rejected_without_mutation(value, field):
    p = BlockbenchProject()
    p.add_group("bone")
    p.add_animation("idle")
    before = copy.deepcopy(p.__dict__)
    with pytest.raises(ValidationError):
        if field == "from":
            p.add_element("cube", [value, 0, 0], [1, 1, 1])
        elif field == "origin":
            p.add_group("new", origin=[0, value, 0])
        elif field == "uv":
            p.add_element("cube", [0, 0, 0], [1, 1, 1], faces={"north": {"uv": [0, 0, value, 1]}})
        elif field == "length":
            p.add_animation("bad", length=value)
        else:
            p.add_keyframe("idle", "bone", "rotation", value if field == "time" else 0,
                           [0, value, 0] if field == "values" else [0, 0, 0])
    assert p.__dict__ == before


def test_negative_animation_time_and_length_rejected():
    p = BlockbenchProject()
    p.add_group("bone"); p.add_animation("idle")
    with pytest.raises(ValidationError):
        p.add_animation("bad", length=-1)
    with pytest.raises(ValidationError):
        p.add_keyframe("idle", "bone", "rotation", -0.5, [0, 0, 0])
    assert p.animation("idle").animators == {}


def test_failed_update_rolls_back_all_fields_and_preserves_history():
    tools.project_create("transaction")
    tools.cube_create("cube", [0, 0, 0], [1, 1, 1])
    before = bbmodel.dumps(session.project)
    status = tools.undo_status()
    revision = session.revision
    with pytest.raises(ToolError):
        tools.cube_update("cube", name="changed", rotation=[float("inf"), 0, 0])
    assert bbmodel.dumps(session.project) == before
    assert tools.undo_status() == status
    assert session.revision == revision
    tools.project_undo()
    assert not session.project.elements


def test_delete_subtree_undo_restores_animation_and_redo_isolated():
    tools.project_create("rig")
    tools.bone_create("root")
    tools.bone_create("jaw", parent="root")
    tools.cube_create("tooth", [0, 0, 0], [1, 1, 1], parent="jaw")
    tools.animation_create("bite")
    tools.keyframe_add("bite", "jaw", "rotation", 0.5, [25, 0, 0])
    before = bbmodel.dumps(session.project)
    tools.bone_delete("jaw")
    deleted = bbmodel.dumps(session.project)
    tools.project_undo()
    assert bbmodel.dumps(session.project) == before
    tools.project_redo()
    assert bbmodel.dumps(session.project) == deleted
    tools.project_undo()
    # Editing a restored object must not mutate the historical snapshot.
    tools.cube_update("tooth", name="fang")
    tools.project_undo()
    assert session.project.element("tooth").name == "tooth"
    assert bbmodel.dumps(session.project) == before
    tools.project_redo()
    assert session.project.element("fang").name == "fang"


def test_atomic_cube_batch_rolls_back_and_success_is_one_undo():
    tools.project_create("bulk")
    specs = [{"name": "first", "from": [0, 0, 0], "to": [1, 1, 1]},
             {"name": "bad", "from": [2, 0, 0], "to": [1, 1, 1]}]
    with pytest.raises(ToolError, match="index=1"):
        tools.cubes_create_bulk(specs, atomic=True)
    assert not session.project.elements and session.revision == 0
    assert tools.undo_status()["data"]["doc_undo"] == 0
    tools.cubes_create_bulk([specs[0]] * 3, atomic=True)
    assert tools.undo_status()["data"]["doc_undo"] == 1
    tools.project_undo()
    assert not session.project.elements


def test_keyframe_batch_failure_restores_prior_track_and_length():
    tools.project_create("batch")
    tools.bone_create("bone")
    tools.animation_create("move", length=1)
    tools.keyframe_add("move", "bone", "position", 0, [1, 0, 0])
    before = bbmodel.dumps(session.project)
    items = [{"animation": "move", "bone": "bone", "channel": "position", "time": 0, "values": [8, 0, 0]},
             {"animation": "move", "bone": "missing", "channel": "position", "time": 2, "values": [0, 0, 0]}]
    with pytest.raises(ToolError, match="index=1"):
        tools.keyframes_add_bulk(items)
    assert bbmodel.dumps(session.project) == before
    count = tools.undo_status()["data"]["doc_undo"]
    items[1]["bone"] = "bone"
    tools.keyframes_add_bulk(items)
    assert session.project.animation("move").length == 2
    assert tools.undo_status()["data"]["doc_undo"] == count + 1
    tools.project_undo()
    assert bbmodel.dumps(session.project) == before


def test_noop_update_does_not_advance_revision_or_undo():
    tools.project_create("noop")
    tools.cube_create("cube", [0, 0, 0], [1, 1, 1])
    count = tools.undo_status()
    tools.cube_update("cube", name="cube")
    assert session.revision == 1 and tools.undo_status() == count


def test_static_bounds_contain_rotated_cube_and_nested_bones():
    p = BlockbenchProject()
    p.add_element("long", [-1, 0, -1], [1, 160, 1], origin=[0, 0, 0], rotation=[90, 0, 0])
    root = p.add_group("root", rotation=[0, 0, 90])
    p.add_group("child", parent=root.uuid, origin=[2, 0, 0], rotation=[0, 90, 0])
    p.add_element("small", [3, 0, 0], [4, 1, 1], parent="child", origin=[2, 0, 0])
    cubes = world_cubes(p)
    assert cubes[1][1][0] == pytest.approx([0, 2, -1])
    desc = bedrock.geometry_json(p)["minecraft:geometry"][0]["description"]
    radius = desc["visible_bounds_width"] * 8
    center = desc["visible_bounds_offset"][1] * 16
    half = desc["visible_bounds_height"] * 8
    for _, corners in cubes:
        for x, y, z in corners:
            assert abs(x) <= radius and abs(z) <= radius
            assert center - half <= y <= center + half
    assert desc["visible_bounds_width"] >= 20


def test_unexported_subtree_does_not_expand_bounds():
    p = BlockbenchProject()
    g = p.add_group("hidden"); g.export = False
    p.add_element("huge", [0, 0, 0], [1000, 1000, 1000], parent=g.uuid)
    assert world_cubes(p) == []
    assert bedrock.geometry_json(p)["minecraft:geometry"][0]["description"]["visible_bounds_width"] == 1


def _animation(interpolation):
    p = BlockbenchProject(name="interpolation")
    p.add_group("bone")
    p.add_animation("move", length=2)
    for t, v in [(0, 0), (1, 8), (2, 20)]:
        p.add_keyframe("move", "bone", "position", t, [v, 0, 0], interpolation=interpolation)
    return p


def test_step_export_holds_previous_pose_until_next_key():
    data = bedrock.animation_json(_animation("step"))["animations"]["move"]["bones"]["bone"]["position"]
    assert data["1.0"] == {"pre": [0, 0, 0], "post": [-8, 0, 0]}
    assert data["2.0"] == {"pre": [-8, 0, 0], "post": [-20, 0, 0]}


def test_catmullrom_export_keeps_current_key_value():
    data = bedrock.animation_json(_animation("catmullrom"))["animations"]["move"]["bones"]["bone"]["position"]
    assert data["1.0"] == {"post": [-8, 0, 0], "lerp_mode": "catmullrom"}
    assert data["2.0"]["post"] == [-20, 0, 0]


def test_bezier_export_fails_before_writing_resource_pair(tmp_path):
    p = _animation("bezier")
    geo = tmp_path / "model.geo.json"
    geo.write_text("previous-good-geometry", encoding="utf-8")
    with pytest.raises(ExportError, match="Bezier"):
        bedrock.export_combined(p, str(geo), str(tmp_path / "model.animation.json"))
    assert geo.read_text(encoding="utf-8") == "previous-good-geometry"
    with pytest.raises(ExportError, match="Bezier"):
        geckolib.export_geckolib(p, str(tmp_path / "pack"))
    assert not (tmp_path / "pack").exists()


def test_atomic_save_preserves_old_file_on_replace_failure(tmp_path, monkeypatch):
    p = BlockbenchProject()
    target = tmp_path / "model.bbmodel"
    target.write_text("old-version", encoding="utf-8")
    def fail(*args):
        raise OSError("locked")
    monkeypatch.setattr(bbmodel.os, "replace", fail)
    with pytest.raises(Exception, match="locked"):
        bbmodel.write(p, str(target))
    assert target.read_text(encoding="utf-8") == "old-version"
    assert list(tmp_path.iterdir()) == [target]


def test_project_sync_records_loaded_revision_and_protects_unsaved(tmp_path, monkeypatch):
    target = tmp_path / "model.bbmodel"
    class Bridge:
        unsaved = False
        def __init__(self, **kwargs): pass
        def health(self): return {"save_path": str(target), "saved": not self.unsaved}
        def command(self, method, params):
            model = json.loads(target.read_text(encoding="utf-8"))
            assert model["mcp_sync"]["source_token"] == params["expect_source_token"]
            return {"ok": True, **model["mcp_sync"]}
    monkeypatch.setattr(tools, "RemoteDriver", Bridge)
    tools.project_create("sync")
    tools.cube_create("cube", [0, 0, 0], [1, 1, 1])
    result = tools.project_sync(str(target), expected_revision=1)
    assert result["data"]["revision"] == session.synced_revision == 1
    original = target.read_bytes()
    Bridge.unsaved = True
    tools.cube_update("cube", rotation=[0, 10, 0])
    with pytest.raises(ToolError, match="未保存"):
        tools.project_sync()
    assert session.synced_revision == 1 and target.read_bytes() == original
    tools.project_sync(replace_unsaved=True)
    assert session.synced_revision == 2


def test_open_nonfinite_file_keeps_existing_session(tmp_path):
    tools.project_create("existing")
    p = _animation("linear")
    data = bbmodel.project_to_dict(p)
    data["animations"][0]["animators"][p.groups[0].uuid]["keyframes"][0]["data_points"][0]["x"] = "Infinity"
    path = tmp_path / "bad.bbmodel"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ToolError):
        tools.project_open(str(path))
    assert session.project.name == "existing"


def test_document_undo_and_pixel_history_remain_consistent():
    tools.project_create("paint")
    tools.texture_create("skin", width=16, height=16, color="#123456")
    original = session.project.texture("skin").source_data
    tools.texture_paint([{"kind": "fill", "color": "#FF0000"}])
    red = session.project.texture("skin").source_data
    tools.texture_update("skin", color="#0000FF")
    assert tools.undo_status()["data"]["pixel_undo"] == 0
    tools.project_undo()
    assert session.project.texture("skin").source_data == red
    tools.texture_undo()
    assert session.project.texture("skin").source_data == original
    tools.project_undo()  # reverse the pixel undo as an edit in the document timeline
    assert session.project.texture("skin").source_data == red
    tools.texture_undo()
    tools.texture_redo()
    assert session.project.texture("skin").source_data == red


def test_failed_texture_assignment_does_not_leave_half_applied_faces():
    tools.project_create("faces")
    tools.texture_create("a")
    tools.texture_create("b")
    tools.cube_create("cube", [0, 0, 0], [1, 1, 1])
    before = bbmodel.dumps(session.project)
    with pytest.raises(ToolError):
        tools.texture_assign("cube", "b", faces=["north", "invalid"])
    assert bbmodel.dumps(session.project) == before
