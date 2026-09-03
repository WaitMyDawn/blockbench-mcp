"""stdio 握手冒烟：启动 main.py 并请求 tools/list。

用法：
    .\.venv\Scripts\python.exe scripts\smoke_stdio.py
成功输出工具数量；失败会打印服务端 stderr。
"""

from __future__ import annotations

import json
import os
import subprocess


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python = os.path.join(root, ".venv", "Scripts", "python.exe")
    proc = subprocess.Popen(
        [python, os.path.join(root, "main.py")],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def recv() -> dict:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line:
            err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            raise RuntimeError(f"服务端提前退出: {err}")
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "smoke", "version": "0"}}})
    recv()
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = recv()["result"]["tools"]
    print(f"握手成功，发现 {len(tools)} 个工具")
    proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
