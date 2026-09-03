"""MCP 工具注册与调用冒烟。"""

from __future__ import annotations

import asyncio

import pytest

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
