"""Blockbench MCP Server 入口（stdio）。

运行：
    python main.py
客户端（Codex/Claude Desktop 等）以 stdio 方式启动该进程并自动发现工具。
"""

from __future__ import annotations

import argparse

from blockbench_mcp import tools  # noqa: F401  注册全部 MCP 工具


def main() -> None:
    parser = argparse.ArgumentParser(description="Blockbench MCP Server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP 传输方式（默认 stdio，本地开发推荐）",
    )
    args = parser.parse_args()
    transport = {"streamable-http": "streamable-http"}.get(args.transport, args.transport)
    tools.get_server().run(transport=transport)


if __name__ == "__main__":
    main()

