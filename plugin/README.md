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

## 局部重建（0.7.0）

局部重建针对同步到 Blockbench 的 Cuboid 工程。导入现有工程时可以先同步到独立的工作副本。
编辑模式的「工具」菜单提供四个操作。

- **设为 MCP 修改范围** 固定当前所选方块的 UUID，青色轮廓与临时选择独立。
- **确认 MCP 提议范围** 将 Agent 提议的橙色高亮范围固定下来。
- **框定 MCP 修改范围** 以当前选中方块的包围盒为起点。输入最小和最大 XYZ 坐标时，透明框实时更新。完整落入框内的方块进入范围，红色轮廓表示与框相交但未被完整包含。
- **清除 MCP 修改范围** 移除固定范围及其视口标记。Agent 可以调用 `scope_cancel` 一并丢弃服务端草稿。

标记是插件管理的视口 gizmo，位于模型层级之外。切换工程或模式时按需隐藏，清除范围和卸载插件时释放资源。
`scope_get`、`scope_propose`、`scope_clear` 通过桥接命令队列处理，范围确认来自插件 UI。
预览和提交重新验证活动工程的同步凭证以及原生文档内容。

`scope_preview(operations=[...])` 每次从同一版本重新生成草稿。更新和删除使用方块 UUID，
新增方块使用 `scope_status` 返回的父骨骼 UUID 或允许的 `null` 根节点，并受世界坐标边界约束。
首版开放方块名称、坐标、旋转和原点。骨骼属性、动画及共享贴图由原工程保留。
`scope_commit(draft_id=...)` 提交指定草稿到内存文档，`project_undo` 一次回退整批，
`project_sync` 将提交或撤销后的版本加载到 Blockbench。

## 选面重绘（0.8.0）

同步工程后，在编辑或绘制模式选中一个或多个方块。通过「工具 → 设为 MCP 重绘面」
勾选具体的面。Agent 可以用 `scope_propose_faces(faces=[{uuid, face}])` 提议同一种范围，
待用户在 Blockbench 点击「确认 MCP 提议范围」后生效。固定轮廓只围住选中的面。

`scope_status` 返回纹理 UUID、UV 对应的像素矩形及可编辑像素数量。
`scope_repaint(operations=[...])` 支持两类操作。

- `paint` 使用 `uuid`、`face` 和 `ops`，绘制指令沿用逐面绘制工具，区域由选中的面决定。
- `replace_png` 使用纹理 UUID `texture` 和 `source_path`，比较同尺寸候选 PNG 与原贴图。

插件提供实际渲染的 UV 和画布单位。每张纹理独立计算选中像素的并集，再扣除未选面共享的像素。
镜像 UV 按实际像素区间计算，部分覆盖的像素留给原图。候选图改动受保护的像素会使整批预览失败。
选面重绘使用静态内嵌纹理和已合并的图层。验收对比图后使用 `scope_commit`，
整批贴图修改可由 `project_undo` 一步回退。

### 从 Blockbench 自动读取选面工程（0.9.0）

直接在 Blockbench 打开立方体工程，选中方块并用「设为 MCP 重绘面」确认具体面。
Agent 首次调用 `scope_status` 或 `scope_repaint` 时会读取确认时的内存快照。
自动读取保留当前标签页和磁盘文件。若原生工程随后发生改动，需要重新确认选面。

MCP 会话中已有未保存的修改时，先保存当前工作或使用新的会话再读取。自动读取后的草稿
通过 `scope_commit` 提交至 MCP 内存。要导出可用 `texture_export`，要在桌面查看可用
`project_sync(path=...)` 指定保存目标。为保留原始工程，首次同步可指定一个新的 `.bbmodel` 路径。

使用旧版桥接时，在同一 MCP 会话先调用 `project_open(path=...)` 和 `project_sync()`，
再回 Blockbench 设定重绘面。

### 从 UV 面板锁定重绘面（0.10.0）

在左侧 UV 面板选面，可以按 Ctrl 或 Shift 多选，再点面板顶部的「锁定选面」。
面板顶部会显示锁定数量，悬停可查看方块和面名。显示对应纹理时，锁定区域带有亮绿色边框和方向标记，
3D 视口中的对应面也有半透明填色。锁定范围独立于后续的普通选择。
盒式 UV 的逐面选取仍可使用「工具 → 设为 MCP 重绘面」。

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
