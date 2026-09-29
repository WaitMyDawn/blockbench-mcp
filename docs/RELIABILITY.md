# 编辑可靠性与原生动画验收

第一阶段覆盖文档事务、动画导出、静态变换、原生同步与截图。第二阶段加入原生动画采样、连续性报告、动态可见范围、相机预设与 GIF 预览。

## 推荐工作流

1. `project_create` 或 `project_open`。
2. 用方块/骨骼/纹理工具编辑，密集动画优先 `keyframes_add_bulk`。
3. `project_sync(path=...)` 原子保存 `.bbmodel`，随后等待 Blockbench 新工程、纹理与视口就绪。
4. `blockbench_screenshot(expect_path=..., expect_elements=..., expect_revision=...)`。
5. 检查结果，再编辑、同步或 `export_model`。

同步要求插件桥 **0.5.0 或更新版本**。客户端需要重新连接 MCP 服务以获得新增工具；插件从仓库加载时热重载即可，若从安装目录副本加载需先同步副本。

## 事务与撤销

方块、骨骼、动画、纹理及绘制工具都提交文档事务。失败时恢复文档、脏状态和像素历史；成功产生一条文档撤销记录。没有实际变化的操作不会增加 revision 或历史。

- `cubes_create_bulk(atomic=True)`：任意项失败回滚整批。默认 `atomic=False` 保留已有的部分成功行为，成功部分仍可一次撤销。
- `keyframes_add_bulk(keyframes=[...])`：每项参数同 `keyframe_add`，任意失败回滚；默认只返回计数，`include_ids=True` 返回每项 UUID。覆盖已有关键帧和延长动画都可撤销。
- `project_undo` / `project_redo`：完整文档时间线，包含结构、绘制以及像素撤销/重做操作。
- `texture_undo` / `texture_redo`：像素历史。替换位图源、改变尺寸或删除纹理会清除旧像素历史；文档撤销可以恢复它。

文档快照容量为 8 步，像素历史为 50 步。大型动画应批量写入，避免逐帧调用的快照与传输开销。恢复的文档使用快照副本，后续编辑不会污染历史记录。

## 数值与导出

坐标、旋转、UV、关键帧值和时间拒绝 NaN/Infinity；时间与动画时长拒绝负值，不再静默改成 0。添加有效关键帧仍会自动延长动画。

Bedrock/GeckoLib：

- Step 用下一帧的 `pre` 保留上一帧值，用 `post` 表达跳变。
- Catmull-Rom 的 `post` 保留当前关键帧值，不再使用下一帧值。
- Bezier 暂不支持直接导出：明确报错，并建议在 Blockbench 烘焙；`.bbmodel` 可保留该插值选项。资源对导出先检查动画，避免该错误覆盖原有几何文件。
- 静态包围盒与软预览共用世界变换：默认 ZYX 欧拉顺序，先子后父，计入每级 Pivot。非导出骨骼的整棵子树不参与计算。

默认导出仍计算静态包围盒。动画实体可以显式使用 `export_model(bounds_mode="animated", sample_rate=12, bounds_margin=16, ...)`；Bedrock 与 GeckoLib 都使用所选工程全部动画的原生动态范围。动态采样失败时不会开始写资源。`bounds_margin` 单位是模型单位（16 单位/方块）。

## 原生同步凭证

`project_status` 返回 `project_id`、单调递增的 `revision` 和 `synced_revision`。新建/打开工程会建立新会话身份。撤销/重做也递增 revision，方便识别当前版本。

保存的 `.bbmodel` 附带 `mcp_sync` 元数据：会话身份、revision、唯一 `source_token`。同步时插件从实际读取的文件验证这组凭证，然后创建新原生工程，确认原生 UUID、路径、方块数量和纹理就绪，再等待旧工程关闭。读取或加载失败会保留旧工程；默认拒绝替换同路径的未保存修改，包括非活动标签。

`replace_unsaved=True` 是显式允许替换未保存修改的开关，默认关闭。`force_close=False` 保留旧接口的“同路径工程存在则拒绝重载”行为。同步失败不会更新 `synced_revision`；保存到磁盘和同步到视口是两个状态。

截图改用一次 `capture` 命令返回 PNG 与同一视口的版本凭证，避免分别请求 health 和 screenshot 的竞态。设置 `expect_revision` 时还验证会话身份、文件 token 和原生脏状态。部分格式插件会把刚加载的工程标记为未保存；当加载时没有撤销历史，桥接保存完整原生序列化内容基线。该工程只有在内容仍与基线一致时才允许版本截图/采样，返回 `load_baseline_unchanged=true`，不会更改未保存标记。对旧工程的覆盖保护始终保留。凭证证明加载来源；它不是所有原生修改的持续追踪系统。

## 原生动画验收（桥接 0.6.0）

先 `project_sync`，再使用这些正式工具，**不需要开启 eval**。工程发生编辑/撤销后必须重新同步；原生时间线播放中则拒绝采样，先暂停。

| 工具 | 输入与结果 |
|---|---|
| `animation_sample` | `samples=[{"animation":"idle","time":0.5}]`，最多 128 姿态；返回动态世界包围盒、地面穿透元素、骨骼的局部 position/rotation/quaternion/scale。position 为模型单位，rotation 为度。`include_elements=True` 可返回逐方块范围。 |
| `animation_review` | `animations` 默认全部；`transitions=[{"from":"takeoff","to":"fly"}]` 指定衔接。覆盖均匀时间点、关键帧及关键帧左极限，报告循环/衔接姿态差、单侧速度差、地面问题、动态 bounds 与 render_bounds。最多 5000 姿态，sample_rate 为 0.1..60。 |
| `animation_preview` | `animation,time,path`；camera 为 hero/front/rear/side/top，原子采样/取景/截图并返回姿态与相机凭证。`fit_bounds` 可以传 review 的 bounds，以固定多帧构图。 |
| `animation_render` | `animations=["takeoff","fly","fly","landing"]`，输出 GIF；固定整段序列构图与调色板。fps 为 1..20、size 为 64..768、最多 180 帧；完整生成后才替换输出文件。loop 默认 False。 |

采样使用原生 `BoneAnimator.displayFrame` 的已有轨道，保留原生曲线、父子关系与 Pivot 计算。避开 `getBoneAnimator`/`Animator.preview` 的补建轨道和效果副作用。每次操作通过 finally 恢复场景变换、时间、选中动画及相机；不添加文档历史，不改变播放开关、模型或保存状态。

`contact_exclude_prefixes=["fx_beam__"]` 仅排除该类方块的地面检查，**仍计入动态渲染范围**。三轴世界缩放均塌缩到原生零缩放阈值时才不计入；单轴薄翼仍保留。非导出骨骼及其子树不计入。

`pose_matches` 的阈值为位置 0.01 模型单位、旋转 0.01 度、缩放 0.0001。旋转姿态比较用四元数，360 度等价不会误报。速度差分别报告位置/旋转/缩放的每秒差值，帮助发现“姿态接上了但突然转向”的接缝；没有自动宣称动作自然或平滑。

动态范围是离散采样结果，曲线在时间点之间仍可能超出，所以默认另加 16 单位余量。此阶段限定独立 Cuboid 骨骼动画；不模拟动画控制器、IK、粒子、音效、运行时 Molang 变量和游戏物理。GIF 使用各帧区间左端姿态，播放时长舍入到 GIF 的 10ms 时间粒度。

桥接命令进入串行队列；HTTP 错误保留插件诊断，超时不会自动重试修改。旧版 `GET /screenshot` 保留，缺少版本凭证，适合直接取图。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/bridge_reliability.test.cjs
.\.venv\Scripts\python.exe scripts/verify_reliability_e2e.py
.\.venv\Scripts\python.exe scripts/verify_animation_e2e.py
```

Python 回归测试覆盖严格数值、撤销分支、批量失败回滚、混合像素/文档历史、插值语义、嵌套变换和同步保护。pytest 在存在 Node 时自动运行真实桥接 JavaScript 的异步测试；没有 Node 时明确跳过该项。

原生验收脚本经真实 stdio MCP 操作 `outputs/reliability_check` 专用工程，检查同路径、同方块数量的版本更新，版本截图与撤销后同步。启用 eval 时额外读取原生几何并验证未保存修改保护；未启用则跳过这两项，保留 JavaScript 测试覆盖。

动画脚本使用 `outputs/animation_stage2/dragon_copy.bbmodel` 专用副本，检查雷龙全部循环、7 组动作衔接、地面穿透、动态导出、五视角 PNG 与飞行/休息 GIF。原始雷龙文件在验证前后进行 SHA256 比对。eval 只在可用时用于独立对照完整原生预览与状态恢复；正式动画工具无需 eval。
