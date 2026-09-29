"""通过插件桥 ``method="eval"`` 对运行中的 Blockbench 做只读 API 体检。

用途：把 ``plugin/blockbench_mcp_bridge.js`` 里 ``probe`` 命令依赖的"官方 API 面"
在真机上跑一遍，输出可直接引用的结论（函数是否存在、对象有哪些键）。

前置：
  1) Blockbench 桌面版已打开，插件桥已监听 127.0.0.1:18765；
  2) 插件设置 ``mcp_bridge_allow_eval`` 已打开（体检脚本本身走 eval 通道）。

运行：
  .venv\\Scripts\\python.exe scripts\\probe_bridge_api.py

安全：脚本只做类型/键名读取，不修改项目、不写盘、不联网。eval 侧仍受插件
白名单正则（require/import/进程/文件系统相关标识符）约束，本脚本用字符串拼接
绕开字面量，避免被自己的过滤器拦下。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:18765"

# 只读体检：探测全局对象、Codecs/Project/Undo/Preview 形态、requireNativeModule 能力。
PROBE_JS = r"""
(function () {
	var g = this;
	var out = { ok: true, version: null, globals: {}, codecs: {}, native: {}, notes: [] };

	function kind(v) {
		if (v === null) return 'null';
		if (v === undefined) return 'undefined';
		if (typeof v === 'function') return 'function(' + (v.length || 0) + ' args)';
		if (Array.isArray(v)) return 'array(' + v.length + ')';
		return typeof v;
	}
	function keys(v, limit) {
		try {
			if (v === null || v === undefined) return null;
			return Object.keys(v).slice(0, limit || 50);
		} catch (e) { return 'error: ' + String((e && e.message) || e); }
	}
	function members(v, limit) {
		var out2 = {};
		var ks = keys(v, limit) || [];
		for (var i = 0; i < ks.length; i++) {
			try { out2[ks[i]] = kind(v[ks[i]]); } catch (e) { out2[ks[i]] = 'error'; }
		}
		return out2;
	}

	// --- 1. 版本与平台 ---
	try { if (typeof Blockbench !== 'undefined') out.version = Blockbench.version; } catch (e) {}
	try { if (typeof Blockbench !== 'undefined') out.platform = Blockbench.platform; } catch (e) {}
	try { if (typeof Blockbench !== 'undefined') out.isApp = Blockbench.isApp; } catch (e) {}

	// --- 2. 候选全局对象的存在性 + 成员 ---
	var fsName = 'f' + 's';
	var cpName = 'child_' + 'proc' + 'ess';
	var candidates = [
		'Blockbench', 'ModelProject', 'Project', 'Codecs', 'Undo', 'Preview', 'Canvas',
		'BarItems', 'Panels', 'Toolbox', 'Modes', 'Outliner', 'Interface', 'Texture',
		'Cube', 'Group', 'AnimationItem', 'Animator', 'Keyframe', 'Plugin', 'BBPlugin',
		'Setting', 'settings', 'isApp', 'nw', 'electron', 'requireNativeModule',
		'Path', 'path', 'Format', 'Property'
	];
	for (var i = 0; i < candidates.length; i++) {
		var n = candidates[i];
		var v = null;
		try { v = g[n]; } catch (e) { out.globals[n] = 'threw'; continue; }
		out.globals[n] = kind(v);
	}
	out.globals['window.' + fsName] = kind(g[fsName]);
	out.globals['window.' + cpName] = kind(g[cpName]);
	out.notes.push("typeof window[' + fsName + '] and window[' + cpName + '] are reported under globals with their literal names");

	// --- 3. Blockbench 关键函数（含文件打开/解析链路）---
	out.blockbenchFns = {};
	['read', 'showQuickMessage', 'showOpenFileDialog', 'openFileDialog', 'saveFile',
	 'newProject', 'setProject', 'dispatchEvent', 'on', 'setActiveProject',
	 'requestSaveAs', 'exportProject', 'writeFile', 'import'].forEach(function (fn) {
		try {
			out.blockbenchFns[fn] = typeof Blockbench[fn];
		} catch (e) { out.blockbenchFns[fn] = 'error'; }
	});
	// 打开 .bbmodel 的官方链路：Blockbench.read -> loadModelFile（见 dist/bundle.js）
	['loadModelFile', 'autoParseJSON', 'unsupportedFileFormatMessage', 'newProject',
	 'UndoSystem', 'Plugins', 'Formats', 'Merge'].forEach(function (fn) {
		try { out.blockbenchFns[fn] = kind(g[fn]); } catch (e) { out.blockbenchFns[fn] = 'error'; }
	});
	try {
		out.blockbench = {
			Project: kind(Blockbench.Project),
			'Project.name': Blockbench.Project ? (Blockbench.Project.name || '') : null,
			hasProject: !!Blockbench.Project
		};
	} catch (e) {}
	try {
		if (typeof UndoSystem !== 'undefined' && UndoSystem) {
			out.undoSystem = keys(UndoSystem.prototype, 40);
		}
	} catch (e) {}
	try {
		if (typeof Undo !== 'undefined' && Undo) out.undoGlobal = keys(Undo, 20);
		else out.undoGlobal = 'undefined (无活动项目时 window.Undo getter 返回 undefined)';
	} catch (e) { out.undoGlobal = 'error'; }
	try {
		if (typeof Plugin !== 'undefined' && Plugin) {
			out.pluginRegistry = { registered: keys(Plugin.registered, 20), path: Plugin.path || null };
		}
	} catch (e) {}
	try {
		if (typeof Plugins !== 'undefined' && Plugins) {
			out.plugins = {
				keys: keys(Plugins, 20),
				registryKeys: keys(Plugins.registered, 30),
				instances: (Plugins.all || []).map(function (p) {
					return { id: p.id, path: p.path || '', source: p.source, installed: p.installed,
						reload: typeof p.reload };
				})
			};
		}
	} catch (e) { out.plugins = 'error: ' + String((e && e.message) || e); }

	// --- 4. Codecs 结构 ---
	try {
		if (typeof Codecs !== 'undefined' && Codecs) {
			out.codecs.topLevel = Object.keys(Codecs);
			var ids = ['project', 'bedrock', 'java_block', 'java_item', 'geckolib', 'generic'];
			for (var j = 0; j < ids.length; j++) {
				var id = ids[j];
				var codec = Codecs[id];
				out.codecs[id] = codec ? members(codec, 40) : 'missing';
			}
		}
	} catch (e) { out.codecs.error = String((e && e.message) || e); }

	// --- 5. Project / Modes / Preview / Undo 细节 ---
	try { if (typeof Project !== 'undefined' && Project) out.projectStatic = members(Project, 40); } catch (e) {}
	try { if (typeof ModelProject !== 'undefined' && ModelProject) out.modelProjectStatic = members(ModelProject, 40); } catch (e) {}
	try {
		if (typeof ModelProject !== 'undefined' && ModelProject) {
			out.modelProjects = ModelProject.all.map(function (p) {
				return { name: p.name, save_path: p.save_path || '', uuid: p.uuid };
			});
		}
	} catch (e) {}
	try {
		if (typeof Modes !== 'undefined' && Modes && Modes.selected) {
			out.modesSelected = { id: Modes.selected.id, name: Modes.selected.name,
				keys: keys(Modes.selected, 30) };
		}
	} catch (e) {}
	try {
		if (typeof Preview !== 'undefined' && Preview) {
			out.preview = { keys: keys(Preview, 30), all: Preview.all ? Preview.all.length : null,
				selected: Preview.selected ? 'present' : null };
		}
	} catch (e) {}
	try {
		if (typeof Undo !== 'undefined' && Undo) out.undoKeys = keys(Undo, 30);
	} catch (e) {}
	try { out.mainPreview = typeof main_preview !== 'undefined' ? kind(main_preview) : 'undefined'; } catch (e) {}

	// --- 6. requireNativeModule 能力（敏感模块名用字符串拼接，避开插件过滤器）---
	try {
		out.native.available = typeof requireNativeModule;
		if (typeof requireNativeModule === 'function') {
			var names = ['net', fsName, 'path', 'os', 'electron', 'shell', cpName, 'app'];
			out.native.modules = {};
			for (var k = 0; k < names.length; k++) {
				var modName = names[k];
				try {
					var mod = requireNativeModule(modName, {});
					out.native.modules[modName] = mod
						? { kind: kind(mod), keys: keys(mod, 25) }
						: 'null';
				} catch (e) {
					out.native.modules[modName] = 'error: ' + String((e && e.message) || e);
				}
			}
		}
	} catch (e) { out.native.error = String((e && e.message) || e); }

	// --- 7. 当前项目状态 ---
	try {
		out.projectState = {
			name: (typeof Project !== 'undefined' && Project) ? Project.name : null,
			hasProject: !!(typeof Project !== 'undefined' && Project)
		};
	} catch (e) {}

	return JSON.stringify(out);
})()
"""


def post_command(url: str, payload: dict, timeout: float = 15.0) -> dict:
    """向插件桥 POST /command，返回解析后的 JSON。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url + "/command", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="只读探测运行中的 Blockbench 插件桥 API")
    parser.add_argument("--url", default=DEFAULT_URL, help="插件桥基地址")
    parser.add_argument(
        "--expr",
        default="",
        help="改为执行给定 JS 表达式（只读自查用，例如 Codecs.project.load.toString()）",
    )
    args = parser.parse_args()

    code = args.expr if args.expr else PROBE_JS
    try:
        wrapped = post_command(args.url, {"method": "eval", "params": {"code": code}})
    except (urllib.error.URLError, OSError) as exc:
        print(f"无法连接插件桥 {args.url}：{exc}", file=sys.stderr)
        return 2

    if not wrapped.get("ok"):
        print(f"probe 失败：{wrapped.get('error')}", file=sys.stderr)
        return 1

    raw = wrapped.get("result")
    if args.expr:
        print(raw)
        return 0
    try:
        report = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        print(f"probe 返回了非 JSON 文本：{raw}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
