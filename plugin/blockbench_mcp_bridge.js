/**
 * Blockbench MCP Bridge - Phase 2 (desktop variant)
 *
 * Protocol (aligned with blockbench_mcp/drivers/remote.py):
 *   GET  /health       -> {"ok": true, "blockbench_version": "...", ...}
 *   POST /command      -> body {"method": "open"|"reload"|"undo"|"eval"|"health", "params": {...}}
 *   GET  /screenshot   -> image/png bytes of the current viewport
 *
 * Notes on the implementation:
 *  - Blockbench 5 desktop does not expose the Node.js `http` module, so this
 *    plugin runs a minimal HTTP/1.1 server over raw TCP obtained from
 *    requireNativeModule("net", ...). See jasonjgardner/blockbench-mcp-plugin
 *    for the same technique.
 *  - `open` tries the Codecs/Filesystem route and returns a clear error that
 *    asks to open the .bbmodel manually when the sandbox API differs, instead
 *    of pretending the file was loaded.
 *  - `eval` is disabled unless the user enables setting mcp_bridge_allow_eval.
 */

var BRIDGE_PORT = 18765;

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

function healthPayload(plugin) {
	var version = 'unknown';
	try { if (typeof Blockbench !== 'undefined' && Blockbench.version) version = Blockbench.version; } catch (e) {}
	var projectName = '';
	var format = '';
	try {
		if (typeof Project !== 'undefined' && Project) {
			projectName = Project.name || '';
			format = Project.format || '';
		}
	} catch (e) { /* optional info */ }
	return {
		ok: true,
		blockbench_version: version,
		project: projectName,
		format: format,
		phase: 'bridge-js',
		methods: ['health', 'open', 'reload', 'undo', 'eval', 'screenshot']
	};
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

async function openProject(path) {
	if (!path || typeof path !== 'string') return { ok: false, error: 'missing params.path' };
	var fs = nativeModule('Filesystem');
	var codecs = nativeModule('Codecs');
	var ProjectM = nativeModule('Project');
	var usable = fs && typeof fs.read === 'function' &&
		codecs && codecs.project && typeof codecs.project.read === 'function' &&
		ProjectM && typeof ProjectM.open === 'function';
	if (!usable) {
		return {
			ok: false,
			error: 'desktop file-open API is not reachable from this sandbox; ' +
				'open the .bbmodel manually in Blockbench (File > Open), then call blockbench_screenshot.'
		};
	}
	try {
		var data = await fs.read(path);
		var project = codecs.project.read(data, path);
		ProjectM.open(project);
		return { ok: true, result: 'opened: ' + path };
	} catch (e) {
		return {
			ok: false,
			error: 'open failed (' + String((e && e.message) || e) + '); ' +
				'fallback: open the .bbmodel manually in Blockbench, then call blockbench_screenshot.'
		};
	}
}

function undoOnce() {
	var undo = nativeModule('Undo');
	if (undo && undo.history && typeof undo.history.undo === 'function') {
		undo.history.undo();
		return { ok: true, result: 'undo executed' };
	}
	return { ok: false, error: 'Undo.history.undo is not available' };
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

async function runCommand(plugin, method, params) {
	switch (method) {
		case 'health':
			return healthPayload(plugin);
		case 'open':
		case 'reload':
			return await openProject(params && params.path);
		case 'undo':
			return undoOnce();
		case 'eval':
			return evalCommand(plugin, params);
		default:
			return { ok: false, error: 'unknown command: ' + String(method) };
	}
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
		runCommand(plugin, payload.method || '', payload.params || {})
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
	var server = net.createServer(function (socket) {
		socket.setTimeout(30000);
		socket.on('data', makeRequestHandler(plugin, socket));
		socket.on('timeout', function () { try { socket.destroy(); } catch (e) {} });
		socket.on('error', function () { /* connection errors are non-fatal */ });
	});
	server.on('error', function (e) {
		notify('server error: ' + String((e && e.message) || e));
	});
	server.listen(plugin.port, '127.0.0.1', function () {
		plugin.server = server;
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
		version: '0.2.0',
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
