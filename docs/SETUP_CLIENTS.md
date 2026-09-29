# 把 Blockbench MCP 接入你的客户端

服务端已经具备：stdio 入口（`main.py`）、虚拟环境（`.venv`）与动态注册的 MCP 工具。
接入 = 告诉你的客户端“用哪个命令启动它”。下面是各客户端的具体做法。

## 0. 前置检查（只做一次）

```powershell
cd d:\VScode\python\blockbench-mcp
.\.venv\Scripts\python.exe -m pytest -q          # Python 与可选 Node 桥接回归
.\.venv\Scripts\python.exe scripts\smoke_stdio.py # 打印工具数量即握手成功
```

如果没装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install "mcp[cli]>=1.2,<2"
```

## 1. VS Code（把项目文件夹作为工作区打开）

项目里已附 .vscode/mcp.json（指向你的绝对路径）。
重新加载窗口（Ctrl+Shift+P → Developer: Reload Window）后：

- VS Code 1.99+ 原生 MCP：命令面板 `MCP: List Servers` 应看到 `blockbench`；
- 装了 Codex / Copilot 等支持 MCP 的扩展后，工具会出现在对话里
  （通常名字形如 `mcp__blockbench__project_create`）。

如果你常打开的是外层 `d:\VScode` 而不是本项目目录，把同一段配置放进
用户级 `settings.json`（Ctrl+Shift+P → Preferences: Open User Settings (JSON)）：

```json
{
  "mcp": {
    "servers": {
      "blockbench": {
        "command": "d:\\VScode\\python\\blockbench-mcp\\.venv\\Scripts\\python.exe",
        "args": ["d:\\VScode\\python\\blockbench-mcp\\main.py"]
      }
    }
  }
}
```

## 2. Codex CLI

推荐用 Codex 自带的管理命令（会自动写 `%USERPROFILE%\.codex\config.toml`）：

```powershell
codex mcp add blockbench -- "d:\VScode\python\blockbench-mcp\.venv\Scripts\python.exe" "d:\VScode\python\blockbench-mcp\main.py"
codex mcp list
```

等价的 config.toml 写法（二选一，不要同时用两套入口重复注册）：

```toml
[mcp_servers.blockbench]
command = "d:\\VScode\\python\\blockbench-mcp\\.venv\\Scripts\\python.exe"
args = ["d:\\VScode\\python\\blockbench-mcp\\main.py"]
```

然后启动 `codex` 对话。注意：上面那种 `"mcpServers": {...}` JSON 是
Claude Desktop 的格式，不适用于 Codex CLI。

### 对话式使用的最小验收

```text
先用 MCP 工具列出能力：mcp__blockbench__project_status
如果第一步没有项目会报“没有打开的项目”，这是正常现象——
说明工具已被调用；接着让它 project_create 再试。
```

要一口气“创建一只狗”，建议给模型明确产出与参照（见 README 的
Agent 提示词思路），例如：参照 examples 里的 img2bb_coyote 结构、
按 bedrock/GeckoLib 格式、生成后导出。第一次调用时 Codex 会请求授权，
选择允许即可；若希望自动执行请用 Codex 的审批模式参数（如 --full-auto，
需自行评估风险）。

### 判断“真的挂载了”以 /mcp 为准

Codex 会话内输入 `/mcp`：能看到 `blockbench` 且下面列出工具，
就是挂载成功（`Auth: Unsupported` 对本地 stdio 服务器是正常显示，可忽略）。
不要用“让模型描述它自己的工具列表”来判断——模型可能误报，
`/mcp` 才是运行时事实。

之后做一次真实调用验收：

```text
请直接调用 mcp__blockbench__project_create(name="dog_test", format="bedrock")，
再调用 mcp__blockbench__project_status，并把两个工具的真实返回贴出来。
```

若 `/mcp` 有工具但模型说“无法调用”，说明 MCP 已挂载、问题在模型/
provider 的工具调用层（本项目与配置无关）。

### DeepSeek 自定义模型的已知坑（必查）

症状：`/mcp` 显示 `blockbench: connected (xx tools)`，但模型反复说“工具列表里
没有 mcp__* 工具”。根因（Codex issue #36382）：DeepSeek 官方安装脚本写入的
`~/.codex/models.json` 把 `supports_search_tool` 声明为 `true`，导致 Codex 把所有
MCP 工具注册成“延迟暴露”，而 DeepSeek 不会去搜索工具。

修复：编辑 `%USERPROFILE%\.codex\models.json`，把三个 DeepSeek 模型条目里的
`"supports_search_tool": true` 改成 `false`，保存后完全退出并新开 Codex/CC GUI 会话。

### CC GUI（mossx.vscode-cc-gui）验收口径

CLI 能原生调用 `mcp__blockbench__*` 后，CC GUI 仍不注入时：

1. 确认对话用的是 **Codex 运行时**（复用 ~/.codex 配置），而不是
   “直连 API / Claude” 模式——只有 Codex 运行时才会加载 `[mcp_servers]`；
2. 完全退出 VS Code（含 CC GUI 的 Codex daemon）后重开，并**新开对话**
   （旧对话持有会话创建时的工具面，不会刷新）；
3. 在 CC GUI 的 MCP 面板确认 blockbench 已连接且列出工具；若状态仍显示
   “未知/未连接”，以对话里是否出现 `mcp__blockbench__` 原生调用为准；
4. 验收原话：只允许调用 mcp__blockbench__project_status，禁止 python/shell 自行启动。

如果 CC GUI 的 Codex 新对话依旧看不到工具（而 CLI 正常），属 CC GUI 侧限制，
可在对话窗口实测能否使用，作者在 CC GUI 连接未知的情况下仍能正常使用。

## 3. Claude Desktop / Cline / 其他 MCP 客户端

统一写法（stdio）：

```json
{
  "mcpServers": {
    "blockbench": {
      "command": "d:\\VScode\\python\\blockbench-mcp\\.venv\\Scripts\\python.exe",
      "args": ["d:\\VScode\\python\\blockbench-mcp\\main.py"]
    }
  }
}
```

- Claude Desktop：编辑 claude_desktop_config.json 后完全退出重开；
- Cline：设置 → MCP Servers → 添加，类型选 stdio。

## 4. 免 Python 环境的用户

打 tag 触发 GitHub Actions 产出 blockbench-mcp.exe 后，把配置里的
command 换成 exe 绝对路径、删掉 args 即可（见 README“发布与使用”）。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 客户端提示启动失败/连接被拒 | 路径写错或用了 main.py 而非 venv python；检查反斜杠与引号 |
| 工具列表为空 | 重启窗口/客户端让 MCP 重新拉起；先用 smoke_stdio.py 验证服务本身正常 |
| 改了代码不生效 | 重启客户端会话（stdio 进程每次都由客户端拉起） |
| 中文乱码 | 终端里给进程加 env: {"PYTHONIOENCODING": "utf-8"}（MCP 客户端无影响） |
