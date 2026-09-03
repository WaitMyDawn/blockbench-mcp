"""GeckoLib (Java 版模组) 资源导出。

GeckoLib 的模型/动画文件本质就是 Bedrock 格式的 .geo.json / .animation.json，
只是目录约定为：
    assets/<modid>/geo/<category>/<name>.geo.json
    assets/<modid>/animations/<category>/<name>.animation.json
    assets/<modid>/textures/<category>/<name>.png
这里复用 bedrock 导出器生成内容，并负责按该布局落盘 + 抽取内嵌纹理。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from ..document import BlockbenchProject
from ..errors import ExportError
from . import bedrock


def _write_json(data: dict[str, Any], path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ExportError(f"无法写入 {path}", str(exc)) from exc


def _decode_source(data_url: str) -> bytes | None:
    if not data_url.startswith("data:image/png;base64,"):
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None


def export_geckolib(
    project: BlockbenchProject,
    output_dir: str,
    *,
    modid: str = "mymod",
    category: str = "item",
    name: str | None = None,
) -> dict[str, Any]:
    """按 GeckoLib 资源布局导出模型/动画/纹理。"""
    if not modid or not modid.strip():
        raise ExportError("modid 不能为空", "例如 mymod（对应模组 assets 目录名）")
    model_name = (name or project.model_identifier.removeprefix("geometry.") or project.name).strip()
    if not model_name:
        raise ExportError("模型名称不能为空", "请给项目命名或传 name 参数")

    written: list[str] = []
    geo_path = os.path.join(output_dir, "assets", modid, "geo", category, f"{model_name}.geo.json")
    _write_json(bedrock.geometry_json(project), geo_path)
    written.append(geo_path)

    anim_path = None
    if project.animations:
        anim_path = os.path.join(output_dir, "assets", modid, "animations", category, f"{model_name}.animation.json")
        _write_json(bedrock.animation_json(project), anim_path)
        written.append(anim_path)

    textures: dict[str, str] = {}
    for tex in project.textures:
        png: bytes | None = None
        if tex.source_data:
            png = _decode_source(tex.source_data)
        elif tex.source_path and os.path.isfile(tex.source_path):
            try:
                with open(tex.source_path, "rb") as fh:
                    png = fh.read()
            except OSError as exc:
                raise ExportError(f"读取纹理失败 {tex.source_path}", str(exc)) from exc
        if not png:
            continue
        stem = tex.name.rsplit(".", 1)[0]
        folder = tex.folder.strip("/")
        rel = os.path.join(*folder.split("/")) if folder else ""
        tex_path = os.path.join(output_dir, "assets", modid, "textures", category, rel, f"{stem}.png")
        try:
            os.makedirs(os.path.dirname(tex_path), exist_ok=True)
            with open(tex_path, "wb") as fh:
                fh.write(png)
        except OSError as exc:
            raise ExportError(f"无法写入纹理 {tex_path}", str(exc)) from exc
        written.append(tex_path)
        textures[tex.name] = f"assets/{modid}/textures/{category}/{stem}.png"

    return {
        "output_dir": output_dir,
        "modid": modid,
        "category": category,
        "model_name": model_name,
        "identifier": bedrock.geometry_identifier(project),
        "written": written,
        "textures": textures,
        "animations": [a.name for a in project.animations],
        "note": "模组代码中的 GeoModel 路径示例：geo/xxx.geo.json、animations/xxx.animation.json",
    }

