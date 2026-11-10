from copy import deepcopy

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from blockbench_mcp import tools
from blockbench_mcp.codecs import bbmodel
from blockbench_mcp.session import session


@pytest.fixture()
def scope_fixture(tmp_path, monkeypatch):
    tools.project_create("rig")
    tools.texture_create("shared", color="#ffa050")
    bone = tools.bone_create("legs")["data"]["bone"]["uuid"]
    outside = tools.bone_create("torso")["data"]["bone"]["uuid"]
    # A cube name may equal another cube's UUID. Identity matching must prefer the UUID.
    decoy = tools.cube_create("decoy", [0, 6, 0], [2, 8, 2], parent=outside)["data"]["element"]["uuid"]
    calf = tools.cube_create("calf", [0, 0, 0], [2, 4, 2], parent=bone)["data"]["element"]["uuid"]
    heel = tools.cube_create("heel", [0, 0, 2], [2, 1, 3], parent=bone)["data"]["element"]["uuid"]
    tools.cube_update(decoy, name=calf)
    tools.animation_create("walk", length=1.0)
    tools.keyframe_add("walk", "legs", "rotation", 0.0, [0, 0, 0])
    tools.project_save(str(tmp_path / "rig.bbmodel"))
    session.synced_revision = session.revision
    session.scope_bridge = True
    bridge = {"scope": {"id": "selected", "uuids": [calf, heel], "confirmed": True}, "valid": True}

    class Driver:
        def __init__(self, **kwargs):
            pass

        def command(self, method, params):
            if method == "scope_get":
                assert params["expect_project_id"] == session.project_id
                assert params["expect_revision"] == session.revision
                assert params["expect_source_token"] == session.source_token
                if not bridge["valid"]:
                    return {"ok": False, "error": "stale native viewport"}
            if method == "scope_propose":
                bridge["scope"] = {"id": "proposal", "uuids": params["uuids"], "confirmed": False}
            if method == "scope_clear":
                assert params["scope_id"] == bridge["scope"]["id"]
                bridge["scope"] = None
            return {"ok": True, "scope": bridge["scope"]}

    monkeypatch.setattr(tools, "RemoteDriver", Driver)
    return bridge, bone, outside, decoy, calf, heel


def test_scope_preview_commit_and_single_undo(scope_fixture):
    bridge, bone, _, decoy, calf, heel = scope_fixture
    project = session.project
    before = bbmodel.project_to_dict(project)
    rev = session.revision
    steps = tools.undo_status()["data"]["doc_undo"]
    status = tools.scope_status()["data"]
    assert status["bounds"] == {"min": [0, 0, 0], "max": [2, 4, 3]}
    assert status["allowed_parents"] == [{"uuid": bone, "name": "legs"}]

    result = tools.scope_preview([
        {"op": "update", "uuid": calf, "from_": [0, 0, 0], "to": [1, 4, 2]},
        {"op": "delete", "uuid": heel},
        {"op": "add", "name": "boot", "parent": bone, "from_": [1, 0, 0], "to": [2, 1, 3]},
    ])
    draft = result[0]["data"]
    assert result[1].data.startswith(b"\x89PNG")
    assert draft["updated"][0]["after"]["uuid"] == calf
    assert draft["deleted"][0]["uuid"] == heel
    assert draft["added"][0]["name"] == "boot"
    assert bbmodel.project_to_dict(project) == before
    assert session.revision == rev
    committed = tools.scope_commit(draft["draft_id"])["data"]
    assert committed["revision"] == rev + 1
    assert bridge["scope"] is None
    after = bbmodel.project_to_dict(project)
    assert project.find_element_by_uuid(decoy).name == calf
    for key in ("textures", "animations", "groups"):
        assert after[key] == before[key]
    assert not (set(after) - set(before))
    assert tools.undo_status()["data"]["doc_undo"] == min(steps + 1, 8)
    tools.project_undo()
    assert bbmodel.project_to_dict(project) == before
    tools.project_redo()
    assert bbmodel.project_to_dict(project) == after


def test_scope_rejects_unscoped_tools_and_invalid_batches(scope_fixture):
    _, bone, outside, decoy, calf, _ = scope_fixture
    before = bbmodel.project_to_dict(session.project)
    with pytest.raises(ToolError, match="局部编辑模式"):
        tools.cube_delete(decoy)
    for operation, message in (
        ({"op": "delete", "uuid": decoy}, "UUID"),
        ({"op": "update", "uuid": calf, "texture": "shared"}, "允许的字段"),
        ({"op": "add", "name": "other", "parent": outside, "from_": [0, 0, 0], "to": [1, 1, 1]}, "父骨骼"),
        ({"op": "add", "name": "escape", "parent": bone, "from_": [0, 0, 0], "to": [1, 5, 1]}, "空间范围"),
        ({"op": "update", "uuid": calf, "origin": [0, 0, 0], "rotation": [0, 0, 45]}, "空间范围"),
    ):
        with pytest.raises(ToolError, match=message):
            tools.scope_preview([{"op": "update", "uuid": calf, "name": "draft only"}, operation])
    assert session.scope_draft is None
    assert bbmodel.project_to_dict(session.project) == before


def test_scope_requires_confirmation_and_rechecks_before_commit(scope_fixture):
    bridge, _, _, _, calf, heel = scope_fixture
    bridge["scope"] = None
    tools.scope_propose([calf])
    with pytest.raises(ToolError, match="待确认"):
        tools.scope_preview([{"op": "delete", "uuid": calf}])
    bridge["scope"]["confirmed"] = True
    draft = tools.scope_preview([{"op": "delete", "uuid": calf}])[0]["data"]["draft_id"]
    before = deepcopy(session.project.elements)
    bridge["valid"] = False
    with pytest.raises(ToolError, match="stale native viewport"):
        tools.scope_commit(draft)
    bridge["valid"] = True
    bridge["scope"] = {"id": "replacement", "uuids": [heel], "confirmed": True}
    with pytest.raises(ToolError, match="范围已变更"):
        tools.scope_commit(draft)
    assert session.project.elements == before
    tools.scope_cancel()
    tools.cube_delete(calf)


def test_box_scope_checks_bridge_selection_against_document_bounds(scope_fixture):
    bridge, _, _, _, calf, heel = scope_fixture
    bridge["scope"]["bounds"] = {"min": [0, 0, 0], "max": [2, 4, 2]}
    with pytest.raises(ToolError, match="跨出"):
        tools.scope_status()
    bridge["scope"]["uuids"].remove(heel)
    assert tools.scope_status()["data"]["cubes"][0]["uuid"] == calf
