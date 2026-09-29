"""插件桥双副本同步：仓库 ↔ %APPDATA%\\Blockbench\\plugins。

背景：Blockbench 只会加载它自己记录的那一份 ``blockbench_mcp_bridge.js``
（``Plugins.registered['blockbench_mcp_bridge'].path``，可能是仓库副本，
也可能是 %APPDATA% 副本）。改完仓库那份如果没同步/没重载，真机上跑的还是旧代码。

用法：
  python scripts/sync_bridge_plugin.py --check        # 只看是否一致（不一致退出码 1）
  python scripts/sync_bridge_plugin.py                # 仓库 -> %APPDATA%（默认）
  python scripts/sync_bridge_plugin.py --direction pull
  python scripts/sync_bridge_plugin.py --reload       # 同步后让运行中的插件热重载

安全：覆盖前默认备份目标文件为 ``<name>.bak_YYYYmmdd_HHMMSS``（``--no-backup`` 关闭）；
``--reload`` 只发一条 ``reload_self`` 命令给本机插件桥，不改项目、不写盘。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_NAME = "blockbench_mcp_bridge.js"
DEFAULT_BRIDGE_URL = "http://127.0.0.1:18765"


def repo_plugin_path() -> Path:
    """仓库内插件源文件：<repo>/plugin/blockbench_mcp_bridge.js。"""
    return Path(__file__).resolve().parents[1] / "plugin" / PLUGIN_NAME


def appdata_plugin_path() -> Path:
    """Blockbench 用户插件目录副本：%APPDATA%\\Blockbench\\plugins\\...。"""
    roaming = os.environ.get("APPDATA")
    base = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
    return base / "Blockbench" / "plugins" / PLUGIN_NAME


def content_key(path: Path) -> str:
    """按“忽略 CRLF/LF 差异”的方式计算内容指纹；文件不存在返回空串。"""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def short_digest(path: Path) -> str:
    key = content_key(path)
    return key[:12] if key else "-"


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = path.with_name(f"{path.name}.bak_{stamp}")
    shutil.copy2(path, target)
    return target


def copy_plugin(source: Path, target: Path, *, make_backup: bool) -> str:
    """覆盖式同步；返回一句人类可读的结果描述。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if target.exists() and make_backup:
        note = f"（已备份 {backup(target).name}）"
    shutil.copyfile(source, target)
    return f"{source} -> {target}{note}"


def request_reload(url: str, timeout: float = 5.0) -> dict:
    """请求插件桥热重载自身（需要插件已支持 reload_self 命令）。"""
    body = json.dumps({"method": "reload_self", "params": {}}).encode("utf-8")
    req = urllib.request.Request(
        url + "/command", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def print_status(repo: Path, target: Path) -> bool:
    """打印两副本状态，返回是否一致。"""
    repo_ok, target_ok = repo.exists(), target.exists()
    print(f"仓库副本   : {repo}  [{short_digest(repo) if repo_ok else 'missing'}]")
    print(f"APPDATA 副本: {target}  [{short_digest(target) if target_ok else 'missing'}]")
    if not repo_ok or not target_ok:
        print("状态       : 有一侧缺失，需要同步")
        return False
    same = content_key(repo) == content_key(target)
    print(f"状态       : {'一致' if same else '不一致（需同步）'}")
    return same


def main() -> int:
    parser = argparse.ArgumentParser(description="Blockbench MCP 桥插件双副本同步")
    parser.add_argument("--repo", default="", help="仓库副本路径（默认自动定位）")
    parser.add_argument("--target", default="", help="Blockbench 插件目录副本路径")
    parser.add_argument("--direction", choices=("push", "pull"), default="push",
                        help="push = 仓库 -> APPDATA（默认）；pull = APPDATA -> 仓库")
    parser.add_argument("--check", action="store_true", help="只检查一致性，不写文件")
    parser.add_argument("--no-backup", action="store_true", help="覆盖前不备份")
    parser.add_argument("--reload", action="store_true", help="同步后请求插件桥热重载")
    parser.add_argument("--url", default=DEFAULT_BRIDGE_URL, help="插件桥基地址")
    args = parser.parse_args()

    repo = Path(args.repo) if args.repo else repo_plugin_path()
    target = Path(args.target) if args.target else appdata_plugin_path()

    same = print_status(repo, target)
    if args.check:
        return 0 if same else 1

    source, dest = (repo, target) if args.direction == "push" else (target, repo)
    if same:
        print("两副本内容一致，无需复制")
    else:
        if not source.exists():
            print(f"源文件不存在：{source}", file=sys.stderr)
            return 2
        print(f"同步       : {copy_plugin(source, dest, make_backup=not args.no_backup)}")

    if args.reload:
        try:
            result = request_reload(args.url)
        except (urllib.error.URLError, OSError) as exc:
            print(f"热重载失败：插件桥不可达（{exc}）；可在 Blockbench 里手动 reload 插件", file=sys.stderr)
            return 1
        if result.get("ok"):
            print(f"热重载     : {result.get('reloaded_from', '') or '(未报告路径)'}")
        else:
            print(f"热重载失败：{result.get('error')}", file=sys.stderr)
            print("提示：运行中的插件可能是旧版本（无 reload_self 命令），"
                  "请在 Blockbench 里手动 reload 一次该插件。", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
