/**
 * Blockbench MCP Bridge - Phase 2 (desktop variant)
 *
 * Protocol (aligned with blockbench_mcp/drivers/remote.py):
 *   GET  /health       -> {"ok": true, "blockbench_version": "...", ...}
 *   POST /command      -> body {"method": "open"|"reload"|"undo"|"eval"|"health"|"probe"|"capture", "params": {...}}
 *   GET  /screenshot   -> image/png bytes of the current viewport
 *
 * 实现说明（Blockbench 5.1.6 真机实测）：
 *  - 插件代码由 Blockbench 以 `new Function("requireNativeModule", "require", code)` 装载，
 *    所以 `requireNativeModule` 只存在于插件作用域，`eval` 通道里 `typeof requireNativeModule`
 *    是 "undefined"；探测原生模块能力必须走本文件的 probe 命令。
 *  - 5.1.6 没有 Codecs/Filesystem/Project 这几个原生模块，打开文件要走官方全局链路：
 *    `Blockbench.read([path], {}, files => loadModelFile(files[0]))`（与 Blockbench 自身
 *    “最近项目”/`open-model` IPC 同一条路径）。`loadModelFile` 按扩展名分发到对应 Codec。
 *  - `window.Undo` 是 getter，返回 `Blockbench.Project?.undo`（即活动项目的 UndoSystem 实例），
 *    没有项目时为 undefined；撤销要调 `Undo.undo()`。
 *  - 桌面端不暴露 Node 的 `http` 模块，所以 HTTP/1.1 服务仍用
 *    `requireNativeModule("net", ...)` 的原始 TCP（该权限在 plugin_permissions.json 已授权）。
 *  - `open` 只在官方链路不可用时如实报错，不伪造“已打开”。
 *  - open 直接调用官方 project codec 绕过同路径缓存：先验证输入，新工程与纹理
 *    就绪后才 await 关闭旧工程。默认保护未保存修改。capture 原子返回像素和版本凭证。
 */

var BRIDGE_PORT = 18765;
var BRIDGE_VERSION = '0.6.0';
// Blockbench.read 在文件读不到时只会弹错误框、不回调，所以这里自我兜底一个超时。
var READ_TIMEOUT_MS = 5000;

/**
 * 兼容旧注释里的说明：
 *  - Blockbench 5 desktop does not expose the Node.js `http` module, so this
 *    plugin runs a minimal HTTP/1.1 server over raw TCP obtained from
 *    requireNativeModule("net", ...). See jasonjgardner/blockbench-mcp-plugin
 *    for the same technique.
 *  - `eval` is disabled unless the user enables setting mcp_bridge_allow_eval.
 */

// --- small helpers ------------------------------------------------------

function notify(msg) {
	try {
		if (typeof Blockbench !== 'undefined' && typeof Blockbench.showQuickMessage === 'function') {
			Blockbench.showQuickMessage('[MCP Bridge] ' + msg);
		} else if (typeof showToast === 'function') {
			showToast(msg);
		}
	} catch (e) {
		// notification must never break the bridge
	}
}

function nativeModule(name) {
	try {
		if (typeof requireNativeModule === 'function') {
			return requireNativeModule(name, {});
		}
	} catch (e) {
		// fall through
	}
	return null;
}

function settingValue(key, fallback) {
	try {
		if (typeof settings === 'undefined' || !settings) return fallback;
		var root = settings.value || settings;
		var v = root[key];
		return (v === undefined || v === null) ? fallback : v;
	} catch (e) {
		return fallback;
	}
}

function normalizePort(v) {
	var n = Math.floor(Number(v));
	if (isFinite(n) && n >= 1024 && n <= 65535) return n;
	return BRIDGE_PORT;
}

function concatBytes(parts, total) {
	var out = new Uint8Array(total);
	var off = 0;
	for (var i = 0; i < parts.length; i++) {
		out.set(parts[i], off);
		off += parts[i].length;
	}
	return out;
}

function bytesToAscii(bytes, start, end) {
	var s = '';
	for (var i = start; i < end; i++) s += String.fromCharCode(bytes[i]);
	return s;
}

function decodeUtf8(bytes) {
	if (typeof TextDecoder === 'function') {
		try { return new TextDecoder('utf-8').decode(bytes); } catch (e) { /* fallback */ }
	}
	return bytesToAscii(bytes, 0, bytes.length);
}

function encodeUtf8(text) {
	if (typeof TextEncoder === 'function') return new TextEncoder().encode(text);
	var out = [];
	for (var i = 0; i < text.length; i++) {
		var code = text.charCodeAt(i);
		if (code < 128) { out.push(code); }
		else if (code < 2048) {
			out.push(192 | (code >> 6), 128 | (code & 63));
		} else if (code < 55296 || code >= 57344) {
			out.push(224 | (code >> 12), 128 | ((code >> 6) & 63), 128 | (code & 63));
		} else {
			var c2 = text.charCodeAt(++i);
			var cp = 65536 + ((code & 1023) << 10) + (c2 & 1023);
			out.push(240 | (cp >> 18), 128 | ((cp >> 12) & 63), 128 | ((cp >> 6) & 63), 128 | (cp & 63));
		}
	}
	return new Uint8Array(out);
}

function base64ToBytes(b64) {
	if (typeof atob !== 'function') return null;
	var bin;
	try { bin = atob(b64); } catch (e) { return null; }
	var out = new Uint8Array(bin.length);
	for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
	return out;
}

function findHeaderEnd(bytes) {
	for (var i = 0; i + 3 < bytes.length; i++) {
		if (bytes[i] === 13 && bytes[i + 1] === 10 && bytes[i + 2] === 13 && bytes[i + 3] === 10) return i;
	}
	return -1;
}

// --- minimal HTTP parser -------------------------------------------------

function parseHead(bytes, marker) {
	var text = bytesToAscii(bytes, 0, marker);
	var lines = text.split('\r\n');
	var first = lines[0] || '';
	var parts = first.split(' ');
	var headers = {};
	for (var i = 1; i < lines.length; i++) {
		var ci = lines[i].indexOf(':');
		if (ci <= 0) continue;
		var k = lines[i].slice(0, ci).trim().toLowerCase();
		var v = lines[i].slice(ci + 1).trim();
		headers[k] = headers[k] ? headers[k] + ', ' + v : v;
	}
	var cl = parseInt(headers['content-length'] || '0', 10);
	return {
		method: parts[0] || '',
		url: parts[1] || '',
		headers: headers,
		contentLength: isFinite(cl) && cl > 0 ? cl : 0
	};
}

function statusText(code) {
	var map = { 200: 'OK', 400: 'Bad Request', 404: 'Not Found', 500: 'Internal Server Error' };
	return map[code] || 'OK';
}

function respondJson(socket, code, obj) {
	var body = '';
	try { body = JSON.stringify(obj) || '{}'; } catch (e) { body = '{"ok":false,"error":"json stringify failed"}'; }
	var head = 'HTTP/1.1 ' + code + ' ' + statusText(code) + '\r\n' +
		'Content-Type: application/json; charset=utf-8\r\n' +
		'Content-Length: ' + encodeUtf8(body).length + '\r\n' +
		'Connection: close\r\n\r\n';
	socket.write(head);
	socket.write(encodeUtf8(body));
	try { socket.end(); } catch (e) { /* ignore */ }
}

function respondBytes(socket, code, contentType, bytes) {
	var head = 'HTTP/1.1 ' + code + ' ' + statusText(code) + '\r\n' +
		'Content-Type: ' + contentType + '\r\n' +
		'Content-Length: ' + bytes.length + '\r\n' +
		'Connection: close\r\n\r\n';
	socket.write(head);
	socket.write(bytes);
	try { socket.end(); } catch (e) { /* ignore */ }
}

// --- bridge capabilities -------------------------------------------------

/**
 * 路径比较：Windows 下大小写与分隔符都不敏感，尾部斜杠也要忽略。
 * 用来判断「目标 .bbmodel 是不是已经作为某个打开项目存在」。
 */
function samePath(left, right) {
	if (!left || !right) return false;
	function norm(p) {
		return String(p).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
	}
	return norm(left) === norm(right);
}

/** 当前项目的立方体数量；没有项目时返回 0。截图凭证要用它证明「截的是哪一版」。 */
function elementCount() {
	try {
		if (typeof Cube !== 'undefined' && Cube.all) return Cube.all.length;
	} catch (e) { /* Cube 不可用时按 0 处理 */ }
	return 0;
}

/** 当前活动项目的只读摘要；没有项目时各字段留空而不是抛错。 */
function projectSummary() {
	var summary = { name: '', save_path: '', open_projects: 0, format: '', elements: 0, uuid: '', saved: null };
	try {
		if (typeof ModelProject === 'undefined' || !ModelProject) return summary;
		if (ModelProject.all) summary.open_projects = ModelProject.all.length;
		var active = null;
		if (typeof Blockbench !== 'undefined' && Blockbench.Project && typeof Blockbench.Project === 'object') {
			active = Blockbench.Project;
		} else if (ModelProject.selected) {
			active = ModelProject.selected;
		} else if (ModelProject.all && ModelProject.all.length) {
			active = ModelProject.all[0];
		}
		if (active) {
			summary.uuid = active.uuid || '';
			summary.saved = active.saved;
			var stamp = active.mcp_bridge_sync || {};
			summary.project_id = stamp.project_id || null;
			summary.revision = stamp.revision === undefined ? null : stamp.revision;
			summary.source_token = stamp.source_token || null;
			summary.name = active.name || '';
			summary.project = summary.name;
			summary.save_path = active.save_path || '';
			try {
				if (typeof Format !== 'undefined' && Format && Format.id) summary.format = Format.id;
			} catch (e) { /* optional */ }
		}
	} catch (e) { /* 摘要失败不影响健康检查 */ }
	summary.elements = elementCount();
	return summary;
}

function blockbenchVersion() {
	try {
		if (typeof Blockbench !== 'undefined' && Blockbench.version) return Blockbench.version;
	} catch (e) { /* optional */ }
	return 'unknown';
}

function healthPayload(plugin) {
	var summary = projectSummary();
	return {
		ok: true,
		blockbench_version: blockbenchVersion(),
		project: summary.name,
		save_path: summary.save_path,
		elements: summary.elements,
		open_projects: summary.open_projects,
		format: summary.format,
		uuid: summary.uuid,
		saved: summary.saved,
		project_id: summary.project_id,
		revision: summary.revision,
		source_token: summary.source_token,
		phase: 'bridge-js',
		bridge_version: BRIDGE_VERSION,
		methods: ['health', 'probe', 'open', 'reload', 'undo', 'eval', 'screenshot', 'capture', 'animation_sample'],
		projects: typeof ModelProject !== 'undefined' && ModelProject.all ? ModelProject.all.map(function (p) {
			return {uuid: p.uuid, save_path: p.save_path || '', saved: p.saved};
		}) : []
	};
}

/**
 * 只读 API 体检：报告当前 Blockbench 到底暴露了哪些打开/截图/撤销链路。
 * 不修改项目、不写盘、不触发权限弹窗（只探测已授权的 net 模块）。
 */
function probeBridge(plugin) {
	var report = {
		ok: true,
		phase: 'bridge-js',
		bridge_version: BRIDGE_VERSION,
		port: plugin.port,
		allow_eval: Boolean(plugin.allowEval),
		read_only: true,
		blockbench: {
			version: blockbenchVersion(),
			platform: safeValue(function () { return Blockbench.platform; }, 'unknown'),
			is_app: safeValue(function () { return Boolean(Blockbench.isApp); }, null)
		},
		globals: {},
		file_open: {},
		screenshot: {},
		undo: {},
		native: {},
		project: projectSummary()
	};

	var globalNames = ['Blockbench', 'Codecs', 'ModelProject', 'Preview', 'Canvas',
		'Undo', 'UndoSystem', 'Formats', 'Plugins', 'settings'];
	for (var i = 0; i < globalNames.length; i++) {
		report.globals[globalNames[i]] = safeValue(function (n) {
			return typeof window[n];
		}, 'unknown', globalNames[i]);
	}

	// 打开 .bbmodel 的官方链路（Blockbench 桌面版内部用 Node fs，不需要插件授权）
	report.file_open = {
		blockbench_read: safeValue(function () { return typeof Blockbench.read; }, 'undefined'),
		load_model_file: safeValue(function () { return typeof loadModelFile; }, 'undefined'),
		auto_parse_json: safeValue(function () { return typeof autoParseJSON; }, 'undefined'),
		codecs_project_load: safeValue(function () { return typeof Codecs.project.load; }, 'undefined'),
		codecs_project_parse: safeValue(function () { return typeof Codecs.project.parse; }, 'undefined'),
		load_filter: safeValue(function () { return Codecs.project.load_filter; }, null),
		preferred: 'Blockbench.read([path], {errorbox:false}, files => loadModelFile(files[0]))'
	};

	report.screenshot = {
		preview_class: safeValue(function () { return typeof Preview; }, 'undefined'),
		preview_selected: safeValue(function () {
			return Preview.selected ? (Preview.selected.id || 'unnamed') : null;
		}, null),
		preview_count: safeValue(function () { return Preview.all ? Preview.all.length : null; }, null),
		has_canvas: safeValue(function () {
			return !!(Preview.selected && Preview.selected.canvas);
		}, false),
		has_render: safeValue(function () {
			return typeof (Preview.selected && Preview.selected.render);
		}, 'undefined'),
		canvas_without_gizmos: safeValue(function () { return typeof Canvas.withoutGizmos; }, 'undefined')
	};

	report.undo = {
		window_undo: safeValue(function () { return typeof Undo; }, 'undefined'),
		active_undo_system: safeValue(function () {
			return Boolean(Blockbench.Project && Blockbench.Project.undo);
		}, false),
		methods: safeValue(function () {
			return ownNames(UndoSystem.prototype);
		}, [])
	};

	report.native = {
		require_native_module: typeof requireNativeModule,
		net: Boolean(nativeModule('net')),
		note: 'requireNativeModule 只在插件作用域可见；其他模块名未探测，避免触发授权弹窗'
	};
	return report;
}

function safeValue(fn, fallback, arg) {
	try {
		var value = arg === undefined ? fn() : fn(arg);
		return value === undefined ? fallback : value;
	} catch (e) {
		return fallback;
	}
}

function ownNames(obj) {
	try {
		if (!obj) return [];
		return Object.getOwnPropertyNames(obj);
	} catch (e) {
		return [];
	}
}

function describeError(e) {
	return String((e && e.message) || e);
}

function capturePngBytes() {
	try {
		var PreviewM = nativeModule('Preview');
		var CanvasM = nativeModule('Canvas');
		var preview = null;
		if (PreviewM) {
			preview = PreviewM.selected ||
				(PreviewM.all && PreviewM.all.length ? PreviewM.all[0] : null) ||
				null;
		}
		if (!preview && typeof main_preview !== 'undefined') preview = main_preview;
		if (!preview) return null;
		if (typeof preview.render === 'function') preview.render();
		var canvas = preview.canvas || (preview.viewport && preview.viewport.canvas) || null;
		if (!canvas || typeof canvas.toDataURL !== 'function') return null;
		var wrap = (CanvasM && typeof CanvasM.withoutGizmos === 'function') ? CanvasM.withoutGizmos : null;
		var dataUrl = null;
		if (wrap) {
			wrap(function () { dataUrl = canvas.toDataURL('image/png'); });
		} else {
			dataUrl = canvas.toDataURL('image/png');
		}
		if (typeof dataUrl !== 'string') return null;
		var marker = 'base64,';
		var idx = dataUrl.indexOf(marker);
		if (idx < 0) return null;
		return base64ToBytes(dataUrl.slice(idx + marker.length));
	} catch (e) {
		return null;
	}
}

/**
 * 用官方通道读文件：桌面版 `Blockbench.read` 内部走 Node fs 同步读取，
 * 回调参数是 [{name, path, content}]；读失败时只弹框、不回调，所以这里加超时兜底。
 */
function readFilesViaBlockbench(paths, options, timeoutMs) {
	return new Promise(function (resolve, reject) {
		var settled = false;
		var timer = setTimeout(function () {
			if (settled) return;
			settled = true;
			reject(new Error('Blockbench.read timed out after ' + timeoutMs + 'ms (file missing or unreadable): ' + paths[0]));
		}, timeoutMs);
		function finish(files) {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			resolve(files || []);
		}
		if (typeof Blockbench === 'undefined' || typeof Blockbench.read !== 'function') {
			clearTimeout(timer);
			settled = true;
			reject(new Error('Blockbench.read is not available in this build'));
			return;
		}
		try {
			Blockbench.read(paths, options || {}, finish);
		} catch (e) {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			reject(e);
		}
	});
}

function activeProject() {
	return typeof Blockbench !== 'undefined' && Blockbench.Project || null;
}

function delay(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

async function waitProjectReady(path, model, oldIds, timeoutMs) {
	var deadline = Date.now() + timeoutMs;
	do {
		var p = activeProject(), summary = projectSummary();
		var count = (model.elements || []).filter(function (el) { return !el.type || el.type === 'cube'; }).length;
		var textures = typeof Texture !== 'undefined' && Texture.all || [];
		var imagesReady = textures.every(function (t) {
			return !t.img || (t.img.complete && t.img.naturalWidth > 0);
		});
		if (p && oldIds.indexOf(p.uuid) < 0 && samePath(summary.save_path, path) &&
			summary.elements === count && textures.length === (model.textures || []).length && imagesReady) {
			if (typeof Canvas !== 'undefined' && Canvas.updateAll) Canvas.updateAll();
			await delay(25); // allow the native viewport to consume the loaded geometry/textures
			if (activeProject() !== p) throw new Error('active project changed while rendering');
			return p;
		}
		await delay(25);
	} while (Date.now() < deadline);
	throw new Error('project/texture readiness timed out after ' + timeoutMs + 'ms');
}

async function openProject(path, params) {
	params = params || {};
	if (!path || typeof path !== 'string') return { ok: false, error: 'missing params.path' };
	var original = activeProject();
	var prior = typeof ModelProject !== 'undefined' && ModelProject.all ? ModelProject.all.slice() : [];
	var old = prior.filter(function (p) { return samePath(p.save_path, path); });
	if (old.length && params.force_close === false) return { ok: false, stale: true, error: 'same-path project already open; force_close=false' };
	if (!params.replace_unsaved && old.some(function (p) { return p.saved === false; })) {
		return { ok: false, error: 'same-path project has unsaved changes', hint: 'Save it first, or explicitly use replace_unsaved=true.' };
	}
	var loaded = null;
	try {
		// Validate the input first. Old tabs remain open until the new project is ready.
		var files = await readFilesViaBlockbench([path], { errorbox: false }, READ_TIMEOUT_MS);
		var file = files[0];
		if (!file || typeof file.content !== 'string') throw new Error('Blockbench.read returned no text');
		var model = JSON.parse(file.content.replace(/^\uFEFF/, ''));
		if (!model || !model.meta || !Array.isArray(model.elements || [])) throw new Error('invalid bbmodel structure');
		['groups', 'textures', 'outliner', 'animations'].forEach(function (key) {
			if (model[key] !== undefined && !Array.isArray(model[key])) throw new Error('invalid bbmodel ' + key);
		});
		var timeoutMs = Number(params.ready_timeout_ms === undefined ? 10000 : params.ready_timeout_ms);
		if (!isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 30000) throw new Error('invalid ready_timeout_ms');
		var stamp = model.mcp_sync || {};
		if (params.expect_source_token && stamp.source_token !== params.expect_source_token) throw new Error('file source_token mismatch');
		if (params.expect_project_id && stamp.project_id !== params.expect_project_id) throw new Error('file project_id mismatch');
		if (params.expect_revision !== undefined && stamp.revision !== params.expect_revision) throw new Error('file revision mismatch');
		if (typeof Formats !== 'undefined' && model.meta.model_format && !Formats[model.meta.model_format]) {
			throw new Error('model format unavailable: ' + model.meta.model_format);
		}
		if (typeof Codecs === 'undefined' || !Codecs.project || typeof Codecs.project.load !== 'function') {
			throw new Error('official project codec unavailable');
		}
		// The project codec creates a new tab, avoiding loadModelFile's same-path cache.
		await Codecs.project.load(model, file);
		var candidate = activeProject();
		if (candidate && prior.indexOf(candidate) < 0) loaded = candidate;
		loaded = await waitProjectReady(path, model, prior.map(function (p) { return p.uuid; }), timeoutMs);
		if (!params.replace_unsaved && old.some(function (p) { return p.saved === false; })) throw new Error('old project changed while loading');
		loaded.mcp_bridge_sync = stamp;
		// Some format plugins mark a freshly parsed document dirty without an edit.
		// Preserve that flag, but record content for strict read-only provenance.
		if (loaded.saved === false && (!loaded.undo || !loaded.undo.history.length)) {
			loaded.mcp_bridge_load_baseline = nativeDocumentContent();
		}
		var closed = [], warnings = [];
		for (var i = 0; i < old.length; i++) {
			try {
				var result = await old[i].close(true);
				if (result === false) warnings.push('old tab could not close: ' + old[i].name);
				else closed.push(old[i].name);
			} catch (e) { warnings.push('old tab cleanup failed: ' + describeError(e)); }
		}
		loaded.select();
		var summary = projectSummary();
		return Object.assign({ ok: true, stale: false, closed_projects: closed, warnings: warnings, result: 'opened: ' + path }, summary);
	} catch (e) {
		if (!loaded) {
			var current = activeProject();
			if (current && prior.indexOf(current) < 0 && samePath(current.save_path, path)) loaded = current;
		}
		if (loaded && prior.indexOf(loaded) < 0) {
			try { await loaded.close(true); } catch (ignore) { /* preserve original failure */ }
		}
		if (original && typeof original.select === 'function') {
			try { original.select(); } catch (ignore) { /* preserve the load error */ }
		}
		return { ok: false, stale: true, error: 'open failed: ' + describeError(e) };
	}
}

function captureGuard(params) {
	params = params || {};
	var viewport = projectSummary();
	viewport.blockbench_version = blockbenchVersion();
	if (params.expect_path && !samePath(viewport.save_path, params.expect_path)) return {ok: false, error: 'capture save_path mismatch'};
	if (params.expect_elements !== undefined && viewport.elements !== params.expect_elements) return {ok: false, error: 'capture elements mismatch'};
	if (params.expect_revision !== undefined && (viewport.revision !== params.expect_revision || viewport.project_id !== params.expect_project_id)) return {ok: false, error: 'capture revision/project_id mismatch'};
	if (params.expect_source_token && viewport.source_token !== params.expect_source_token) return {ok: false, error: 'capture source_token mismatch'};
	if (params.expect_revision !== undefined && viewport.saved === false) {
		var project = activeProject();
		if (!project.mcp_bridge_load_baseline || project.mcp_bridge_load_baseline !== nativeDocumentContent()) return {ok: false, error: 'capture project has unsaved native edits'};
		viewport.load_baseline_unchanged = true;
	}
	return {ok: true, viewport: viewport};
}

function nativeDocumentContent() {
	if (!Codecs.project || typeof Codecs.project.compile !== 'function') return null;
	// Exclude editor playhead/camera/selection; include every serialized asset.
	var value = Codecs.project.compile({raw: true, editor_state: false, bitmaps: true});
	return typeof value === 'string' ? value : JSON.stringify(value);
}

function captureCommand(params) {
	params = params || {};
	var guard = captureGuard(params);
	if (!guard.ok) return guard;
	if (params.animation !== undefined || params.camera !== undefined) return animatedOperation(params, true);
	return captureResult(guard.viewport);
}

function captureResult(viewport) {
	var png = capturePngBytes();
	if (!png) return {ok: false, error: 'native viewport capture failed'};
	var binary = '';
	for (var i = 0; i < png.length; i++) binary += String.fromCharCode(png[i]);
	return {ok: true, viewport: viewport, png_base64: btoa(binary)};
}

// Isolated Cuboid skeletal evaluation uses native BoneAnimator interpolation.
// Do not call animation.select(), getBoneAnimator(), or preview(): these can
// change selection/history, create tracks, or trigger sound/particle effects.
function sampleSpecs(params) {
	var specs = params.samples || [{animation: params.animation, time: params.time === undefined ? 0 : params.time}];
	if (!Array.isArray(specs) || !specs.length || specs.length > 128) throw new Error('samples must contain 1..128 poses');
	return specs.map(function (s) {
		if (!s || typeof s.animation !== 'string') throw new Error('animation name/UUID required');
		var matches = Animation.all.filter(function (a) { return a.uuid === s.animation || a.name === s.animation; });
		if (matches.length !== 1) throw new Error('animation missing or ambiguous: ' + s.animation);
		var a = matches[0], t = s.time === undefined ? 0 : s.time;
		if (typeof t !== 'number' || !isFinite(t) || t < 0 || t > a.length + 1e-7) throw new Error('time outside animation length: ' + s.animation);
		return {animation: a, time: Math.min(t, a.length)};
	});
}

function snapshotTransforms() {
	var items = [];
	Canvas.scene.traverse(function (o) {
		items.push({o: o, position: o.position.clone(), quaternion: o.quaternion.clone(), scale: o.scale.clone(), visible: o.visible,
			pre: o.pre_rotation && o.pre_rotation.clone()});
	});
	return function () {
		items.forEach(function (s) {
			s.o.position.copy(s.position); s.o.quaternion.copy(s.quaternion); s.o.scale.copy(s.scale); s.o.visible = s.visible;
			if (s.pre) s.o.pre_rotation.copy(s.pre); else delete s.o.pre_rotation;
		});
		Canvas.scene.updateMatrixWorld(true);
	};
}

function exportedCube(cube) {
	var node = cube;
	while (node && typeof node === 'object') {
		if (node.export === false) return false;
		node = node.parent;
	}
	return !!cube.mesh;
}

function poseBounds(params) {
	var all = new THREE.Box3(), contact = new THREE.Box3(), below = [], elements = [];
	var floor = params.floor === undefined ? 0 : params.floor;
	var tolerance = params.floor_tolerance === undefined ? 0.05 : params.floor_tolerance;
	var excluded = params.contact_exclude_prefixes || [];
	if (typeof floor !== 'number' || !isFinite(floor) || typeof tolerance !== 'number' || !isFinite(tolerance) || tolerance < 0) throw new Error('invalid floor/tolerance');
	if (!Array.isArray(excluded) || excluded.some(function (s) { return typeof s !== 'string'; })) throw new Error('contact_exclude_prefixes must be strings');
	Cube.all.filter(exportedCube).forEach(function (c) {
		// Blockbench clamps scale=0 to 1e-5. Ignore only collapsed geometry,
		// never drop an entire thin wing or a one-axis planar Cuboid.
		var scale = c.mesh.getWorldScale(new THREE.Vector3());
		if (Math.max(Math.abs(scale.x), Math.abs(scale.y), Math.abs(scale.z)) <= 0.00002) return;
		var b = new THREE.Box3().setFromObject(c.mesh);
		if (b.isEmpty()) return;
		if (!b.min.toArray().concat(b.max.toArray()).every(isFinite)) throw new Error('non-finite animated bounds: ' + c.name);
		all.union(b);
		if (!excluded.some(function (prefix) { return c.name.indexOf(prefix) === 0; })) {
			contact.union(b);
			if (b.min.y < floor - tolerance) below.push({uuid: c.uuid, name: c.name, minimum_y: b.min.y});
		}
		if (params.include_elements) elements.push({uuid: c.uuid, name: c.name, min: b.min.toArray(), max: b.max.toArray()});
	});
	var result = {bounds: all.isEmpty() ? null : {min: all.min.toArray(), max: all.max.toArray()},
		contact_bounds: contact.isEmpty() ? null : {min: contact.min.toArray(), max: contact.max.toArray()}, below_floor: below};
	if (params.include_elements) result.elements = elements;
	if (params.include_bones) result.bones = Group.all.filter(function (g) { return !!g.mesh; }).map(function (g) {
		return {uuid: g.uuid, name: g.name, position: g.mesh.position.toArray(),
			rotation: g.mesh.rotation.toArray().slice(0, 3).map(function (v) { return v * 180 / Math.PI; }),
			quaternion: g.mesh.quaternion.toArray(), scale: g.mesh.scale.toArray()};
	});
	return result;
}

function fitCamera(preview, preset, bounds) {
	var directions = {hero: [1, .7, -1.3], front: [0, 0, -1], rear: [0, 0, 1], side: [1, 0, 0], top: [0, 1, 0]};
	if (!directions[preset]) throw new Error('camera must be hero/front/rear/side/top');
	if (!bounds) throw new Error('no visible geometry to frame');
	if (!Array.isArray(bounds.min) || !Array.isArray(bounds.max) || bounds.min.length !== 3 || bounds.max.length !== 3 ||
		!bounds.min.concat(bounds.max).every(function (v) { return typeof v === 'number' && isFinite(v); }) ||
		bounds.min.some(function (v, i) { return v > bounds.max[i]; })) throw new Error('invalid fit_bounds');
	var b = new THREE.Box3(new THREE.Vector3().fromArray(bounds.min), new THREE.Vector3().fromArray(bounds.max));
	var center = b.getCenter(new THREE.Vector3()), size = b.getSize(new THREE.Vector3());
	preview.setProjectionMode(true);
	preview.setLockedAngle(null);
	var cam = preview.camera;
	cam.up.set(0, preset === 'top' ? 0 : 1, preset === 'top' ? 1 : 0);
	cam.position.copy(center).add(new THREE.Vector3().fromArray(directions[preset]).normalize().multiplyScalar(Math.max(size.length() * 2, 32)));
	preview.controls.target.copy(center);
	cam.zoom = 1; cam.lookAt(center); cam.updateProjectionMatrix(); cam.updateMatrixWorld(true);
	var extent = 0;
	[b.min.x, b.max.x].forEach(function (x) { [b.min.y, b.max.y].forEach(function (y) { [b.min.z, b.max.z].forEach(function (z) {
		var q = new THREE.Vector3(x, y, z).project(cam); extent = Math.max(extent, Math.abs(q.x), Math.abs(q.y));
	}); }); });
	cam.zoom = .88 / Math.max(extent, 1e-8); cam.updateProjectionMatrix(); preview.controls.update();
	return {preset: preset, projection: 'orthographic', position: cam.position.toArray(), target: center.toArray(), zoom: cam.zoom, bounds: bounds};
}

function animatedOperation(params, capture) {
	var guard = captureGuard(params);
	if (!guard.ok) return guard;
	var restore, cameraRestore, originalTime, originalSelected;
	try {
		if (typeof Timeline === 'undefined' || typeof Animator === 'undefined' || typeof Animation === 'undefined') throw new Error('animation API unavailable');
		if (Timeline.playing) throw new Error('pause native timeline before sampling');
		if (typeof NullObject !== 'undefined' && NullObject.all.some(function (n) { return n.ik_target; })) throw new Error('IK rigs are not supported by isolated Cuboid sampling');
		var specs = sampleSpecs(params);
		if (capture && specs.length !== 1) throw new Error('capture requires exactly one pose');
		restore = snapshotTransforms(); originalTime = Timeline.time; originalSelected = Animation.selected;
		var p = capture && Preview.selected;
		if (p && params.camera !== undefined) {
			var cameras = [p.camPers, p.camOrtho].map(function (cam) { return {cam: cam, position: cam.position.clone(), quaternion: cam.quaternion.clone(), up: cam.up.clone(), zoom: cam.zoom, layers: cam.layers.mask, axis: cam.axis, backgroundHandle: cam.backgroundHandle}; });
			var isOrtho = p.isOrtho, angle = p.angle, target = p.controls.target.clone(), sideTarget = p.side_view_target && p.side_view_target.clone();
			cameraRestore = function () {
				p.setProjectionMode(isOrtho);
				p.setLockedAngle(angle);
				cameras.forEach(function (s) { s.cam.position.copy(s.position); s.cam.quaternion.copy(s.quaternion); s.cam.up.copy(s.up); s.cam.zoom = s.zoom; s.cam.layers.mask = s.layers; s.cam.axis = s.axis; s.cam.backgroundHandle = s.backgroundHandle; s.cam.updateProjectionMatrix(); });
				p.controls.target.copy(target); if (sideTarget) p.side_view_target.copy(sideTarget); p.controls.update();
			};
		}
		var states = specs.map(function (s) {
			Timeline.time = s.time; Animation.selected = s.animation;
			Animator.showDefaultPose(true);
			Group.all.forEach(function (g) {
				Animator.resetLastValues();
				var a = s.animation.animators[g.uuid];
				if (a && typeof a.displayFrame === 'function') a.displayFrame(1);
			});
			Canvas.scene.updateMatrixWorld(true);
			return Object.assign({animation: s.animation.name, animation_uuid: s.animation.uuid, time: s.time}, poseBounds(params));
		});
		if (capture) {
			var state = states[0], camera = params.camera === undefined ? null : fitCamera(p, params.camera, params.fit_bounds || state.bounds);
			Canvas.scene.traverse(function (o) { if (o.name === 'grid_group') o.visible = false; });
			if (Canvas.side_grids) { Canvas.side_grids.x.visible = false; Canvas.side_grids.z.visible = false; }
			guard.viewport.pose = {animation: state.animation, time: state.time, camera: camera};
			return captureResult(guard.viewport);
		}
		return {ok: true, viewport: guard.viewport, samples: states, units: {position: 'model units (16/block)', rotation: 'degrees', scale: 'factor'},
			limitations: ['isolated Cuboid skeletal pose; controllers, IK, particles, sound and runtime Molang variables are not simulated']};
	} catch (e) {
		return {ok: false, error: 'animation operation failed: ' + describeError(e)};
	} finally {
		if (restore) {
			Timeline.time = originalTime; Animation.selected = originalSelected;
			if (cameraRestore) cameraRestore(); restore();
		}
	}
}

/** 撤销：window.Undo 是活动项目 UndoSystem 实例的 getter，没有项目时为 undefined。 */
function undoOnce() {
	var undo = null;
	try { if (typeof Undo !== 'undefined' && Undo) undo = Undo; } catch (e) { /* getter 可能抛错 */ }
	if (!undo) {
		try {
			if (typeof Blockbench !== 'undefined' && Blockbench.Project) undo = Blockbench.Project.undo;
		} catch (e) { /* optional */ }
	}
	if (!undo || typeof undo.undo !== 'function') {
		return { ok: false, error: 'no active undo system (open a project first: 项目未打开或撤销栈不可用)' };
	}
	undo.undo();
	return {
		ok: true,
		result: 'undo executed',
		index: typeof undo.index === 'number' ? undo.index : null,
		history: Array.isArray(undo.history) ? undo.history.length : null
	};
}

function evalCommand(plugin, params) {
	if (!plugin.allowEval) {
		return {
			ok: false,
			error: 'eval disabled; enable Setting "mcp_bridge_allow_eval" in Blockbench and reload the plugin.'
		};
	}
	var code = params && params.code;
	if (typeof code !== 'string' || !code.trim()) return { ok: false, error: 'missing params.code' };
	if (/\b(require|import)\s*\(|\b(fs|child_process|process)\b/.test(code)) {
		return { ok: false, error: 'code contains blocked identifiers (require/import/fs/child_process/process)' };
	}
	try {
		var value = (0, eval)(code);
		return { ok: true, result: value === undefined ? null : String(value) };
	} catch (e) {
		return { ok: false, error: 'eval failed: ' + String((e && e.message) || e) };
	}
}

/**
 * 让插件从磁盘重新加载自己：改完仓库那份 JS 后调它即可生效，
 * 避免"改了仓库副本、Blockbench 里跑的还是旧代码"。延迟一点点再 reload，
 * 好让 onunload 里的 server.close() 先完成，减少端口占用竞态。
 */
function reloadSelf() {
	var instance = null;
	try {
		if (typeof Plugins !== 'undefined' && Plugins && Plugins.registered) {
			instance = Plugins.registered['blockbench_mcp_bridge'];
		}
	} catch (e) { /* optional */ }
	if (!instance || typeof instance.reload !== 'function') {
		return { ok: false, error: 'plugin instance "blockbench_mcp_bridge" not found in Plugins.registered' };
	}
	var source_path = instance.path || '';
	setTimeout(function () {
		try {
			instance.reload();
			notify('reloaded from ' + source_path);
		} catch (e) {
			notify('self reload failed: ' + describeError(e));
		}
	}, 150);
	return { ok: true, result: 'reload scheduled', reloaded_from: source_path };
}

async function runCommand(plugin, method, params) {
	switch (method) {
		case 'health':
			return healthPayload(plugin);
		case 'probe':
			return probeBridge(plugin);
		case 'reload_self':
			return reloadSelf();
		case 'open':
		case 'reload':
			return await openProject(params && params.path, params);
		case 'undo':
			return undoOnce();
		case 'eval':
			return evalCommand(plugin, params);
		case 'capture':
			return captureCommand(params);
		case 'animation_sample':
			return animatedOperation(params || {}, false);
		default:
			return { ok: false, error: 'unknown command: ' + String(method) };
	}
}

function queueCommand(plugin, method, params) {
	var next = (plugin.commandQueue || Promise.resolve()).then(function () { return runCommand(plugin, method, params); });
	plugin.commandQueue = next.catch(function () {});
	return next;
}

// --- HTTP server ----------------------------------------------------------

function handleRequest(plugin, socket, req, body) {
	var url = (req.url || '').split('?')[0];
	if (req.method === 'GET' && url === '/health') {
		respondJson(socket, 200, healthPayload(plugin));
		return;
	}
	if (req.method === 'GET' && url === '/screenshot') {
		var png = capturePngBytes();
		if (!png) {
			respondJson(socket, 500, {
				ok: false,
				error: 'screenshot failed; is Blockbench in a modeling mode with a visible preview viewport?'
			});
			return;
		}
		respondBytes(socket, 200, 'image/png', png);
		return;
	}
	if (req.method === 'POST' && url === '/command') {
		var payload = null;
		try {
			payload = JSON.parse(decodeUtf8(body) || '{}');
		} catch (e) {
			respondJson(socket, 400, { ok: false, error: 'invalid JSON body' });
			return;
		}
		queueCommand(plugin, payload.method || '', payload.params || {})
			.then(function (result) { respondJson(socket, 200, result); })
			.catch(function (e) {
				respondJson(socket, 500, { ok: false, error: String((e && e.message) || e) });
			});
		return;
	}
	respondJson(socket, 404, { ok: false, error: 'not found: ' + req.method + ' ' + url });
}

function makeRequestHandler(plugin, socket) {
	var parts = [];
	var total = 0;
	var head = null;
	var need = 0;
	var bodyParts = [];
	var bodyLen = 0;
	var done = false;

	return function onData(chunk) {
		if (done) return;
		if (!head) {
			parts.push(chunk);
			total += chunk.length;
			var blob = concatBytes(parts, total);
			var marker = findHeaderEnd(blob);
			if (marker < 0) return;
			head = parseHead(blob, marker);
			need = head.contentLength;
			var bodyStart = marker + 4;
			var avail = blob.length - bodyStart;
			if (avail > 0) {
				var p = blob.slice(bodyStart, bodyStart + avail);
				bodyParts.push(p);
				bodyLen += p.length;
			}
			parts = [];
			total = 0;
		} else {
			bodyParts.push(chunk);
			bodyLen += chunk.length;
		}
		if (head && bodyLen >= need) {
			done = true;
			var body = concatBytes(bodyParts, bodyLen).slice(0, need);
			try {
				handleRequest(plugin, socket, head, body);
			} catch (e) {
				try { respondJson(socket, 500, { ok: false, error: String((e && e.message) || e) }); } catch (e2) {}
			}
		}
	};
}

function startServer(plugin) {
	var net = nativeModule('net');
	if (!net || typeof net.createServer !== 'function') {
		notify('net module unavailable; HTTP endpoints disabled. ' +
			'Blockbench 5 desktop with requireNativeModule is required.');
		return;
	}
	var attempts = 0;
	var server = net.createServer(function (socket) {
		socket.setTimeout(30000);
		socket.on('data', makeRequestHandler(plugin, socket));
		socket.on('timeout', function () { try { socket.destroy(); } catch (e) {} });
		socket.on('error', function () { /* connection errors are non-fatal */ });
	});
	server.on('error', function (e) {
		// 热重载时旧监听可能还没完全释放端口，重试几次再放弃。
		if (e && e.code === 'EADDRINUSE' && attempts < 5) {
			attempts++;
			setTimeout(function () {
				try { server.listen(plugin.port, '127.0.0.1'); } catch (e2) { /* 下次重试 */ }
			}, 200 * attempts);
			return;
		}
		notify('server error: ' + String((e && e.message) || e));
	});
	server.listen(plugin.port, '127.0.0.1', function () {
		plugin.server = server;
		attempts = 0;
		notify('listening on http://127.0.0.1:' + plugin.port);
	});
}

// --- plugin registration ---------------------------------------------------

function registerSettings() {
	try {
		if (typeof Setting === 'undefined') return;
		try {
			new Setting('mcp_bridge_port', {
				name: 'MCP Bridge Port',
				description: 'TCP port of the local HTTP bridge',
				category: 'General',
				type: 'number',
				min: 1024,
				max: 65535,
				default: BRIDGE_PORT
			});
		} catch (e) { /* already registered on reload */ }
		try {
			new Setting('mcp_bridge_allow_eval', {
				name: 'Allow eval commands',
				description: 'Enable /command method=eval (blocked identifiers still rejected)',
				category: 'General',
				type: 'switch',
				default: false
			});
		} catch (e) { /* already registered on reload */ }
	} catch (e) {
		notify('settings registration failed: ' + String((e && e.message) || e));
	}
}

function registerPlugin() {
	var registerFn = null;
	if (typeof Plugin !== 'undefined' && typeof Plugin.register === 'function') {
		registerFn = Plugin.register.bind(Plugin);
	} else if (typeof BBPlugin !== 'undefined' && typeof BBPlugin.register === 'function') {
		registerFn = BBPlugin.register.bind(BBPlugin);
	}
	if (!registerFn) {
		notify('no plugin registration API found');
		return;
	}
	registerFn('blockbench_mcp_bridge', {
		title: 'Blockbench MCP Bridge',
		author: 'blockbench-mcp',
		description: 'Local HTTP bridge for the blockbench-mcp Python MCP server (phase 2)',
		icon: 'icon-blockbench_file',
		version: BRIDGE_VERSION,
		variant: 'desktop',
		onload: function () {
			registerSettings();
			this.port = normalizePort(settingValue('mcp_bridge_port', BRIDGE_PORT));
			this.allowEval = Boolean(settingValue('mcp_bridge_allow_eval', false));
			startServer(this);
		},
		onunload: function () {
			if (this.server && typeof this.server.close === 'function') {
				try { this.server.close(); } catch (e) { /* ignore */ }
			}
			this.server = null;
		}
	});
}

registerPlugin();
