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

## 当前状态（二期 JS 已实现，待真机联调）

- Python 侧客户端（健康检查/截图/命令）已实现且为标准库，可直接调用
  `capture_blockbench_screenshot(out_path)` / `RemoteDriver().command(...)`。
- `blockbench_mcp_bridge.js` 已在桌面端注册插件与端口设置，并用
  `requireNativeModule('net')` 起最小 HTTP/1.1 服务：
  - `GET /health`：返回版本、当前项目与可用命令；
  - `GET /screenshot`：对当前预览视口截图（先 render 再去 Gizmo，返回 PNG）；
  - `POST /command`：`health / open / reload / undo / eval`。
- 诚实降级：`open` 若拿不到 Codecs/Filesystem 打开链路会返回明确错误
  （提示在 Blockbench 手动打开 .bbmodel），不会伪造“已打开”。
- 联调前必须实测的 API 面：`requireNativeModule('net')` 是否可用、截图 Canvas
  是否走 `Preview.selected`、`Setting` 取值与 `Undo.history.undo()` 的真实形态。

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

> 截图依赖建模模式里有可见预览视口；若截图为空，先在 Blockbench 打开任意项目。
