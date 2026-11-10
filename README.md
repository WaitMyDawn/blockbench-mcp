# Blockbench MCP

在支持 MCP 的客户端里用自然语言创建和编辑 Minecraft 模型。可以调整方块和骨骼、绘制贴图、制作关键帧动画，并把工程保存为 `.bbmodel` 或导出成游戏资源。

常规编辑和预览可以直接在 MCP 会话里完成。加载桌面版 Blockbench 插件桥后，还可以同步工程、截取视口、检查动画，以及在 UV 面板选定重绘范围。

## 安装与连接

下面以 Windows PowerShell 为例。在仓库目录中使用 Python 3.11 或更新版本创建环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

### Codex CLI

在仓库目录中注册本地 stdio 服务。

```powershell
$projectRoot = (Get-Location).Path
codex mcp add blockbench -- "$projectRoot\.venv\Scripts\python.exe" "$projectRoot\main.py"
```

重新打开会话后，在 `/mcp` 中查看 `blockbench` 和它的工具列表。

### Claude Desktop、Cline 和 VS Code

在使用 `mcpServers` 配置的客户端中加入下列内容，把路径换成本地仓库位置。

```json
{
  "mcpServers": {
    "blockbench": {
      "command": "C:\\projects\\blockbench-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\projects\\blockbench-mcp\\main.py"]
    }
  }
}
```

使用 VS Code 时，修改 [`.vscode/mcp.json`](.vscode/mcp.json) 中的路径，然后重新加载窗口。其他接入方式见[客户端配置](docs/SETUP_CLIENTS.md)。

## 打开、预览和保存

连接后可以先试试内置长剑。

> 加载 `sword_geckolib` 样例，给我看一张预览，然后保存为 `C:/models/sword.bbmodel`。

Agent 会使用 `project_load_example` 加载样例，用 `render_preview` 返回图片，再用 `project_save` 写入工程。也可以让它用 `project_open` 打开已有的 `.bbmodel`，或者用 `project_create` 从头创建。

一个 stdio 服务进程维护一个当前工程。编辑会先进入内存，`project_save` 将工程写入磁盘。独立会话编辑多个工程时，分别指定各自的保存路径。

样例可通过 `project_load_example` 按 ID 加载，例如 `polar_bear` 和 `bettermodel_demon_knight`。模型来源和各自的许可见[样例目录](examples/README.md)与[样例清单](examples/catalog.json)。

## 连接桌面版 Blockbench

1. 打开 Blockbench 5 桌面版，从 **File → Plugins → Load from File** 加载 [插件桥](plugin/blockbench_mcp_bridge.js)。仓库附带的桥接版本为 **0.10.0**。
2. 调用 `blockbench_health` 检查连接。也可以在 PowerShell 中运行 `Invoke-RestMethod http://127.0.0.1:18765/health`。
3. 用 `project_sync(path=...)` 保存并在 Blockbench 中打开当前 MCP 工程。完成后可以用 `blockbench_screenshot` 查看真实视口。

`project_sync` 会重新加载指定路径的工程，默认保护 Blockbench 中同路径的未保存修改。需要保留原标签页时，可以把工作版本同步到另一条 `.bbmodel` 路径。更新插件后，在 Blockbench 中重新加载插件，并让 MCP 客户端重连。加载和更新方式见[插件桥说明](plugin/README.md)。

### 只重绘选中的面

1. 在 Blockbench 打开工程，选中要修改的方块。在左侧 UV 面板选择面，按 **Ctrl** 或 **Shift** 可以多选，再点 **锁定选面**。使用盒式 UV 时，可以通过 **工具 → 设为 MCP 重绘面** 勾选具体面。
2. 确认 UV 图上的绿色标记，然后告诉 Agent 要怎么改，例如“只把锁定的两面改成蓝紫色，先给我看对比图”。`scope_status` 会读取锁定范围和工程快照，`scope_repaint` 会生成预览。手动锁定面时可以直接从活动标签页读取工程。
3. 看过预览后用 `scope_commit` 提交。可以用 `project_undo` 一次撤销整批修改。用 `texture_export` 导出 PNG，或用 `project_sync(path=...)` 将这一版加载到桌面。

一次重绘可以覆盖多个互不相连的 UV 区域。可直接按面绘制，也可导入同尺寸的候选 PNG。选区外的像素，以及被未选面共用的像素，都会受到保护。选面重绘使用静态内嵌纹理和已合并的图层。

### 只重建一部分模型

1. 先用 `project_sync` 同步工程。在编辑模式选中目标方块，点 **工具 → 设为 MCP 修改范围**。也可以用 **框定 MCP 修改范围** 输入 XYZ 边界。完整位于框内的方块会进入范围。
2. 描述要调整的部件。`scope_preview` 会预览方块的增删改，并检查修改目标、父骨骼和空间边界。
3. 用 `scope_commit` 提交整批操作，或用 `scope_cancel` 放弃。提交后可以用 `project_undo` 一次回退，再同步到桌面。

Agent 也可以用 `scope_propose` 或 `scope_propose_faces` 提议范围，再由你在 Blockbench 中确认。固定高亮与范围框属于视口标记，和保存、导出分开。

## 预览、动画与导出

| 想做什么 | 使用的工具 |
| --- | --- |
| 看轮廓、贴图和结构问题 | `render_preview`、`render_texture`、`render_ascii`、`validate_quality` |
| 绘制或替换贴图 | `texture_paint`、`texture_paint_face`、`texture_paint_cube`、`texture_update` |
| 检查面与纹理的对应关系 | `texture_face_map`、`texture_validate_uv` |
| 创建骨骼动画 | `animation_create`、`keyframe_add`、`keyframes_add_bulk` |
| 看指定姿态、检查衔接或生成 GIF | `animation_preview`、`animation_review`、`animation_render`，先同步到 Blockbench |
| 保存模型资源 | `export_model` |

`export_model` 支持以下目标。

| `target` | 文件 |
| --- | --- |
| `bbmodel` | Blockbench 工程 `.bbmodel` |
| `bedrock` | 基岩版 `.geo.json` 和工程中的动画 `.animation.json` |
| `java_block` | Java 版方块或物品模型 JSON |
| `cem` | OptiFine 自定义实体模型 `.jem` |
| `geckolib` | 按模组资源目录组织的几何、动画和纹理文件 |

例如，把内置长剑导出为 `examplemod` 的 GeckoLib 物品资源。

```text
project_load_example(template="sword_geckolib")
export_model(target="geckolib", path="C:/models/pack", modid="examplemod", category="item", model_name="sword")
```

文件分别位于 `assets/examplemod/geo/item/`、`assets/examplemod/animations/item/` 和 `assets/examplemod/textures/item/`。使用 Bézier 关键帧的工程需要在 Blockbench 中烘焙动画，再导出为 Bedrock 或 GeckoLib。动态可见范围、动画采样和撤销行为见[编辑与动画说明](docs/RELIABILITY.md)。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| MCP 工具列表为空 | 检查 Python 和 `main.py` 的路径，重新连接客户端。可运行 `.\.venv\Scripts\python.exe scripts\smoke_stdio.py` 检查握手。 |
| Blockbench 连接失败或看不到「锁定选面」 | 打开桌面版 Blockbench，加载仓库中的插件桥，调用 `blockbench_health` 核对版本。 |
| 同步提示同路径有未保存修改 | 先保存 Blockbench 中的修改，或同步到另一个 `.bbmodel` 路径。 |
| 选区失效或工程版本改变 | 用 `scope_cancel` 放弃旧草稿，在活动工程中重新锁定范围，再生成预览。 |
| 无法打开压缩备份 | 在 Blockbench 中另存为普通 `.bbmodel` 后再打开。 |

仅需查看工程结构和质检结果时，也可以直接运行命令行。

```powershell
.\.venv\Scripts\python.exe -m blockbench_mcp.cli inspect .\examples\models\polar_bear.bbmodel --ascii
```

项目采用 [MIT 许可证](LICENSE)。第三方样例的署名与许可见 [Third-Party Notices](THIRD_PARTY_NOTICES.md)。
