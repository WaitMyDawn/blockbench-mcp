# 样例库（examples/）

- `models/`：**完整的 .bbmodel 文件**（不是硬编码）。运行时按 `catalog.json` 读取。
- `catalog.json`：清单，登记每个样例的许可证、来源、作者、结构与用途。

## 收录规则

1. 只收录**明确许可证**的资源：MIT / CC0 / 官方授权。无许可证声明 = ARR，不收录。
2. 每个样例必须能说明来源与作者（catalog 的 `source`/`author`）。
3. 你（仓库所有者）的自研文件默认 ARR，发布前请决定是否开源并更新 `license_spdx`。

## 如何加自己的样例

```text
1. 把 xxx.bbmodel 放进 examples/models/
2. 在 catalog.json 的 samples 里加一条记录
3. 之后 project_load_example(template="xxx") 即可加载
```

## 当前样例

| id | 名称 | 许可证 | 用途 |
|---|---|---|---|
| redeemer | Redeemer GeckoLib 步枪 | ARR（作者自持） | GeckoLib 成品道具基准 |
| polar_bear | Polar Bear 基岩实体 | MIT | 生物骨骼与动画学习 |
| bettermodel_demon_knight | Demon Knight 人形骑士 | MIT | Java 模组实体建模/动画 |
| img2bb_chimpanzee / img2bb_coyote / img2bb_elephant | 照片转模型动物 | MIT | 有机体/四足体块参考 |

说明：候选审核中因许可证不明（ARR 默认）或结构暂不支持而未收录的样例，
会记录在本批收集备注里，不放入 models/。
