# 完整示例：站姿金毛 + 5 动画（可复用模板）

这份文档把“用 MCP 做一只带材质、带动画的狗”的完整会话拆成**可直接复用**的模板：
先给一段“一句话目标”，再给**可直接粘贴给 AI 的主提示词**，然后是**关键数据表**
（骨骼/色板/动画）与**少踩坑清单**。照此描述，通常能一次拿到更像样的结果。

> 适用：Codex CLI / CC GUI 等已确认 `/mcp` 里能看到 `mcp__blockbench__*` 工具的新会话。

---

## 一、一句话目标

> 做一只站姿金毛（块状 Minecraft 风格，带材质贴图与 5 个动画），用于 Java 版 GeckoLib
> 道具/实体演示；最终落盘到 `D:\Ps_2022_PSD\item\bbmcp`。

---

## 二、可直接粘贴的主提示词

```text
【任务】用 mcp__blockbench__* 工具做一只站姿金毛：建模 + 分部位材质贴图 + 5 个动画。
只允许调用 mcp__blockbench__* 系列工具；禁止用 python/shell 启动服务器或绕过工具写模型文件。
全程“先查询再修改”，每步用工具真实返回核对，不编造结果。

【阶段 0】project_status 确认工具已注入；若没有 mcp__blockbench__ 前缀，直接报“MCP 未注入”并停。

【阶段 1 · 视觉 canary】project_create(name="canary", format="bedrock", texture_width=16, texture_height=16)
→ texture_create(name="t",16,16,color="#FF2200") → cube_create(name="box",[0,0,0],[8,8,8],texture="t")
→ render_preview 看能否描述颜色/朝向；若“看不到图/答非所问”就停，报告视觉通道不通。

【阶段 2 · 参考学习】project_load_example(template="img2bb_coyote") → project_status + render_ascii
读它的骨骼与体块比例（只读，不产出）。

【阶段 3 · 精细几何】project_create(name="dog_v2", format="bedrock", texture_width=64, texture_height=64)
创建骨骼：root / body / head / ear_l_bone / ear_r_bone / tail / leg_fl / leg_fr / leg_bl / leg_br，
骨骼 origin 放在关节。几何建议：躯干拆胸口+腰腹两段并加过渡块；头=颅+吻部+鼻梁凸起；
耳两片薄立方旋转下垂；尾两段；每条腿分大腿/小腿/爪，前后粗细不同；加胸肌凸起与臀圆角。
目标 28~42 个 cube，全部语义化命名并挂到对应骨骼，脚底 y≈0，无 0 厚度面。
每完成一块 project_status 核 counts；每 2~3 步 render_preview 看图，比例不对就 cube_update/bone_update 修。
【重要】先规划出完整“部位表”再逐块 bone_create/cube_create，命名唯一且语义化。

【阶段 4 · 真实贴图】先做“放大画布 + 重排 UV”，再做分部位配色：
1. 用只读脚本从 .bbmodel 读每个 cube 尺寸，按 2× 缩放生成 256×256 整数 UV 打包清单（0 重叠），
   再用 texture_update 把画布升到 256（会自动同步项目分辨率），最后逐块 cube_update(faces=...) 应用新 UV。
2. 分部位上色（texture_paint_cube / texture_paint，一次一个 undo 事务）：
   - 躯干/头/四肢侧面 = 暖金 #D9A05B，顶面压深 #B07A35
   - 胸/腹/口鼻/前爪 = 奶油 #F2E3C4（口鼻/爪用更浅 #F0E0BD）
   - 耳 = 红棕 #8A4B24；鼻 = 近黑 #1C1512；眼 = #241A12（头部正面两个点 + 白高光）
   - 鼻梁 = #C98E4B；尾梢 = 偏浅 #EAD3A0
3. 做体积：给所有侧面叠“顶部深 #A9885F → 底部白”垂直渐变(blend=multiply)，顶面加 radial 高光(overlay)。
4. 做材质：整张画布叠一层 noise(amount≈0.07)，肩/头/胸用 soft_stamp 柔边高光，腹部/胸前用
   multi_gradient 奶油→暖金的柔和过渡。
5. 验收：texture_validate_uv 应 0 问题；render_preview / render_texture 看图。

【阶段 5 · 动画】5 个动画（吻朝 +Z：X=俯仰、Y=偏航、Z=滚转；全部 linear 插值）：
1) walk       0.8s / loop   对角腿（左前+右后同相、右前+左后反相）前后摆 ±18°，身体微起伏，头/尾微动
2) run        0.4s / loop   腿摆 ±30°，身体前倾 +8°、起伏更大，尾上扬小摆
3) pounce     1.2s / loop   root.position 蹲→跃→腾空→落地，身体俯仰，四肢蹬/收
4) crouch     1.0s / hold   身体下沉后倾、后腿折叠、前腿撑地、抬头、尾上卷
5) head_shake 0.6s / loop   头绕 Z 快速 ±18° 往复 + 双耳反向甩动
完成后 keyframe_list 核对，再 project_save。

【阶段 6 · 收尾与导出】validate_quality 要求 0 error（warning 先修）；render_ascii(cells=40) 确认“狗样”；
然后落盘到 D:\Ps_2022_PSD\item\bbmcp：
- project_save(path="D:/Ps_2022_PSD/item/bbmcp/dog_v2.bbmodel")
- export_model(target="bedrock", path="D:/Ps_2022_PSD/item/bbmcp")
- export_model(target="geckolib", path="D:/Ps_2022_PSD/item/bbmcp", modid="mymod", category="item", model_name="dog_v2")

【阶段 7 · 真机截图】仅当 Blockbench 已打开且已加载 plugin/blockbench_mcp_bridge.js：
先 project_open(path=...) 把 .bbmodel 载入 MCP 会话，再 blockbench_screenshot(path=".../dog_v2_blockbench.png")；
若 blockbench_command(open) 返回“请手动打开”，就让用户 File>Open 后重试，不要假装成功。

【回报】最终 counts、validate_quality 分数、render_ascii 前视图、阶段1 canary 结论、
实际写出的文件清单（含字节数）；明确哪些是“真实渲染验收”、哪些只是“结构自检”，
不得声称已产出游戏级成品——纹理精修和造型终审由人在 Blockbench 完成。
```

---

## 三、关键数据表（本次实际用到的值）

### 骨骼

| 骨骼 | origin | 说明 |
|---|---|---|
| root | [0,0,0] | 根；可做整体位移（pounce） |
| body | [0,9,1] | 躯干；上下起伏/俯仰 |
| head | [0,13,7.5] | 头；转/甩 |
| ear_l_bone / ear_r_bone | [±1.8,15,8] | 耳朵甩动 |
| tail | [0,8,-6] | 尾巴摆动 |
| leg_fl / leg_fr | [±2,6,4.5] | 前腿（肩为轴） |
| leg_bl / leg_br | [±2,6,-3.5] | 后腿（髋为轴） |

### 几何（29 个 cube）

躯干 `chest / abdomen / torso_transition / chest_muscle / hip_round`；
头部 `cranium / muzzle / muzzle_bridge / nose / jaw_lower / cheek_l / cheek_r`；
耳 `ear_l / ear_r`；前腿 `thigh_fl/fr + shin_fl/fr + paw_fl/fr`；
后腿 `thigh_bl/br + shin_bl/br + paw_bl/br`；尾 `tail_base / tail_tip`。

### 色板（Alex's Mobs 风格）

| 含义 | 颜色 |
|---|---|
| 基础金毛色 | `#C89B6C` |
| 侧面/背部主干 | `#D9A05B` |
| 顶面压深（背部阴影） | `#B07A35` |
| 胸/腹 | `#F2E3C4` |
| 口鼻/下颚/爪 | `#F0E0BD` |
| 耳缘 | `#8A4B24` |
| 鼻头 | `#1C1512` |
| 眼 | `#241A12` |
| 鼻梁 | `#C98E4B` |
| 尾梢 | `#EAD3A0` |
| 体积渐变暗部（multiply 用） | `#A9885F` |

### 动画

| 动画 | 时长/循环 | 主要骨骼通道 | 要点 |
|---|---|---|---|
| walk | 0.8s/loop | 4 腿 rotation / body position / head / tail | 对角腿 ±18°，身体 -0.6 起伏，头+4° 点，尾 ±8° 摆 |
| run | 0.4s/loop | 4 腿 rotation / body rotation+position / head / tail | 腿 ±30°，body 前倾 +8°、-1 起伏，头+5°，尾 -10° 扬 + ±10° 摆 |
| pounce | 1.2s/loop | root position / body rotation / 4 腿 / head / tail | root 蹲[0,-1,0]→跃[0,2,4]→腾[0,3.5,7]→落[0,0.5,8]→回；身体俯仰 +18/-6；后腿 -45°→+30°→-20° |
| crouch | 1.0s/hold | body position+rotation / 后腿 / 前腿 / head / tail | body 下 -1.5 倾斜 -15°；后腿折叠 -50°；头抬 -10°；尾上卷 +15° |
| head_shake | 0.6s/loop | head rotation / ear_l/r_bone | 头绕 Z ±18° 往复；双耳反向 ±12° |

---

## 四、少踩坑清单（本次踩过的）

1. **先把画布放大 + 重排 UV，再着色**。29 个 cube 若都挤在 64×64，每个面只有几像素，
   画不出细节。升到 256×256 并重排整数 UV（`texture_validate_uv` 0 问题）后再画。
2. **别只涂点缀**。只给耳/鼻/腹撒 accent，躯干/头/四肢大面积还是底色 = “99% 纯色”。
   一定把**大面**也覆盖到。
3. **先查 UV 再画**。`texture_face_map` 确认每个 face 在画布上的落点；同一画布可能被镜像
   的左右部件共用。
4. **颜色/透明度用 `#RRGGBB[AA]`**，渐变用 angle/color1、radial 用 center/radius、
   阴影用 blur/offset/strength、噪点用 amount/seed。
5. **眼睛要放在“吻部朝向”那一面**。这只狗吻朝 +Z，面是 `south`；放到 `north` 会到脑后。
6. **动画只能作用于骨骼**，且骨骼 origin=旋转轴心；腿/手摆动要把 origin 放关节处。
7. **重开模型要重新 `project_open`**：会话切换后内存项目会清空，先 `project_open` 再操作。
8. **放大画布后分辨率要同步**：`texture_update` 已自动同步项目级 `resolution`，无需手动改尺寸。
9. **真机截图**：先 `project_open` 把 .bbmodel 载入 MCP 会话，再 `blockbench_screenshot`；
   若 `blockbench_command(open)` 报“请手动打开”，就让用户 File>Open 重试。
10. **老实区分**：`validate_quality`/`render_ascii`/`texture_validate_uv` 是结构自检；
    `render_preview`/`blockbench_screenshot` 才是真实看图；终审在 Blockbench。

---

## 五、基于此继续调优

- **改比例/细节**：`cube_update(cube=..., from_/to=...)` / `bone_update`，再 `render_preview` 看。
- **加强某个部位材质**：`texture_paint_cube(element=..., side=..., top=..., bottom=...)`
  或 `texture_paint` + `ops`，再用 `render_preview`/`blockbench_screenshot` 验收。
- **微调动画**：改对应 `keyframe_add` 的 `time/values/length`，或 `animation_create` 改
  `loop`（walk/run 用 loop，坐下用 hold）。
- **换配色**：直接告诉 AI“把背部改成更深棕、腹部的奶油减少一点”，它会用
  `texture_paint` 重新上色并返回缩略图。
- **来回调整时**：每一步都让 AI 用真实工具返回核对，并明确“哪些是结构自检、哪些是真实
  渲染”，你才能真正知道效果对不对。
