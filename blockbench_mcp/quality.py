"""无视觉建模质检：给不支持看图的模型一套可执行的结构/几何体检。

只做"能由坐标与关系证明"的检查：空工程/退化立方体/命名/贴图缺失/
UV 越界/空骨骼/动画引用缺失/同层穿插（AABB 近似）等。
结论是"体检报告"，不是审美评价。
"""

from __future__ import annotations

from typing import Any

from .document import BlockbenchProject


def _aabb_overlap(a_min: list[float], a_max: list[float], b_min: list[float], b_max: list[float]) -> bool:
    return all(a_min[i] < b_max[i] - 1e-6 and b_min[i] < a_max[i] - 1e-6 for i in range(3))


def quality_report(project: BlockbenchProject, *, max_issues: int = 60) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    def add(level: str, code: str, message: str, hint: str | None = None) -> None:
        issues.append({"level": level, "code": code, "message": message, "hint": hint})

    # ---- 基础体量
    if not project.elements:
        add("error", "empty_project", "项目没有任何立方体", "先 cube_create 或 project_load_example 加载模板")

    # ---- 立方体
    generic_count = 0
    degenerate = 0
    for el in project.elements:
        size = el.size()
        zero_axes = [i for i in range(3) if size[i] <= 1e-6]
        if zero_axes:
            degenerate += 1
            if degenerate <= 5:
                add("warning", "degenerate_cube", f"元素 {el.name} 存在零厚度轴 {zero_axes}", "扁平装饰可用，但导出某些引擎可能异常")
        if el.name == "cube":
            generic_count += 1
    if generic_count > 3:
        add("warning", "generic_names", f"有 {generic_count} 个元素仍叫 cube", "给部件起语义化名字便于定位与动画绑定")

    # ---- 命名重复（按名字引用会歧义）
    names: dict[str, int] = {}
    for obj in list(project.groups) + list(project.elements):
        names[obj.name] = names.get(obj.name, 0) + 1
    dupes = [n for n, c in names.items() if c > 1]
    if dupes:
        add("warning", "duplicate_names", f"名称重复：{', '.join(dupes[:6])}", "MCP 工具支持 uuid/名称寻址，重名时建议改用 uuid")

    # ---- 骨骼树
    for g in project.groups:
        descendants: set[str] = set()

        def collect(uids: list[str]) -> None:
            for uid in uids:
                descendants.add(uid)
                child = project.find_group_by_uuid(uid)
                if child:
                    collect(child.children)

        collect(g.children)
        if not descendants:
            add("warning", "empty_bone", f"骨骼 {g.name} 没有任何子内容", "导出可能被跳过；删除或放入几何")

    # ---- 贴图
    if project.textures:
        untextured = 0
        for el in project.elements:
            if el.box_uv:
                continue
            if not any(face.texture for face in el.faces.values()):
                untextured += 1
        if untextured:
            add("warning", "untextured_elements", f"{untextured} 个元素没有任何面贴图", "用 texture_assign 指定纹理")
        tex_by_uuid = {t.uuid: t for t in project.textures}
        for el in project.elements:
            for key, face in el.faces.items():
                if not face.texture:
                    continue
                tex = tex_by_uuid.get(face.texture)
                if tex is not None:
                    max_u = max(face.uv[0], face.uv[2])
                    max_v = max(face.uv[1], face.uv[3])
                    if max_u > tex.width + 1e-6 or max_v > tex.height + 1e-6:
                        add("warning", "uv_out_of_bounds", f"元素 {el.name} 的 {key} 面 UV 超出纹理 {tex.width}x{tex.height}",
                            "调整 uv_offset 或放大纹理")

    # ---- 同层穿插（仅无旋转、同一父级下的 AABB 近似，装饰性重叠属正常）
    containers: list[list[str]] = [project.root_children] + [g.children for g in project.groups]
    overlap_reported = 0
    for uids in containers:
        els = [project.find_element_by_uuid(uid) for uid in uids]
        els = [e for e in els if e is not None and all(abs(x) < 1e-9 for x in e.rotation)]
        for i in range(len(els)):
            for j in range(i + 1, len(els)):
                a, b = els[i], els[j]
                if _aabb_overlap(a.from_, a.to, b.from_, b.to):
                    overlap_reported += 1
                    if overlap_reported <= 8:
                        add("info", "overlap", f"{a.name} 与 {b.name} 包围盒重叠", "装饰性嵌入可忽略；否则移动部件避免穿模")

    # ---- 动画
    for anim in project.animations:
        if not anim.animators:
            add("warning", "empty_animation", f"动画 {anim.name} 没有任何骨骼通道", "用 keyframe_add 添加关键帧")
            continue
        for bone_uuid, ator in anim.animators.items():
            if not project.find_group_by_uuid(bone_uuid):
                add("error", "missing_anim_bone", f"动画 {anim.name} 引用了不存在的骨骼 {bone_uuid}", "bone_delete 会清理动画；此问题多来自手工编辑")
            if not ator.keyframes:
                add("warning", "empty_animator", f"动画 {anim.name} 的骨骼 {ator.bone_name} 没有关键帧")

    # ---- 汇总
    level_count = {"error": 0, "warning": 0, "info": 0}
    for item in issues:
        level_count[item["level"]] += 1
    score = max(0, 100 - 15 * level_count["error"] - 5 * level_count["warning"] - 1 * level_count["info"])
    verdict = "good" if score >= 85 else "acceptable" if score >= 60 else "needs_work"
    if level_count["error"]:
        verdict = "needs_work"
    return {
        "score": score,
        "verdict": verdict,
        "counts": level_count,
        "issues": issues[:max_issues],
        "total_issues": len(issues),
        "metrics": {
            "elements": len(project.elements),
            "groups": len(project.groups),
            "textures": len(project.textures),
            "animations": len(project.animations),
            "degenerate_cubes": degenerate,
        },
    }
