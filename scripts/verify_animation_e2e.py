"""Real stdio + native animation verification using a COPY of the dragon.

Run with the project's venv, Blockbench and bridge >=0.6.0. No eval needed
for the new tools; optional eval independently compares the native preview
and verifies restoration. Never modifies the original creature assets.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/animation_stage2"


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    originals = list((ROOT / "outputs").glob("lightning_dragon.*"))
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}
    report = {}
    async with stdio_client(StdioServerParameters(command=sys.executable, args=[str(ROOT / "main.py")], cwd=str(ROOT))) as (r,w):
        async with ClientSession(r,w) as client:
            await client.initialize()
            async def call(tool, **params):
                res = await client.call_tool(tool, params)
                if res.isError:
                    raise RuntimeError([c.text for c in res.content if c.type == "text"])
                return json.loads(next(c.text for c in res.content if c.type == "text"))["data"]
            await call("project_open", path=str(ROOT/"outputs/lightning_dragon.bbmodel"))
            await call("project_sync", path=str(OUT/"dragon_copy.bbmodel"), replace_unsaved=True)
            status = await call("project_status")
            probe = await call("blockbench_command", method="probe")
            allow_eval = probe.get("allow_eval", False)
            async def ev(code):
                return (await call("blockbench_command", method="eval", params={"code": code}))["result"]
            fingerprint = "JSON.stringify({time:Timeline.time,selected:Animation.selected&&Animation.selected.uuid,playing:Animation.all.map(a=>a.playing),saved:Blockbench.Project.saved,mode:Modes.selected.id,camera:[Preview.selected.isOrtho,Preview.selected.angle,Preview.selected.camera.position.toArray(),Preview.selected.camera.quaternion.toArray(),Preview.selected.camera.up.toArray(),Preview.selected.camera.zoom,Preview.selected.controls.target.toArray()],bones:Group.all.map(g=>[g.uuid,g.mesh.position.toArray(),g.mesh.quaternion.toArray(),g.mesh.scale.toArray()])})"
            before = json.loads(await ev(fingerprint)) if allow_eval else None
            pairs = [("takeoff","fly"),("fly","landing"),("landing","idle"),("idle","lie_down"),("lie_down","rest"),("rest","wake_up"),("wake_up","idle")]
            name = lambda n: "animation.lightning_dragon."+n
            review = await call("animation_review", sample_rate=8,
                                transitions=[{"from":name(a),"to":name(b)} for a,b in pairs],
                                contact_exclude_prefixes=["fx_beam__"])
            report["review"] = review
            assert all(d["pose_matches"] for d in review["loops"].values()), review["loops"]
            assert all(d["pose_matches"] for d in review["transitions"]), review["transitions"]
            assert review["floor_violation_count"] == 0, review["floor_violations"]
            for camera in ["hero","front","side","rear","top"]:
                result = await call("animation_preview", animation=name("fly"), time=.4,
                                    camera=camera,path=str(OUT/(camera+".png")))
                assert result["viewport"]["pose"]["camera"]["preset"] == camera
                print("captured",camera,flush=True)
            for clip,t in [("lightning_breath",2.0),("rest",3.0)]:
                await call("animation_preview", animation=name(clip),time=t,path=str(OUT/(clip+".png")))
            exported = await call("export_model",target="bedrock",path=str(OUT/"dynamic.geo.json"),
                                  bounds_mode="animated",sample_rate=8)
            description = json.loads((OUT/"dynamic.geo.json").read_text(encoding="utf-8"))["minecraft:geometry"][0]["description"]
            assert all(description[k] == v for k,v in review["render_bounds"].items())
            report["exported"] = exported
            report["flight_gif"] = await call("animation_render", animations=[name(n) for n in ["takeoff","fly","landing"]],
                                               path=str(OUT/"flight.gif"),fps=4,size=512,loop=True)
            report["rest_gif"] = await call("animation_render", animations=[name(n) for n in ["rest","wake_up","idle"]],
                                             path=str(OUT/"rest_cycle.gif"),fps=2,size=512)
            # Compare isolated formal evaluation with full native preview.
            if allow_eval:
                after = json.loads(await ev(fingerprint))
                def close(a,b):
                    if isinstance(a,(int,float)) and isinstance(b,(int,float)): return abs(a-b)<1e-8
                    if isinstance(a,list) and isinstance(b,list): return len(a)==len(b) and all(close(x,y) for x,y in zip(a,b))
                    if isinstance(a,dict) and isinstance(b,dict): return a.keys()==b.keys() and all(close(a[k],b[k]) for k in a)
                    return a==b
                assert close(before,after), (before["camera"],after["camera"])
                report["state_restored"] = True
                # Keep diagnostic mutations restricted to our disposable copy.
                sample = (await call("animation_sample",samples=[{"animation":name("fly"),"time":.4}]))["samples"][0]
                await ev("Modes.options.animate.select();'ready'")
                await ev("Animation.all.forEach(a=>a.playing=false);let a=Animation.all.find(a=>a.name==='"+name("fly")+"');a.select();a.playing=true;Timeline.setTime(.4);Animator.preview();Canvas.scene.updateMatrixWorld(true);'ready'")
                reference = json.loads(await ev("JSON.stringify(Group.all.map(g=>({uuid:g.uuid,position:g.mesh.position.toArray(),quaternion:g.mesh.quaternion.toArray(),scale:g.mesh.scale.toArray()})))"))
                lookup = {g["uuid"]:g for g in sample["bones"]}
                assert all(close(g[ch],lookup[g["uuid"]][ch]) for g in reference for ch in ("position","quaternion","scale"))
                report["matches_native_preview"] = True
            final = await call("project_status")
            assert final["revision"] == status["revision"]
            if "undo" in status: assert final["undo"] == status["undo"]
    assert hashes == {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}
    report["original_assets_unchanged"] = True
    report["passed"] = True
    (OUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("PASS:",review["sample_count"],"poses; original assets unchanged",flush=True)

if __name__ == "__main__":
    asyncio.run(main())
