"""命令行入口：不经过 MCP 客户端也能直接使用文档引擎。

示例：
    python -m blockbench_mcp.cli open "d:/Ps_2022_PSD/item/blockbench/Redeemer.bbmodel" --ascii
    python -m blockbench_mcp.cli render --out preview.png
    python -m blockbench_mcp.cli export --target geckolib --out ./pack --modid mymod
"""

from __future__ import annotations

import argparse
import os
import sys

from . import preview as preview_renderer
from . import quality as quality_checker
from .codecs import bbmodel as bbmodel_codec
from .codecs import geckolib as geckolib_codec
from .session import session


def _open(path: str, ascii_cells: int | None) -> None:
    project = bbmodel_codec.read(path)
    session.project = project
    session.save_path = path
    session.dirty = False
    print(f"已打开 {path}")
    print(f"格式: {project.format_id} | 摘要: {project.summary()['counts']}")
    issues = project.validate()
    print(f"一致性检查: {len(issues)} 个问题")
    if ascii_cells:
        print_ascii(ascii_cells)


def print_ascii(cells: int) -> None:
    project = session.require_project()
    views = preview_renderer.ascii_views(project, cells=cells)
    for name in ("front", "side", "top"):
        print(f"--- {name} ---")
        for row in views[name]:
            print(row)


def render(out: str, width: int, height: int, yaw: float, pitch: float) -> None:
    project = session.require_project()
    png = preview_renderer.render_png(project, width=width, height=height, yaw=yaw, pitch=pitch)
    if not out:
        out = os.path.join(os.getcwd(), f"{project.name or 'model'}_preview.png")
    with open(out, "wb") as fh:
        fh.write(png)
    print(f"预览已保存: {out} ({len(png)} bytes)")


def quality(cells_note: bool = False) -> None:
    project = session.require_project()
    report = quality_checker.quality_report(project)
    print(f"评分: {report['score']}/100 | 判定: {report['verdict']} | {report['counts']}")
    for item in report["issues"][:30]:
        print(f"  [{item['level']}] {item['message']}" + (f"（{item['hint']}）" if item["hint"] else ""))


def inspect(path: str, cells: int | None = None) -> None:
    """打开文件并立即输出质检与（可选）ASCII 视图。"""
    _open(path, None)
    print()
    quality()
    if cells:
        print_ascii(cells)


def export(args: argparse.Namespace) -> None:
    project = session.require_project()
    if args.target == "bbmodel":
        bbmodel_codec.write(project, args.out)
        print(f"已保存 .bbmodel: {args.out}")
    elif args.target == "geckolib":
        result = geckolib_codec.export_geckolib(
            project, args.out, modid=args.modid, category=args.category, name=args.model_name
        )
        for path in result["written"]:
            print("written:", path)
    else:
        raise SystemExit(f"CLI 暂支持 target=bbmodel|geckolib，收到 {args.target}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="blockbench-mcp.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="打开 .bbmodel 文件")
    p_open.add_argument("path")
    p_open.add_argument("--ascii", action="store_true", help="打开后打印 ASCII 三视图")
    p_open.add_argument("--cells", type=int, default=40)

    p_inspect = sub.add_parser("inspect", help="打开文件并输出质检报告")
    p_inspect.add_argument("path")
    p_inspect.add_argument("--ascii", action="store_true")
    p_inspect.add_argument("--cells", type=int, default=40)

    sub.add_parser("status", help="查看当前项目摘要")
    sub.add_parser("quality", help="无视觉质检")
    p_ascii = sub.add_parser("ascii", help="ASCII 三视图")
    p_ascii.add_argument("--cells", type=int, default=40)

    p_render = sub.add_parser("render", help="渲染 PNG")
    p_render.add_argument("--out", default="")
    p_render.add_argument("--width", type=int, default=640)
    p_render.add_argument("--height", type=int, default=640)
    p_render.add_argument("--yaw", type=float, default=45.0)
    p_render.add_argument("--pitch", type=float, default=28.0)

    p_export = sub.add_parser("export", help="导出（bbmodel/geckolib）")
    p_export.add_argument("--target", choices=("bbmodel", "geckolib"), default="bbmodel")
    p_export.add_argument("--out", required=True)
    p_export.add_argument("--modid", default="mymod")
    p_export.add_argument("--category", default="item")
    p_export.add_argument("--model-name", dest="model_name", default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "open":
            _open(args.path, args.cells if args.ascii else None)
        elif args.command == "inspect":
            inspect(args.path, args.cells if args.ascii else None)
        elif args.command == "status":
            print(session.require_project().summary())
        elif args.command == "quality":
            quality()
        elif args.command == "ascii":
            print_ascii(args.cells)
        elif args.command == "render":
            render(args.out, args.width, args.height, args.yaw, args.pitch)
        elif args.command == "export":
            export(args)
    except Exception as exc:  # 领域错误统一带提示输出
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
