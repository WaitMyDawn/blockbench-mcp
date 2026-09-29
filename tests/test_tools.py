"""MCP 工具注册与调用冒烟。"""

from __future__ import annotations

import asyncio
import io

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from PIL import Image as PILImage

from blockbench_mcp import tools


def test_server_exposes_expected_tools() -> None:
    server = tools.get_server()
    names = [t.name for t in asyncio.run(server.list_tools())]
    required = {
        "project_create",
        "project_open",
        "project_save",
        "project_status",
        "project_outline",
        "cube_create",
        "cubes_create_bulk",
        "cube_update",
        "cube_delete",
        "bone_create",
        "bone_update",
        "bone_delete",
        "texture_create",
        "texture_list",
        "texture_assign",
        "texture_update",
        "texture_delete",
        "texture_export",
        "animation_create",
        "keyframe_add",
        "keyframe_list",
        "keyframe_remove",
        "export_model",
        "render_preview",
        "render_texture",
        "render_ascii",
        "project_load_example",
        "blockbench_health",
        "blockbench_ensure_running",
        "blockbench_screenshot",
        "blockbench_command",
    }
    assert required <= set(names)


def test_requires_project_first() -> None:
    with pytest.raises(Exception):
        tools.project_status()


def test_full_tool_flow() -> None:
    res = tools.project_create("sword", format="bedrock", texture_width=64, texture_height=64)
    assert res["ok"] is True

    tools.texture_create("skin", width=64, height=64, color="#FF8800")
    tools.bone_create("root", origin=[8, 0, 8])
    tools.bone_create("blade", parent="root", origin=[8, 20, 8])
    tools.cube_create("geo", from_=[-1, 20, -1], to=[1, 34, 1], parent="blade", texture="skin")
    tools.texture_assign("geo", "skin", faces="all")
    tools.animation_create("swing", length=1.0, loop="loop")
    tools.keyframe_add("swing", "blade", "rotation", 0.0, [0, 0, 0])
    tools.keyframe_add("swing", "blade", "rotation", 1.0, [0, 90, 0])

    status = tools.project_status()
    assert status["data"]["project"]["counts"]["elements"] == 1
    assert status["data"]["project"]["counts"]["keyframes"] == 2

    frames = tools.keyframe_list("swing")
    assert "blade" in frames["data"]["animations"][0]["bones"]


def test_phase2_texture_roundtrip_and_uv(tmp_path) -> None:
    """B 阶段纹理闭环：纯色创建 → 导出 PNG → 磁盘改色回灌 → 逐面 UV → 预览图。"""
    res = tools.project_create("dog", format="bedrock", texture_width=64, texture_height=64)
    assert res["ok"] is True

    tools.texture_create("dog_skin", width=64, height=64, color="#C89B6C")
    exported = tmp_path / "dog_skin.png"
    out = tools.texture_export("dog_skin", str(exported))
    assert out["data"]["bytes"] > 0
    assert exported.exists() and exported.stat().st_size > 0

    # 换色（生成新纯色 PNG），再从磁盘回灌同一张图
    tools.texture_update("dog_skin", color="#8A5A2B")
    tools.texture_update("dog_skin", source_path=str(exported))

    # 建一个带显式逐面 UV 的立方体并给其余面贴整幅画布
    tools.cube_create(
        "box",
        from_=[-1, 0, -1],
        to=[1, 2, 1],
        texture="dog_skin",
        faces={"north": {"uv": [0, 0, 32, 32]}},
    )
    tools.texture_assign("box", "dog_skin", faces=["east", "south"])

    sheet = tmp_path / "skin_sheet.png"
    rendered = tools.render_texture("dog_skin", path=str(sheet))
    assert isinstance(rendered, list) and len(rendered) == 2  # 文本 JSON + Image 双通道
    assert rendered[0]["data"]["image_path"].endswith("skin_sheet.png")
    assert sheet.exists() and sheet.stat().st_size > 0

    status = tools.project_status()
    assert status["data"]["project"]["counts"]["elements"] == 1


# --------------------------------------------------------------------------- 批量建方块
def test_cubes_create_bulk_creates_many_in_one_call() -> None:
    tools.project_create("bulk", format="bedrock", texture_width=64, texture_height=64)
    tools.bone_create("root", origin=[0, 0, 0])
    specs = [
        {"name": f"c{i}", "from_": [i, 0, 0], "to": [i + 1, 1, 1], "parent": "root"}
        for i in range(25)
    ]
    res = tools.cubes_create_bulk(specs)
    assert res["ok"] is True
    assert res["data"]["created_count"] == 25
    assert res["data"]["failed_count"] == 0
    assert res["data"]["total_elements"] == 25
    assert res["data"]["created"][0]["name"] == "c0"
    assert tools.project_status()["data"]["project"]["counts"]["elements"] == 25


def test_cubes_create_bulk_stops_and_points_at_bad_item() -> None:
    """坏项必须能定位到 index，否则批量接口没法排错。"""
    tools.project_create("bulk2", format="bedrock", texture_width=64, texture_height=64)
    res = tools.cubes_create_bulk(
        [
            {"name": "ok", "from_": [0, 0, 0], "to": [1, 1, 1]},
            {"name": "bad", "from_": [5, 5, 5], "to": [0, 0, 0]},  # from > to
            {"name": "never", "from_": [0, 0, 0], "to": [1, 1, 1]},
        ],
        stop_on_error=True,
    )
    assert res["data"]["created_count"] == 1
    assert res["data"]["failed_count"] == 1
    assert res["data"]["failures"][0]["index"] == 1
    assert res["data"]["failures"][0]["name"] == "bad"
    assert res["data"]["total_elements"] == 1
    assert res["warnings"]


def test_cubes_create_bulk_continue_on_error() -> None:
    tools.project_create("bulk3", format="bedrock", texture_width=64, texture_height=64)
    res = tools.cubes_create_bulk(
        [
            {"name": "a", "from_": [0, 0, 0], "to": [1, 1, 1]},
            {"name": "bad", "from_": [5, 5, 5], "to": [0, 0, 0]},
            {"name": "c", "from_": [2, 0, 0], "to": [3, 1, 1]},
        ],
        stop_on_error=False,
    )
    assert res["data"]["created_count"] == 2
    assert res["data"]["failed_count"] == 1
    assert res["data"]["total_elements"] == 2


def test_cubes_create_bulk_accepts_from_alias_and_info() -> None:
    tools.project_create("bulk4", format="bedrock", texture_width=64, texture_height=64)
    res = tools.cubes_create_bulk(
        [{"name": "alias", "from": [0, 0, 0], "to": [2, 3, 4]}],
        include_info=True,
    )
    element = res["data"]["created"][0]["element"]
    assert element["size"] == [2, 3, 4]


# --------------------------------------------------------------------------- 截图凭证
class _FakeDriver:
    """假插件桥：health 给视口凭证，screenshot 写一张真 PNG。"""

    def __init__(self, health: dict) -> None:
        self._health = health

    def capture(self, out_path: str, params: dict | None = None) -> dict:
        self.screenshot(out_path)
        return self._health.copy()

    def health(self) -> dict:
        return self._health

    def screenshot(self, out_path: str) -> str:
        buf = io.BytesIO()
        PILImage.new("RGBA", (2, 2), (255, 0, 0, 255)).save(buf, format="PNG")
        with open(out_path, "wb") as fh:
            fh.write(buf.getvalue())
        return out_path


def _use_fake_driver(monkeypatch, health: dict) -> None:
    monkeypatch.setattr(tools, "RemoteDriver", lambda *a, **k: _FakeDriver(health))


def test_screenshot_reports_viewport_proof(tmp_path, monkeypatch) -> None:
    """截图必须回报"截的是哪一版"，否则旧模型截图会被当成验收依据。"""
    tools.project_create("gl", format="bedrock", texture_width=64, texture_height=64)
    _use_fake_driver(
        monkeypatch,
        {
            "project": "gl",
            "save_path": r"D:\m\gl.bbmodel",
            "elements": 7,
            "open_projects": 1,
            "blockbench_version": "5.1.6",
        },
    )
    res = tools.blockbench_screenshot(path=str(tmp_path / "gl.png"))
    viewport = res[0]["data"]["viewport"]
    assert viewport["elements"] == 7
    assert viewport["save_path"] == r"D:\m\gl.bbmodel"
    assert viewport["blockbench_version"] == "5.1.6"


def test_screenshot_rejects_stale_viewport(tmp_path, monkeypatch) -> None:
    tools.project_create("gl", format="bedrock", texture_width=64, texture_height=64)
    _use_fake_driver(
        monkeypatch,
        {"project": "gl", "save_path": r"D:\m\gl.bbmodel", "elements": 7},
    )
    with pytest.raises(ToolError) as exc:
        tools.blockbench_screenshot(path=str(tmp_path / "gl.png"), expect_elements=42)
    assert "旧模型" in str(exc.value)

    with pytest.raises(ToolError) as exc2:
        tools.blockbench_screenshot(path=str(tmp_path / "gl.png"), expect_path=r"D:\m\other.bbmodel")
    assert "save_path" in str(exc2.value)


def test_screenshot_accepts_matching_expectations(tmp_path, monkeypatch) -> None:
    tools.project_create("gl", format="bedrock", texture_width=64, texture_height=64)
    _use_fake_driver(
        monkeypatch,
        {"project": "gl", "save_path": r"D:\m\gl.bbmodel", "elements": 7},
    )
    res = tools.blockbench_screenshot(
        path=str(tmp_path / "gl.png"),
        expect_path=r"D:/m/gl.bbmodel",
        expect_elements=7,
    )
    assert res[0]["ok"] is True


def test_blockbench_command_raises_when_plugin_reports_failure(monkeypatch) -> None:
    """插件返回 ok=false（例如 open 报 stale）时不能包装成成功。"""

    class _FailingDriver:
        def command(self, method: str, params: dict) -> dict:
            return {"ok": False, "stale": True, "result": "STALE: 视口里仍是旧项目"}

    monkeypatch.setattr(tools, "RemoteDriver", lambda *a, **k: _FailingDriver())
    with pytest.raises(ToolError) as exc:
        tools.blockbench_command("open", {"path": r"D:\m\gl.bbmodel"})
    assert "STALE" in str(exc.value)
