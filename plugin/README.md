# Blockbench 插件桥（二期）

让 Python MCP Server 在“真实 Blockbench 视口”里截图/执行操作，形成 Agent 看得见
真实渲染结果的闭环（弥补纯 Python 软渲染的画质上限）。

## 架构

```text
Codex/客户端 ──MCP(stdin)──> Python MCP Server
                              │  RemoteDriver (blockbench_mcp/drivers/remote.py)
                              ▼
                        Blockbench 插件桥（本目录 JS，端口 18765）
                              │
                              ▼
                        Blockbench 真实视口/项目
```

## 当前状态（二期已真机联调：Blockbench 5.1.6 桌面版）

- Python 侧客户端（健康检查/截图/命令）已实现且为标准库，可直接调用
  `capture_blockbench_screenshot(out_path)` / `RemoteDriver().command(...)`；
  另外 `RemoteDriver().ensure_running()`（MCP 工具 `blockbench_ensure_running`）
  会先探 `/health`，不通才拉起 Blockbench 并轮询就绪。
- `blockbench_mcp_bridge.js` 已在桌面端注册插件与端口设置，并用
  `requireNativeModule('net')` 起最小 HTTP/1.1 服务：
  - `GET /health`：返回版本、桥版本、当前项目与可用命令；
  - `GET /screenshot`：对当前预览视口截图（先 render 再去 Gizmo，返回 PNG）；
  - `POST /command`：`health / probe / open / reload / reload_self / undo / eval / capture / animation_sample`。
- `probe` 是**只读** API 体检，报告真机上真正可用的打开/截图/撤销链路，
  排障时优先调它（`blockbench_command("probe")`）。
- 真机实测结论（Blockbench 5.1.6）：

| API | 事实 | 影响 |
|---|---|---|
| `requireNativeModule` | 只存在于**插件作用域**（插件由 `new Function("requireNativeModule","require", code)` 装载），全局 `eval` 里 `typeof` 是 `undefined` | 探测原生模块能力必须写在插件里；`eval` 通道探不到 |
| `Codecs` / `Filesystem` / `Project` 原生模块 | 5.1.6 **不存在**（旧版 `open` 因此永远走降级分支） | 打开文件改用官方全局链路 |
| `Blockbench.read` + `loadModelFile` | 都在全局；桌面版内部读取文件，`loadModelFile` 会命中同路径缓存 | 新版同步绕过该缓存，直接调用 project codec |
| `Codecs.project.load_filter` | `{type: "json", extensions: ["bbmodel"]}`，`load(model, file)` 需要**已解析的 JSON** | 0.5.0 先验证文件，再加载新标签；新工程与纹理就绪后 await 关闭旧标签 |
| `window.Undo` | getter，返回 `Blockbench.Project?.undo`（活动项目的 `UndoSystem`），无项目时为 `undefined` | 撤销调 `Undo.undo()`，不是 `Undo.history.undo()` |
| 截图 | `Preview.selected` 存在，实例有 `canvas` / `render()`，`Canvas.withoutGizmos` 可用 | 原截图实现正确，未改 |

0.5.0 经 Blockbench 5.2.1 真实 stdio MCP 联调：同路径同数量重载、版本截图、未保存修改保护与撤销后同步通过。
`capture` 一次返回 PNG 与原生 UUID、会话身份、revision、source_token；命令串行处理。

0.6.0 增加正式 `animation_sample(samples=[{animation,time}], include_bones=true)` 与 `capture(animation,time,camera,fit_bounds)`，无需 eval。camera 支持 hero/front/rear/side/top；操作结束后恢复姿态、时间、动画选择与相机。只采样已有 Cuboid 骨骼轨道，不触发控制器/粒子/音效/IK 或补建空轨道。`expect_*` 参数沿用版本保护；MCP 工具会自动传入当前同步凭证。

完整使用方式和限制见 [原生动画验收](../docs/RELIABILITY.md)。桥接版本可由 `/health` 查看，客户端需要重连 MCP 才能获取新工具。
默认拒绝替换未保存标签，只有显式 `replace_unsaved=true` 才允许。详见 [可靠性说明](../docs/RELIABILITY.md)。

## 双副本同步

Blockbench 只加载它自己记录的那一份（`Plugins.registered['blockbench_mcp_bridge'].path`，
可能是仓库副本，也可能是 `%APPDATA%` 副本）。改完仓库那份务必同步 + 热重载：

```powershell
.\.venv\Scripts\python.exe scripts\sync_bridge_plugin.py --check     # 只查一致性
.\.venv\Scripts\python.exe scripts\sync_bridge_plugin.py --reload    # 仓库 -> APPDATA 并热重载
.\.venv\Scripts\python.exe scripts\sync_bridge_plugin.py --direction pull
```

`--reload` 走插件的 `reload_self` 命令；若运行中的插件是旧版本（不认识该命令），
脚本会提示在 Blockbench 里手动 reload 一次，之后就都能热重载了。

## 端点/设置

- 默认端口 `18765`（插件加载后在 Blockbench 设置面板可改 `mcp_bridge_port`）。
- `mcp_bridge_allow_eval`（默认关）：打开后才允许 `method="eval"`，
  且代码含 `require/import/fs/child_process/process` 会被拒绝。

## 联调步骤

1. 打开 Blockbench（桌面版，Blockbench 5）。
2. `File > Plugins > Load from File`，选择本目录 `blockbench_mcp_bridge.js`。
3. 看右上角消息“listening on http://127.0.0.1:18765”。
4. 浏览器或 PowerShell 验证：`Invoke-RestMethod http://127.0.0.1:18765/health`。
5. Python 侧调用 `blockbench_health` / `blockbench_screenshot` 验收。
6. 排障：`blockbench_command("probe")` 看真机 API 体检报告；
   打开文件用 `blockbench_command("open", {"path": ...})`（成功会回项目名与 save_path）。

> 截图依赖建模模式里有可见预览视口；若截图为空，先在 Blockbench 打开任意项目。
