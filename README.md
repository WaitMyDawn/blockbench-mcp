# Blockbench MCP Server (Python)

让 Codex / Claude 等 MCP 客户端通过自然语言创建、编辑 **Minecraft 模型**。

- 新建 / 打开 / 保存 **Blockbench 项目**（.bbmodel 5.0）
- 建模：骨骼（组）、立方体、纹理贴面、逐面 UV
- 纹理：纯色 / 磁盘 PNG 导入导出 / 外部改图回灌 / **像素级绘制（Pillow）**
- 动画：创建动画、为骨骼添加 `rotation / position / scale` 关键帧
- 导出：**Bedrock**（.geo.json + .animation.json）、**Java Block/Item**、**OptiFine CEM**、**GeckoLib**
- 视觉反馈（双通道：文本路径 + 图片内容）：
  - 纯 Python 软渲染 `render_preview`（无需 Blockbench）
  - 纹理画布放大图 `render_texture`
  - 真实 Blockbench 视口截图 `blockbench_screenshot`（需二期插件桥）
  - ASCII 三视图 `render_ascii`（不支持看图的模型也能读形状）
- 内置模板：`sword_geckolib`（自研长剑，过程化金属纹理，无外部素材）

一期是**文档引擎**模式：所有编辑发生在内存文档模型上，保存的 `.bbmodel`
可直接用 Blockbench 打开继续编辑。二期接入 **Blockbench 插件桥**：真实视口截图、撤销、打开/重载项目。

## 项目结构

```text
blockbench-mcp/
├── main.py                  # MCP 入口（stdio）
├── blockbench_mcp/
│   ├── document.py          # 内存文档模型（与 bbmodel 5.0 语义一致）
│   ├── session.py           # 会话状态（当前项目）
│   ├── tools.py             # MCP 工具注册、编辑事务与原生同步
│   ├── paint.py             # 纹理像素绘制核心（Pillow：渐变/阴影/噪点/软笔刷）
│   ├── undo.py              # 像素级 + 文档级 undo/redo 双栈
│   ├── images.py            # PNG 解码/取色/编码（预览与回灌）
│   ├── errors.py / result.py
│   ├── preview.py           # 软渲染器 + 纹理画布图 + ASCII 三视图
│   ├── quality.py           # 无视觉质检（validate_quality）
│   ├── examples.py          # 自研模板 + examples/catalog.json 样例加载
│   ├── drivers/             # 二期插件桥 Python 客户端（health/screenshot/command/拉起 Blockbench）
│   └── codecs/
│       ├── bbmodel.py       # .bbmodel 5.0 读写
│       ├── bedrock.py       # Bedrock 几何体 + 动画
│       ├── java.py          # Java Block + OptiFine CEM
│       └── geckolib.py      # GeckoLib 资源布局导出
├── examples/                # 样例库：models/*.bbmodel + catalog.json（许可证登记）
├── plugin/                  # 二期 Blockbench 插件桥（JS 已实现，待真机联调）
├── scripts/                 # build_exe.ps1（PyInstaller 打包）
├── .github/workflows/       # release.yml（tag 自动出 Windows exe）
├── tests/                   # Python 回归与 JavaScript 异步桥接测试
├── docs/                    # 客户端接入 + 提示词示例（PROMPT_DOG.md）
└── agent_prompt.md          # 给 Codex 的建模/绘制/动画提示词指导
```

## 安装与运行

```powershell
cd d:\VScode\python\blockbench-mcp
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

依赖：`mcp[cli]` + `pillow`（纹理像素绘制必需）。

启动（stdio，供 Codex/Claude Desktop/VS Code 配置使用）：

```powershell
.\.venv\Scripts\python main.py
```

### VS Code / Claude Desktop 配置示例

```json
{
  "mcpServers": {
    "blockbench": {
      "command": "d:\\VScode\\python\\blockbench-mcp\\.venv\\Scripts\\python.exe",
      "args": ["d:\\VScode\\python\\blockbench-mcp\\main.py"],
      "type": "stdio"
    }
  }
}
```

## 工具一览

| 域 | 工具 |
|---|---|
| 项目 | `project_create` `project_open` `project_save` `project_sync` `project_status` `project_outline` `project_load_example` |
| 纹理元数据 | `texture_create` `texture_list` `texture_assign` `texture_update` `texture_delete` `texture_export` |
| 纹理绘制 | `texture_paint` `texture_paint_face` `texture_paint_cube` `texture_face_map` `texture_validate_uv` |
| 几何 | `cube_create` `cubes_create_bulk` `cube_update` `cube_delete` |
| 骨骼 | `bone_create` `bone_update` `bone_delete` |
| 动画 | `animation_create` `keyframe_add` `keyframes_add_bulk` `keyframe_list` `keyframe_remove` `animation_sample` `animation_review` `animation_preview` `animation_render` |
| 导出 | `export_model`（bbmodel / bedrock / java_block / cem / geckolib）|
| 预览/质检 | `render_preview` `render_texture` `render_ascii` `validate_quality` |
| Undo/Redo | `texture_undo` `texture_redo` `project_undo` `project_redo` `undo_status` `undo_clear` |
| 二期插件桥 | `blockbench_health` `blockbench_ensure_running` `blockbench_screenshot` `blockbench_command` |

所有返回都是结构化 JSON `{ok, tool, data, warnings}`；失败时错误消息自带修复建议。

### 可靠编辑与同步（桥接 0.5.0）

结构和纹理修改统一提交可撤销事务，失败时回滚。`cubes_create_bulk(atomic=True)` 和
`keyframes_add_bulk` 可将整批操作合并成一步；密集动画优先使用批量接口。

`project_sync(path=...)` 原子保存工程，等待原生工程和纹理就绪，再关闭同路径旧标签；
默认保护未保存修改。随后用 `blockbench_screenshot(expect_revision=...)` 检查版本，
截图像素与身份凭证由同一次桥接命令返回。普通同步与截图不依赖 eval。

严格数值校验、插值兼容性、撤销历史和端到端验证见 [可靠性改进说明](docs/RELIABILITY.md)。

### 纹理绘制操作（op 体系）

`texture_paint` / `texture_paint_face` / `texture_paint_cube` 都接受 `ops` 列表，每条 op：

- `kind`：`fill`（纯色）/ `linear_gradient`（线性渐变）/ `radial_gradient`（径向渐变）/
  `shadow`（柔边阴影）/ `multi_gradient`（多段渐变）/ `noise`（毛纹噪点）/ `soft_stamp`（径向软笔刷）
- `color` / `color1` / `stops`（多段色标）/ `angle`（渐变方向）/ `center`+`radius`（径向/笔刷）/
  `blur`+`offset`+`strength`（阴影）/ `amount`+`seed`（噪点）
- `blend`：`set` / `overlay` / `multiply` / `erase`
- `region`：`{rect:[x,y,w,h]}` 或 `{uv:[x1,y1,x2,y2]}`

一次 `texture_paint` 调用 = 一个 **undo 事务**（像素级 + 文档级快照均记录）。

## 测试

```powershell
.\.venv\Scripts\python -m pytest -q
```

覆盖：文档模型行为、bbmodel 往返、真实 5.0 文件解析、多种导出结构、软渲染/ASCII、
纹理导出-回灌、逐面 UV、GeckoLib 布局、质检与样例库、MCP 工具注册与全流程调用、
绘制原语（fill/渐变/shadow/多段渐变/noise/软笔刷）、undo/redo 双栈闭环、分辨率同步。
还覆盖编辑回滚、非有限数值、Step/Catmull-Rom 导出、旋转包围盒、同步与截图凭证。
安装 Node 时，pytest 自动运行 JavaScript 异步桥接测试。

## 样例库（examples/）

样例以**完整 .bbmodel 文件**存放在 `examples/models/`，许可证与来源登记在
`examples/catalog.json`——不是硬编码在代码里。规则：无许可证声明 = ARR，不收录；
只收 MIT/CC0/官方授权。当前样例：

| id | 内容 | 许可证 |
|---|---|---|
| redeemer | 你的 GeckoLib 步枪成品（234 立方体、3 骨骼，GeckoLib 道具基准）| ARR（作者自持，发布前请决定许可）|
| polar_bear | bmaMC 极地熊（Bedrock 实体 + 动画）| MIT |
| sword_geckolib | 自研长剑模板（代码生成，无外部素材）| 随项目许可 |

加自己的样例：把 `.bbmodel` 放进 `examples/models/`，在 `catalog.json` 加一条，
即可用 `project_load_example(template="xxx")` 加载。

## 视觉反馈与二期插件桥

两条视觉通道按客户端能力选用：

1. **纯 Python（无需开 Blockbench）**：`render_preview`（软渲染 PNG）与
   `render_texture`（纹理画布图）直接把图片内容回传给支持看图的 MCP 客户端；
   文本客户端用 `render_ascii` 读形状。
2. **二期插件桥（真实材质/光照/视口）**：Blockbench 打开 → `File > Plugins >
   Load from File` 选择 `plugin/blockbench_mcp_bridge.js` → Python 侧即可调用
   `blockbench_health` / `blockbench_screenshot` / `blockbench_command(probe|open|undo|eval)`。
   Blockbench 没开时先调 `blockbench_ensure_running`：它探一次 `/health`，不通才拉起
   Blockbench（清掉 `ELECTRON_RUN_AS_NODE`，否则 Electron 会把 Blockbench 当 node 跑），
   再轮询到插件桥就绪。

纹理精修闭环：`texture_create` 或 `texture_export` 得到 PNG → 交给图像模型/人工细化 →
`texture_update(source_path=...)` 回灌 → `render_texture`/`render_preview` 验收 →
满意后 `export_model`。建议用支持多模态图片的模型（如视觉版 DeepSeek）跑这条闭环。

## 已知边界

- 一期文档引擎不做实时编辑器控制；实时截图/撤销/UI 走二期桥，要求 Blockbench
  保持打开并已加载插件 JS。
- 二期插件桥已在 **Blockbench 5.1.6 桌面版真机联调通过**（health / probe / open /
  screenshot / undo）。实测要点：`requireNativeModule` 只在插件作用域可见；5.1.6 没有
  Codecs/Filesystem/Project 原生模块，打开 `.bbmodel` 走官方全局链路
  新版同步使用 `Blockbench.read -> Codecs.project.load` 绕过同路径缓存，并等待纹理就绪；
  撤销是 `window.Undo.undo()`（活动项目的 UndoSystem）。0.5.0 同步/版本截图/未保存修改保护
  另在 Blockbench 5.2.1 经真实 stdio MCP 验证通过。
  仓库副本与 `%APPDATA%\Blockbench\plugins` 副本用 `scripts/sync_bridge_plugin.py` 保持同步。
- `texture_update` 放大画布时会**自动同步项目级 `resolution`**，避免 Blockbench 打开时
  仍按旧尺寸显示，无需手动把 64x64 改成 256x256。
- 自动 UV 采用文档化展开布局；精确贴图请用 `faces` 显式指定，或在 Blockbench UV 编辑器微调。
- bezier 关键帧导出时按线性近似（BB 会烘焙曲线）。
- Java Block 多轴旋转按单轴导出并给出警告（经典模型格式限制）。
- Java 实体动画无原生 JSON 标准：动画保留在 `.bbmodel` / Bedrock 动画文件中。
- 图像工具与几何都能落地，但**游戏级美术观感不是一次会话能交付的**：纹理精修与
  造型迭代需要多轮“渲染→看图→修改”，并由人在 Blockbench 最终验收。
- 素材合规：内置模板全部为自研；如需内置第三方样例，仅选用 MIT/CC0 且保留署名。

## GeckoLib（Java 模组）快速开始

```text
project_load_example(template="sword_geckolib")   # 载入内置长剑模板
project_status()                                   # 查看部件
render_ascii(cells=36)                             # 无视觉模型也能读形状
render_preview(path="C:/tmp/sword.png")            # 视觉客户端看图
export_model(target="geckolib", path="C:/mod_resource",
             modid="mymod", category="item", model_name="sword")
```

输出资源布局：

```text
assets/mymod/geo/item/sword.geo.json
assets/mymod/animations/item/sword.animation.json
assets/mymod/textures/item/sword_tex.png
```

模组代码侧用 `GeoItem`/`GeoEntity` 指向 `geo/item/sword.geo.json`
与 `animations/item/sword.animation.json` 即可。

## 命令行（不需要 MCP 客户端）

```powershell
.\.venv\Scripts\python.exe -m blockbench_mcp.cli open "d:/.../Redeemer.bbmodel" --ascii
.\.venv\Scripts\python.exe -m blockbench_mcp.cli quality
.\.venv\Scripts\python.exe -m blockbench_mcp.cli render --out preview.png
.\.venv\Scripts\python.exe -m blockbench_mcp.cli export --target geckolib --out .\pack --modid mymod
```

## 接入 VS Code / Codex（快速版）

- 把 `d:\VScode\python\blockbench-mcp` 作为工作区打开，项目已内置
  [.vscode/mcp.json](.vscode/mcp.json)（stdio 指向 .venv）；重载窗口即可。
- 外层工作区 / Codex CLI / Claude Desktop 的配置写法见
  [docs/SETUP_CLIENTS.md](docs/SETUP_CLIENTS.md)。
- 验证：`.\.venv\Scripts\python.exe scripts\smoke_stdio.py`

## 发布与使用（GitHub）

### 有 Python 环境（开发者）

```powershell
git clone <你的仓库>
cd blockbench-mcp
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

然后在 MCP 客户端配置 stdio：

```json
{
  "mcpServers": {
    "blockbench": {
      "command": "C:\\path\\to\\blockbench-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\blockbench-mcp\\main.py"]
    }
  }
}
```

### 无编译/无 Python 环境（普通用户）

发布 tag（如 `v0.2.0`）后 GitHub Actions 自动产出
`blockbench-mcp.exe`（PyInstaller 单文件，已内置 Python 与依赖）。用户只需：

1. 从 Releases 下载 `blockbench-mcp.exe` 放到任意目录；
2. 在 Claude Desktop / Cline / VS Code 的 MCP 配置里指向该 exe：
```json
{
  "mcpServers": {
    "blockbench": {
      "command": "C:\\tools\\blockbench-mcp.exe"
    }
  }
}
```

本地打包：

```powershell
.\.venv\Scripts\python -m pip install -e ".[build]"
powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
```

---

## 提示词指导（重要）

如何用自然语言描述建模 / 纹理 / 动画，才能得到更好效果？完整指南见
[agent_prompt.md](agent_prompt.md)。一句话原则：**先说目标与风格 → 再让模型先查询再动手 →
纹理要“分部位、有明暗、有材质颗粒”→ 动画要说明轴向、幅度、时长与循环方式 → 老实区分
“结构自检”与“真实渲染验收”，不夸大成品**。
