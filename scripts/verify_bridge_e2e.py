"""端到端验收：MCP 文档引擎建模 -> .bbmodel -> Blockbench 桥接打开 -> 视口截图。

验证两件事：
  1) 工具层（内存文档引擎）能否独立完成"骨骼 + 立方体 + 纹理 + 动画 + 导出"全流程；
  2) 二期插件桥能否把 MCP 产出的 .bbmodel 推送到真实 Blockbench 视口并截图回传。

运行：
  D:\\VScode\\python\\blockbench-mcp\\.venv\\Scripts\\python.exe scripts\\verify_bridge_e2e.py

前置：Blockbench 已打开且插件桥已监听 127.0.0.1:18765（脚本会先做健康检查）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from blockbench_mcp import tools  # noqa: E402  导入即注册全部 MCP 工具
from blockbench_mcp.drivers.remote import RemoteDriver  # noqa: E402

OUT_DIR = REPO_ROOT / "outputs" / "bridge_check"
failures: list[str] = []


def step(label: str, fn, *args, **kwargs):
    """执行一步工具调用；失败不中断后续步骤，最终统一汇总。"""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 验收脚本需要暴露任意失败
        failures.append(f"{label}: {type(exc).__name__}: {exc}")
        print(f"[FAIL] {label} -> {type(exc).__name__}: {exc}")
        return None
    text = json.dumps(result, ensure_ascii=False, default=str)
    print(f"[ ok ] {label} -> {text[:200]}")
    return result


def build_model() -> Path:
    """用文档引擎工具搭一个带骨骼/纹理/动画的小模型，返回 .bbmodel 路径。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    step("project_create", tools.project_create,
         name="bridge_check", format="bedrock",
         texture_width=64, texture_height=64)
    step("texture_create", tools.texture_create,
         name="skin", color="#8AB4F8", width=64, height=64)

    step("bone_create(body)", tools.bone_create, name="body", origin=[0, 8, 0])
    step("cube_create(torso)", tools.cube_create, name="torso",
         from_=[-4, 8, -3], to=[4, 20, 3], parent="body")

    step("bone_create(head)", tools.bone_create,
         name="head", origin=[0, 20, 0], parent="body")
    step("cube_create(head_cube)", tools.cube_create, name="head_cube",
         from_=[-3, 20, -3], to=[3, 26, 3], parent="head")

    step("bone_create(arm_l)", tools.bone_create,
         name="arm_l", origin=[5, 19, 0], parent="body")
    step("cube_create(arm_l_cube)", tools.cube_create, name="arm_l_cube",
         from_=[4, 11, -2], to=[6, 19, 2], parent="arm_l")

    step("texture_paint_cube(torso)", tools.texture_paint_cube,
         element="torso", side="#3F6FD8", top="#8AB4F8", bottom="#2B4E9B")
    step("texture_paint_cube(head_cube)", tools.texture_paint_cube,
         element="head_cube", side="#E8C39E", top="#F2D5B8")
    step("texture_paint_cube(arm_l_cube)", tools.texture_paint_cube,
         element="arm_l_cube", side="#8AB4F8")

    step("animation_create(idle)", tools.animation_create,
         name="idle", loop="loop", length=1.0)
    step("keyframe_add(t=0)", tools.keyframe_add, animation="idle", bone="head",
         channel="rotation", time=0.0, values=[0, 0, 0])
    step("keyframe_add(t=1)", tools.keyframe_add, animation="idle", bone="head",
         channel="rotation", time=1.0, values=[12, 0, 0])

    step("validate_quality", tools.validate_quality, max_issues=20)
    step("render_ascii", tools.render_ascii, cells=28)
    step("texture_validate_uv", tools.texture_validate_uv)
    step("project_outline", tools.project_outline)

    preview = OUT_DIR / "bridge_check_preview.png"
    step("render_preview", tools.render_preview, path=str(preview),
         width=480, height=480, yaw=35.0, pitch=22.0)

    bbmodel = OUT_DIR / "bridge_check.bbmodel"
    step("project_save", tools.project_save, path=str(bbmodel))
    step("export_model(bedrock)", tools.export_model, target="bedrock",
         path=str(OUT_DIR / "bedrock"))
    return bbmodel


def check_bridge(bbmodel: Path) -> None:
    """确保就绪 -> API 体检 -> 把 .bbmodel 推送到 Blockbench -> 截图回传。"""
    driver = RemoteDriver()

    # 1) 没开 Blockbench 就自动拉起（已开则等价于一次健康检查）
    try:
        state = driver.ensure_running(wait_seconds=30.0)
        print(f"[ ok ] ensure_running -> action={state['action']} "
              f"waited={state['waited_seconds']}s url={state['url']}")
        health = state["health"]
        print(f"[ ok ] bridge /health -> {json.dumps(health, ensure_ascii=False)}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"bridge ensure_running: {exc}")
        print(f"[FAIL] bridge ensure_running -> {exc}")
        return

    # 2) 只读 API 体检：报告真机上真正可用的打开/截图/撤销链路
    try:
        probe = driver.command("probe")
        file_open = probe.get("file_open", {})
        print(f"[ ok ] bridge probe -> bridge_version={probe.get('bridge_version')} "
              f"read={file_open.get('blockbench_read')} loadModelFile={file_open.get('load_model_file')}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"bridge probe: {exc}")
        print(f"[FAIL] bridge probe -> {exc}")

    # 3) 打开 .bbmodel（官方 Blockbench.read -> loadModelFile 链路）
    try:
        opened = driver.command("open", {"path": str(bbmodel)})
        print(f"[ ok ] bridge open -> {json.dumps(opened, ensure_ascii=False)[:200]}")
        if not opened.get("ok"):
            failures.append(f"bridge open rejected: {opened.get('error')}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"bridge open: {exc}")
        print(f"[FAIL] bridge open -> {exc}")

    # 4) 截图回传
    shot = OUT_DIR / "blockbench_viewport.png"
    try:
        driver.screenshot(str(shot))
        size = shot.stat().st_size
        print(f"[ ok ] bridge screenshot -> {shot} ({size} bytes)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"bridge screenshot: {exc}")
        print(f"[FAIL] bridge screenshot -> {exc}")


def main() -> int:
    bbmodel = build_model()
    check_bridge(bbmodel)
    print("\n==== 验收汇总 ====")
    if failures:
        for item in failures:
            print(f"  - {item}")
        return 1
    print("  全部步骤通过：文档引擎建模 + Blockbench 可视化桥接闭环可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
