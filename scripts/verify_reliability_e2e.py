"""Real stdio MCP -> native bridge checks, confined to a dedicated test project.

Requires a running bridge >=0.5.0. Eval is optional; if enabled it also checks
native geometry and protects a deliberately dirtied test tab.
"""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/reliability_check"


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {}
    async with stdio_client(StdioServerParameters(command=sys.executable, args=[str(ROOT / "main.py")], cwd=str(ROOT))) as (r, w):
        async with ClientSession(r, w) as client:
            await client.initialize()

            async def call(tool_name, **args):
                result = await client.call_tool(tool_name, args)
                if result.isError:
                    raise RuntimeError([c.text for c in result.content if c.type == "text"])
                return json.loads(next(c.text for c in result.content if c.type == "text"))

            report["health"] = (await call("blockbench_health"))["data"]
            probe = (await call("blockbench_command", method="probe"))["data"]
            await call("project_create", name="mcp_reliability_check", format="bedrock")
            await call("texture_create", name="check", width=64, height=64, color="#795FC6")
            await call("bone_create", name="root", origin=[0, 0, 0], rotation=[0, 0, 25])
            await call("bone_create", name="head", parent="root", origin=[0, 16, 0], rotation=[0, 30, 0])
            await call("cubes_create_bulk", atomic=True, cubes=[
                {"name": "body", "from": [-4, 0, -4], "to": [4, 16, 4], "parent": "root"},
                {"name": "head_cube", "from": [-4, 16, -4], "to": [4, 24, 4], "parent": "head"},
            ])
            await call("animation_create", name="idle", length=1, loop="loop")
            await call("keyframes_add_bulk", keyframes=[
                {"animation": "idle", "bone": "head", "channel": "rotation", "time": t, "values": [0, v, 0]}
                for t, v in [(0, 0), (0.5, 12), (1, 0)]])
            for name, interpolation in [("step_check", "step"), ("spline_check", "catmullrom")]:
                await call("animation_create", name=name, length=2)
                await call("keyframes_add_bulk", keyframes=[
                    {"animation": name, "bone": "head", "channel": "position", "time": t,
                     "values": [v, 0, 0], "interpolation": interpolation}
                    for t, v in [(0, 0), (1, 4), (2, 12)]])
            status = (await call("project_status"))["data"]
            revision = status["revision"]
            target = str(OUT / "check.bbmodel")
            a = (await call("project_sync", path=target, expected_revision=revision))["data"]["viewport"]
            first = await call("blockbench_screenshot", path=str(OUT / "first.png"), expect_path=target,
                               expect_elements=2, expect_revision=revision)
            assert first["data"]["viewport"]["source_token"] == a["source_token"]
            await call("cube_update", cube="head_cube", from_=[-5, 16, -5], to=[5, 28, 5])
            revision += 1
            b = (await call("project_sync", expected_revision=revision))["data"]["viewport"]
            assert a["uuid"] != b["uuid"] and a["source_token"] != b["source_token"]
            assert a["elements"] == b["elements"] == 2
            await call("blockbench_screenshot", path=str(OUT / "updated.png"), expect_path=target,
                       expect_elements=2, expect_revision=revision)
            report["same_path_same_count_reload"] = {"first": a, "second": b}
            if probe.get("allow_eval"):
                native = (await call("blockbench_command", method="eval", params={"code":
                    "JSON.stringify({max:Cube.all.find(c=>c.name==='head_cube').to,order:Group.all[0].mesh.rotation.order})"}))["data"]["result"]
                native = json.loads(native)
                assert native["max"] == [5, 28, 5] and native["order"] == "ZYX", native
                report["native_geometry"] = native
                await call("blockbench_command", method="eval", params={"code": "Project.saved=false;'test tab marked dirty'"})
                unchanged = Path(target).read_bytes()
                rejected = await client.call_tool("project_sync", {})
                assert rejected.isError and Path(target).read_bytes() == unchanged
                report["unsaved_protection"] = "passed"
                await call("project_sync", replace_unsaved=True)
            else:
                report["unsaved_protection"] = "covered by JavaScript tests; native eval disabled"
            await call("project_undo")
            revision += 1
            await call("project_sync", expected_revision=revision)
            await call("blockbench_screenshot", path=str(OUT / "restored.png"), expect_path=target,
                       expect_elements=2, expect_revision=revision)
            report["undo_sync"] = "passed"
            exported = (await call("export_model", target="bedrock", path=str(OUT / "check.geo.json")))["data"]
            if probe.get("allow_eval"):
                compiled = json.loads((await call("blockbench_command", method="eval", params={"code":
                    "JSON.stringify(Object.fromEntries(['step_check','spline_check'].map(n=>[n,Animation.all.find(a=>a.name===n).compileBedrockAnimation()])))"
                }))["data"]["result"])
                exported_animations = json.loads(Path(exported["animation"]["path"]).read_text(encoding="utf-8"))["animations"]
                for name in compiled:
                    native_track = compiled[name]["bones"]["head"]["position"]
                    exported_track = exported_animations[name]["bones"]["head"]["position"]
                    normalize = lambda track: {float(t): value for t, value in track.items()}
                    assert normalize(native_track) == normalize(exported_track), (name, native_track, exported_track)
                report["native_interpolation_export_match"] = True
                bounds = json.loads((await call("blockbench_command", method="eval", params={"code":
                    "(function(){Canvas.scene.updateMatrixWorld(true);let b=new THREE.Box3();Cube.all.forEach(c=>b.union(new THREE.Box3().setFromObject(c.mesh)));return JSON.stringify({min:b.min.toArray(),max:b.max.toArray()})})()"}))["data"]["result"])
                geometry = json.loads(Path(exported["geometry"]["path"]).read_text(encoding="utf-8"))
                desc = geometry["minecraft:geometry"][0]["description"]
                half_width = desc["visible_bounds_width"] * 8
                half_height = desc["visible_bounds_height"] * 8
                center = desc["visible_bounds_offset"][1] * 16
                assert all(abs(bounds[key][axis]) <= half_width + 1e-5 for key in ("min", "max") for axis in (0, 2))
                assert center - half_height <= bounds["min"][1] and center + half_height >= bounds["max"][1]
                report["native_bounds_contained"] = bounds
            report["passed"] = True
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
