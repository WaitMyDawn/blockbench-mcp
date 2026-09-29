"""Read-only native skeletal sampling and bounded animation review reports."""
from __future__ import annotations

import math
from typing import Any

from .document import BlockbenchProject, finite_number
from .drivers.remote import RemoteDriver
from .errors import StateError, ValidationError
from .session import session


def native_proof() -> dict[str, Any]:
    if session.synced_revision != session.revision or not session.source_token or session.dirty:
        raise StateError("动画验收需要同步当前工程", "先调用 project_sync；采样不会自动覆盖原生未保存修改")
    return {"expect_path": session.save_path, "expect_elements": len(session.require_project().elements),
            "expect_project_id": session.project_id, "expect_revision": session.revision,
            "expect_source_token": session.source_token}


def sample_native(samples: list[dict], *, include_bones: bool = True, include_elements: bool = False,
                  floor: float = 0, floor_tolerance: float = .05,
                  contact_exclude_prefixes: list[str] | None = None) -> dict:
    params = native_proof()
    params.update(samples=samples, include_bones=include_bones, include_elements=include_elements,
                  floor=finite_number(floor, "floor"), floor_tolerance=finite_number(floor_tolerance, "floor_tolerance", minimum=0),
                  contact_exclude_prefixes=contact_exclude_prefixes or [])
    result = RemoteDriver(timeout=30).command("animation_sample", params)
    if not result.get("ok"):
        raise StateError(result.get("error", "原生动画采样失败"), "需要桥接插件 0.6.0；确认时间线已暂停")
    if len(result.get("samples", [])) != len(samples):
        raise StateError("原生采样数量与请求不一致")
    return result


def visible_bounds(bounds: dict, margin: float = 16) -> dict:
    """Conservative square X/Z render volume; input in model units, output blocks."""
    margin = finite_number(margin, "bounds_margin", minimum=0)
    lo, hi = bounds["min"], bounds["max"]
    if len(lo) != 3 or len(hi) != 3:
        raise ValidationError("bounds min/max 必须是三维向量")
    lo = [finite_number(v, "bounds.min") for v in lo]
    hi = [finite_number(v, "bounds.max") for v in hi]
    if any(a > b for a, b in zip(lo, hi)):
        raise ValidationError("bounds min 不能大于 max")
    radius = max(abs(lo[0]), abs(lo[2]), abs(hi[0]), abs(hi[2])) + margin
    return {"visible_bounds_width": max(1, math.ceil(radius / 8)),
            "visible_bounds_height": max(1, math.ceil((hi[1] - lo[1] + margin * 2) / 16)),
            "visible_bounds_offset": [0, (hi[1] + lo[1]) / 32, 0]}


def pose_delta(a: dict, b: dict) -> dict:
    aa, bb = {v["uuid"]: v for v in a["bones"]}, {v["uuid"]: v for v in b["bones"]}
    if aa.keys() != bb.keys():
        raise ValidationError("衔接姿态的骨骼集合不同")
    result = {"position": 0., "rotation_degrees": 0., "scale": 0., "worst_bones": {}}
    for uid, av in aa.items():
        bv = bb[uid]
        # Quaternions handle 360-degree equivalence and Euler wrapping.
        dot = abs(sum(x * y for x, y in zip(av["quaternion"], bv["quaternion"])))
        metrics = {"position": max(abs(x-y) for x, y in zip(av["position"], bv["position"])),
                   "rotation_degrees": math.degrees(2 * math.acos(min(1., dot))),
                   "scale": max(abs(x-y) for x, y in zip(av["scale"], bv["scale"]))}
        for channel, value in metrics.items():
            if value > result[channel]:
                result[channel] = value
                result["worst_bones"][channel] = av["name"]
    result["pose_matches"] = result["position"] <= .01 and result["rotation_degrees"] <= .01 and result["scale"] <= .0001
    return result


def velocity_delta(a0: dict, a1: dict, b0: dict, b1: dict) -> dict:
    """One-sided finite difference, a diagnostic rather than a smoothness proof."""
    dt_a, dt_b = a1["time"] - a0["time"], b1["time"] - b0["time"]
    if dt_a <= 0 or dt_b <= 0:
        return {"available": False}
    maps = [{v["uuid"]: v for v in s["bones"]} for s in (a0, a1, b0, b1)]
    result = {"available": True, "position_per_second": 0., "rotation_degrees_per_second": 0., "scale_per_second": 0.}
    for uid in maps[0]:
        for ch, output in (("position", "position_per_second"), ("rotation", "rotation_degrees_per_second"), ("scale", "scale_per_second")):
            for axis in range(3):
                da = maps[1][uid][ch][axis] - maps[0][uid][ch][axis]
                db = maps[3][uid][ch][axis] - maps[2][uid][ch][axis]
                if ch == "rotation":
                    da, db = (da+180) % 360-180, (db+180) % 360-180
                result[output] = max(result[output], abs(da / dt_a - db / dt_b))
    return result


def review_native(project: BlockbenchProject, animations: list[str] | None = None,
                  transitions: list[dict[str, str]] | None = None, sample_rate: float = 12,
                  margin: float = 16, floor: float = 0, floor_tolerance: float = .05,
                  contact_exclude_prefixes: list[str] | None = None) -> dict:
    rate = finite_number(sample_rate, "sample_rate", minimum=.1)
    if rate > 60:
        raise ValidationError("sample_rate 最大为 60")
    finite_number(margin, "bounds_margin", minimum=0)
    names = animations if animations is not None else [a.name for a in project.animations]
    if not names or len(set(names)) != len(names):
        raise ValidationError("animations 不能为空或包含重复名称")
    clips = {}
    for name in names:
        matches = [a for a in project.animations if a.name == name or a.uuid == name]
        if len(matches) != 1:
            raise ValidationError(f"动画不存在或名称重复：{name}")
        clips[name] = matches[0]
    transitions = transitions or []
    for t in transitions:
        if set(t) != {"from", "to"} or t["from"] not in clips or t["to"] not in clips:
            raise ValidationError("transition 必须含 from/to，且两者均在 animations 中")
    plans = {}
    for name, a in clips.items():
        length = finite_number(a.length, "animation.length", minimum=0)
        count = max(1, math.ceil(length * rate))
        if count > 2000:
            raise ValidationError("采样过多：降低 sample_rate 或拆分动画")
        times = {0., length, max(0., length - 1/rate), min(length, 1/rate)}
        times.update(length * i / count for i in range(count + 1))
        for ator in a.animators.values():
            for k in ator.keyframes:
                if 0 <= k.time <= length:
                    times.add(k.time)
                    # Cover the left limit of Step discontinuities as well.
                    times.add(max(0., k.time - 1e-5))
        plans[name] = sorted(times)
    if sum(map(len, plans.values())) > 5000:
        raise ValidationError("总采样超过 5000；降低采样频率或分批检查")
    requests = [{"animation": n, "time": t} for n, times in plans.items() for t in times]
    states: dict[str, dict[float, dict]] = {n: {} for n in names}
    lo, hi = [math.inf]*3, [-math.inf]*3
    provenance = None
    violations, violation_count = [], 0
    for offset in range(0, len(requests), 32):
        batch = requests[offset:offset+32]
        result = sample_native(batch, floor=floor, floor_tolerance=floor_tolerance,
                               contact_exclude_prefixes=contact_exclude_prefixes)
        provenance = result["viewport"]
        for request, s in zip(batch, result["samples"]):
            states[request["animation"]][request["time"]] = s
            if s["bounds"]:
                lo = [min(x, y) for x, y in zip(lo, s["bounds"]["min"])]
                hi = [max(x, y) for x, y in zip(hi, s["bounds"]["max"])]
            violation_count += len(s["below_floor"])
            if s["below_floor"] and len(violations) < 100:
                violations.append({"animation": s["animation"], "time": s["time"], "elements": s["below_floor"][:20]})
    if not all(math.isfinite(v) for v in lo+hi):
        raise ValidationError("所选动画中没有可见的导出几何")
    def seam(left, right):
        a, b = clips[left], clips[right]
        sa, sb = states[left], states[right]
        return {**pose_delta(sa[a.length], sb[0.]),
                "velocity_difference": velocity_delta(sa[max(0., a.length-1/rate)], sa[a.length], sb[0.], sb[min(b.length, 1/rate)])}
    loops = {n: seam(n, n) for n, a in clips.items() if a.loop == "loop"}
    links = [{"from": t["from"], "to": t["to"], **seam(t["from"], t["to"])} for t in transitions]
    bounds = {"min": lo, "max": hi}
    return {"viewport": provenance, "sample_count": len(requests), "sample_rate": rate,
            "bounds": bounds, "render_bounds": visible_bounds(bounds, margin), "bounds_margin": margin,
            "loops": loops, "transitions": links, "floor_violation_count": violation_count,
            "floor_violations": violations, "floor_report_truncated": violation_count > sum(len(v["elements"]) for v in violations),
            "contact_exclude_prefixes": contact_exclude_prefixes or [],
            "limitations": ["离散采样并覆盖关键帧左右边界；不保证采样之间的极值，导出另加 margin",
                            "速度差是单侧差分诊断，不等同于动作自然度评分",
                            "仅原生 Cuboid 骨骼；不模拟控制器、IK、粒子、音效和游戏运行时 Molang"]}


def render_native(project: BlockbenchProject, animations: list[str], path: str,
                  fps: int = 6, size: int = 512, camera: str = "hero", loop: bool = False) -> dict:
    """A sequence of native captures, fixed framing, atomic final GIF write."""
    import os
    import tempfile
    from pathlib import Path
    from PIL import Image
    if type(fps) is not int or not 1 <= fps <= 20 or type(size) is not int or not 64 <= size <= 768:
        raise ValidationError("fps 必须为 1..20 整数，size 为 64..768 整数")
    if camera not in ("hero", "front", "rear", "side", "top"):
        raise ValidationError("camera 必须为 hero/front/rear/side/top")
    target = Path(path).resolve()
    if target.suffix.lower() != ".gif" or not target.parent.is_dir():
        raise ValidationError("path 必须是已存在目录内的 .gif 文件")
    if not animations or len(animations) > 20:
        raise ValidationError("animations 需要 1..20 个动画名称，可重复以组成连续预览")
    frames_plan = []
    for name in animations:
        matches = [a for a in project.animations if a.name == name or a.uuid == name]
        if len(matches) != 1:
            raise ValidationError(f"动画不存在或名称重复：{name}")
        a = matches[0]
        length = finite_number(a.length, "animation.length", minimum=0)
        count = max(1, math.ceil(length * fps))
        if count + len(frames_plan) > 180:
            raise ValidationError("GIF 最多 180 帧；降低 fps 或缩短动画序列")
        frames_plan.extend((name, length*i/count, max(20, round(max(length, 1/fps)*1000/count/10)*10)) for i in range(count))
    proof = native_proof()
    # Union across clips prevents breathing/flying clips from changing camera scale.
    review = review_native(project, list(dict.fromkeys(animations)), sample_rate=max(8, fps))
    driver = RemoteDriver(timeout=30)
    images, durations = [], []
    with tempfile.TemporaryDirectory(prefix=".mcp-animation-", dir=target.parent) as temp:
        frame_path = str(Path(temp)/"frame.png")
        for name, time, duration in frames_plan:
            driver.capture(frame_path, dict(proof, animation=name, time=time, camera=camera, fit_bounds=review["bounds"]))
            with Image.open(frame_path) as im:
                rgba = im.convert("RGBA")
                backdrop = Image.new("RGBA", rgba.size, "#171422")
                backdrop.alpha_composite(rgba)
                backdrop.thumbnail((size,size), Image.Resampling.NEAREST)
                images.append(backdrop.convert("RGB"))
            durations.append(duration)
        # One shared palette avoids per-frame color flicker.
        atlas = Image.new("RGB", (size, size*min(8,len(images))))
        for i in range(min(8,len(images))):
            atlas.paste(images[round(i*(len(images)-1)/max(1,min(8,len(images))-1))], (0,i*size))
        palette = atlas.quantize(colors=256)
        indexed = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
        staged = Path(temp)/"preview.gif"
        options = dict(save_all=True, append_images=indexed[1:], duration=durations, disposal=2, optimize=False)
        if loop: options["loop"] = 0
        indexed[0].save(staged, **options)
        os.replace(staged,target)
    return {"path": str(target), "frames": len(images), "duration_ms": sum(durations), "loop": loop,
            "camera": camera, "fit_bounds": review["bounds"], "viewport": proof,
            "limitations": review["limitations"], "sampling": "left-closed frame intervals; GIF timing rounded to 10ms"}
