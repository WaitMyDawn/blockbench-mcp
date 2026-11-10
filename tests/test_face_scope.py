from copy import deepcopy

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from blockbench_mcp import paint, tools
from blockbench_mcp.codecs import bbmodel
from blockbench_mcp.session import session


@pytest.fixture()
def atlas(tmp_path, monkeypatch):
    tools.project_create("atlas", texture_width=16, texture_height=16)
    tools.texture_create("cloth", width=16, height=16, color="#abcdeeff")
    project = session.project
    texture = project.textures[0]
    first = project.add_element("left", [0, 0, 0], [1, 2, 1], faces={
        "north": {"uv": [4, 0, 0, 4]}, "west": {"uv": [0, 4, 4, 8]},
    })
    second = project.add_element("back", [1, 0, 0], [2, 2, 1], faces={
        "south": {"uv": [12, 12, 16, 16]}, "up": {"uv": [4, 4, 8, 8]},
    })
    tools.project_save(str(tmp_path / "atlas.bbmodel"))
    session.synced_revision = session.revision
    state = {
        "id": "face-scope",
        "confirmed": True,
        "faces": [{"uuid": first.uuid, "face": "north"}, {"uuid": second.uuid, "face": "south"}],
        "footprints": [
            {"uuid": cube.uuid, "face": key, "texture": texture.uuid, "uv": face.uv.copy(), "uv_size": [16, 16]}
            for cube in (first, second) for key, face in cube.faces.items()
        ],
    }

    class Driver:
        def command(self, method, params):
            if method == "scope_propose":
                state.update(faces=params["faces"], confirmed=False)
            return {"ok": True, "scope": state if method != "scope_clear" else None}

    monkeypatch.setattr(tools, "RemoteDriver", Driver)
    return project, texture, state, first, second


def test_repaint_disconnected_mirrored_uvs_and_undo(atlas):
    project, texture, _, first, second = atlas
    original = paint.load_canvas(texture)
    geometry = deepcopy(project.elements)
    status = tools.scope_status()["data"]
    assert status["kind"] == "faces"
    assert status["textures"][0]["editable_pixels"] == 32
    out = tools.scope_repaint([
        {"op": "paint", "uuid": first.uuid, "face": "north", "ops": [{"kind": "fill", "color": "#102040"}]},
        {"op": "paint", "uuid": second.uuid, "face": "south", "ops": [{"kind": "fill", "color": "#ef322488", "blend": "set"}]},
    ])
    assert out[1].data.startswith(b"\x89PNG")
    assert paint.load_canvas(texture).tobytes() == original.tobytes()
    assert out[0]["data"]["textures"][0]["changed_pixels"] == 32
    tools.scope_commit(out[0]["data"]["draft_id"])
    after = paint.load_canvas(project.textures[0])
    assert after.getpixel((1, 1)) == (16, 32, 64, 255)
    assert after.getpixel((13, 13)) == (239, 50, 36, 136)
    assert after.getpixel((8, 8)) == original.getpixel((8, 8))
    assert project.elements == geometry
    tools.project_undo()
    assert paint.load_canvas(project.textures[0]).tobytes() == original.tobytes()


def test_shared_pixels_and_outside_alpha_edits_are_rejected(atlas, tmp_path):
    _, texture, state, first, second = atlas
    state["footprints"][1]["uv"] = [2, 2, 5, 5]
    assert tools.scope_status()["data"]["textures"][0]["editable_pixels"] == 28
    with pytest.raises(ToolError, match="共用"):
        tools.scope_repaint([
            {"op": "paint", "uuid": first.uuid, "face": "north", "ops": [{"kind": "fill", "color": "#ff0000"}]},
        ])
    candidate = tmp_path / "candidate.png"
    image = paint.load_canvas(texture)
    red, green, blue, _ = image.getpixel((7, 7))
    image.putpixel((7, 7), (red, green, blue, 10))
    image.save(candidate)
    op = {"op": "replace_png", "texture": texture.uuid, "source_path": str(candidate)}
    with pytest.raises(ToolError, match="之外"):
        tools.scope_repaint([op])
    image = paint.load_canvas(texture)
    image.putpixel((1, 1), (14, 15, 16, 0))
    image.save(candidate)
    draft = tools.scope_repaint([op])
    assert draft[0]["data"]["textures"][0]["changed_pixels"] == 1
    with pytest.raises(ToolError, match="确认范围内"):
        tools.scope_repaint([
            {"op": "paint", "uuid": second.uuid, "face": "up", "ops": [{"kind": "fill", "color": "#ff0000"}]},
        ])


def test_face_proposal_waits_for_confirmation_and_blocks_geometry(atlas):
    _, _, state, first, _ = atlas
    tools.scope_propose_faces([{"uuid": first.uuid, "face": "north"}])
    with pytest.raises(ToolError, match="待确认"):
        tools.scope_repaint([])
    state["confirmed"] = True
    with pytest.raises(ToolError, match="选面重绘"):
        tools.scope_preview([{"op": "delete", "uuid": first.uuid}])
    with pytest.raises(ToolError, match="局部编辑模式"):
        tools.texture_update("cloth", color="#ffffff")


def test_manual_faces_import_without_project_sync(atlas, monkeypatch):
    project, _, state, first, _ = atlas
    snapshot = bbmodel.project_to_dict(project)
    stamp = {"project_id": "native-project", "source_token": "native-source", "revision": 0}
    methods = []

    class Driver:
        def __init__(self, **kwargs):
            pass

        def command(self, method, params):
            methods.append(method)
            if method == "scope_import":
                return {"ok": True, "scope": state, "model": snapshot, "stamp": stamp}
            if method == "scope_get":
                assert params["expect_project_id"] == stamp["project_id"]
                assert params["expect_source_token"] == stamp["source_token"]
                assert "expect_path" not in params
            return {"ok": True, "scope": state}

    monkeypatch.setattr(tools, "RemoteDriver", Driver)
    session.reset()
    result = tools.scope_status()["data"]
    assert methods == ["scope_import"]
    assert result["kind"] == "faces"
    assert session.save_path is None
    draft = tools.scope_repaint([
        {"op": "paint", "uuid": first.uuid, "face": "north", "ops": [{"kind": "fill", "color": "#101010"}]},
    ])[0]["data"]
    tools.scope_commit(draft["draft_id"])
    assert methods == ["scope_import", "scope_get", "scope_get", "scope_clear"]
    with pytest.raises(ToolError, match="保存的路径"):
        tools.project_save()


def test_manual_face_import_preserves_unsaved_mcp_project(atlas):
    project, _, _, _, _ = atlas
    session.edited()
    with pytest.raises(ToolError, match="未保存的修改"):
        tools.scope_status()
    assert session.project is project
