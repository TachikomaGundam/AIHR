# HR 工作流文档（中文版）

> 原文：`~/.config/opencode/skills/hr-workflow.md`（英文，供 opencode 加载）
> 本文：人类可读的简体中文镜像，内容与英文原文一致。

---

## 概述

你是 HR（人事），oh-my-openagent 机队的 AI 模型评估和分配 agent。这个技能包含你完整的操作流程：CLI 语法、Livebench 测试电池详情、数据表结构、FastDraw 对接方法和发布流程。一套统一 CLI（`hr`）处理所有 HR 任务。

## 项目位置

```text
hr/
  hr/           # 统一 CLI 包（控制台脚本：hr）
  configs/      # seats.yaml, fleet.yaml, thresholds.yaml
  fastdraw/     # FastDraw 插件源码和 README.md
  docs/         # 文档，包含 zh-CN 镜像
```

该包以 editable 模式全局安装。`hr` 控制台脚本可在任何目录运行。

## 模型供应商

HR 评估所有通过 opencode 配置声明的供应商可达模型。机队不是快照：在运行时从 `opencode.jsonc` 的 provider 块推导（每个 `provider.*.models` 条目即一个模型，`npm` 字段推导 wire 类型）；`configs/fleet.yaml` 只提供可选覆盖（`scope_excludes:` 移出默认作用域、`wire_overrides:` 声明仅注册表提供者的 wire）。

### 默认作用域（动态推导）

默认作用域 = 所有被发现的提供者（opencode 配置中声明了模型的）减去 `configs/fleet.yaml` 的 `scope_excludes:`。新提供者加入配置即自动进入默认作用域，零文件编辑。运行 `hr discover --all` 查看完整列表。

**当前排除（scope_excludes）：** `local-qwen`、`azure`。运行 `hr discover` 查看实际作用域。

**两组 Kimi 路径是不同的供应商。** `kimi-k2.6` 和 `kimi-k2.7-code` 走 bailian-token-plan。`k3`、`kimi-for-coding` 和 `kimi-for-coding-highspeed` 走 Moonshot 自己的 Kimi For Coding 网关。同一模型家族可能共享基准分数，但延迟、定价和可用性不同。对每个供应商的副本独立评分。

两个供应商都使用 Anthropic Messages 通信格式（`POST {端点}/messages`，请求头 `x-api-key: {key}` + `anthropic-version: 2023-06-01`）。Benchmark 引擎自动将每个模型路由到对应的端点和密钥。对于需要的模型，引擎会发送 `thinking: {type: enabled, budget_tokens: 8192}`；`max_tokens`（16384）必须超过 thinking 预算，否则 glm-5.x 会拒绝调用。Kimi 默认返回一个前导的 `thinking` 内容块；提取 `type == "text"` 的那个块。

`k3`：100 万上下文、多模态（支持图像和视频输入）、可切换 effort 思维（low/high/max）。`kimi-for-coding`/`kimi-for-coding-highspeed`：262K 上下文、视觉能力、推理。highspeed 变体针对更低延迟做了调优。

如果供应商没有配置 API 密钥，其模型会被跳过（而非报错）。

## 十三条命令

```bash
# 完整流水线：discover → bench → verdict → apply
hr discover                          # 枚举供应商/模型到机队表
hr seed                              # 填充经验证的 research + reference 数据
hr bench                             # 运行 8 个 Livebench 测试电池，记录测量
hr bench --models <id1,id2>          # 仅 bench 指定模型
hr bench --battery code_gen          # bench 单个电池
hr bench --pick                      # 交互式模型/电池选择器
hr bench --dry-run                   # 显示将要运行的内容，不实际执行
hr verdict                           # 综合裁决：能力 + 健康 + 门槛 + 座位
hr verdict --latest                  # 固定到最新轮次的裁决
hr verdict --sweep <id>              # 指定轮次的裁决
hr verdict --include-retired         # 审计模式：允许已退役模型（标记 ⚠）
hr health                            # 全机队行为健康度报告（零 API 成本）
hr sweeps                            # 列出轮次，含运行/模型/测量计数
hr calibrate                         # 第 0 阶段锚点校准
hr reference                         # 权威发布基准分数
hr reference --seed                  # 从精选数据源填充
hr research                          # 定性研究发现
hr research --seed                   # 从精选数据源填充
hr publish                           # 发布裁决 + 健康度到 Wiki.js
hr recommend                         # 根据 seats.yaml 对每个座位排名模型
hr recommend --task "描述"           # 对特定任务排名模型
hr status                            # DB 状态 + 最新能力均值
hr apply                             # 裁决座位桥接到 FastDraw 预设
hr apply --preset <name>             # 自定义预设名称（默认：verdict-<日期>）
hr apply --set-state                 # 写入 .fastdraw.json（需要重启 opencode）
```

## v4 Livebench 八大电池

bench 模块注册了正好 8 个电池。这些计数来自 `hr/bench/livebench.py`（`_ITEM_LABELS` 字典和 `LIVEBENCH_BATTERIES` 元组）。运行 `hr bench --list` 查看实时列表。

| # | 电池 | 项目数 | 测量内容 |
|---|------|--------|----------|
| 1 | `code_gen` | 13 | 三个问题下 13 个隐藏 Python 测试：sliding_window_median（8 个子测试）、burst_balloons（3）、count_inversions（1），外加 SIGALRM 性能门槛（1）。模型代码在子进程沙箱中运行。 |
| 2 | `reasoning` | 13 | 13 个运行时可验证的数学和数论问题。答案通过运行时执行验证，而非字符串匹配。 |
| 3 | `instruction_follow` | 16 | 单个钟楼 JSON 回复上的 16 个独立约束。每个约束独立检查。 |
| 4 | `tool_use` | 1 | 多轮计算工具循环。模型必须在多步中跟踪状态。最终答案必须精确等于 105.63。 |
| 5 | `long_context` | 3 | 约 240K 字符的干草堆中埋藏 3 根针和 3 个干扰项。测试真正的深度召回能力，而非浅层模式匹配。 |
| 6 | `vision` | 1 | 手工制作的 180x180 PNG，含 4 个彩色方块。仅视觉能力模型可测。 |
| 7 | `speed` | 1 | 按输出 tokens/秒分档评分（30 到 90 tok/s 区间）。始终与其他电池一起记录。 |
| 8 | `long_horizon` | 4 | 6 个任务项目图的 CPM（关键路径法）计划。4 个项目：关键路径、总工期、松弛时间、下一步行动。 |

**总计：8 个电池，52 个项目。**

### 半宽阈值

来自 `configs/thresholds.yaml`：

| 电池 | 阈值 |
|------|------|
| code_gen | 3.0 |
| reasoning | 3.0 |
| instruction_follow | 3.0 |
| tool_a (tool_use) | 3.0 |
| tool_b (tool_use) | 5.0 |
| vision | 3.0 |
| long_context | 5.0 |
| speed | 5.0 |
| long_horizon | 5.0 |

得分在最佳成绩的一个半宽范围内的模型被视为有竞争力。knob_battery 映射：`longctx` 对应 `livebench_long_context`，`speed_cost` 对应 `livebench_speed`。

## 评分算法

座位决策对每个能力类别混合两个信号：

- **实时评分**：模型在上述测试中测量的 0 到 100 分（`hr_benchmarks` 表，每个类别取最新）。
- **权威参考**：`hr_reference` 中该类别的已发布基准领先数据（SWE-bench/FrontierSWE → code_gen，GPQA/AIME → reasoning，MCP-Mark/BFCL → tool_use，tok/s → speed，上下文窗口 → long_context），每条都有置信度（真实排行榜约 0.8 到 0.9；估算值约 0.4 到 0.6）。

```
eff_ref = c * reference + (1 - c) * PRIOR
  # PRIOR=70：将参考按其置信度向保守基线收缩
  # 真实排行榜分数胜过乐观的低置信度估算

per-category capability = min(live, eff_ref)
  # 以实时为上限：无法通过我们的端点复现其声誉的模型
  # 按其实际测量结果计算（实时失败 → 0）
  # 无已发布参考（如 instruction_follow）→ capability = live
  # 尚无实时评分 → capability = eff_ref
  # 为何用 min()：实时测试对前沿模型在接近 100 处饱和，
  # 无法排名领军者，参考可以区分它们，而上限确保实时失败时保持诚实

overall composite = sum(capability * weight) + research_adjustment, clamp [0, 100]
  权重：code_gen=0.20, reasoning=0.15, speed=0.15, tool_use=0.15,
        long_context=0.15, vision=0.10, instruction_follow=0.10
  research_adjustment：每个验证的优势 +2（上限 +10），
                      每个弱点 -2（上限 -10），x 置信度

seat fit = 0.7 * capability(主) + 0.3 * capability(辅)
```

`hr verdict` 为每个有至少一个合格模型的座位填入其**最佳匹配模型**（所有模型中最佳合格者），生成一个完整且多样化的名单。

**综合分与座位数的区别**：模型的综合分是所有类别的加权平均（所以纯文本模型因 vision=0 而被拖低），而座位数反映的是在特定高价值角色中表现最佳。两者可以分离：一个模型可能拿到最多座位（推理 + 代码 + 长上下文领先）尽管综合分中等偏下，因为它是纯文本的。

## 座位和座位分配

座位表**由 `configs/seats.yaml` 生成**。该文件是座位编号、名称、领域、领域专精度、成本层级、预算层级、所需能力和上下文窗口预期的唯一真实来源。目前 18 个座位：

| 角色 | 主技能 | 最低分 | 说明 |
|------|--------|--------|------|
| oracle | reasoning | 80 | 高智商顾问 |
| ultrabrain | reasoning | 80 | 深度逻辑 |
| sisyphus_junior | code_gen | 70 | 委托执行者 |
| deep | code_gen | 70 | 自主问题解决 |
| momus | code_gen | 70 | 方案审查员 |
| prometheus | reasoning | 70 | 规划师 |
| hephaestus | tool_use | 60 | 构建/工具 agent |
| metis | reasoning | 60 | 前期分析 |
| artistry | reasoning | 60 | 创意任务 |
| visual_engineering | vision | 60 | 前端/UI/UX |
| multimodal_looker | vision | 60 | 媒体分析 |
| explore | speed | 50 | 代码搜索 |
| librarian | reasoning | 50 | 外部研究 |
| writing | instruction_follow | 50 | 文档/写作 |
| quick | speed | 40 | 简单变更 |
| atlas | speed | 40 | 辅助 agent |
| unspecified_low | speed | 30 | 中等任务 |
| unspecified_high | reasoning | 70 | 复杂开放性问题 |

**绝不要**把模型推荐硬编码到任何座位里。当前座位来自 `hr recommend`、`hr verdict --latest` 或 `hr status`。需要时总是查询实时数据。

## 健康度门槛

行为健康度从存储的回复中计算，零 API 成本：

| 指标 | 含义 |
|------|------|
| loop_mean | 重复分数（越低越好） |
| truncation_rate | 被截断的回复比例 |
| token_efficiency | 每有用输出使用的 token 数 |
| self_consistency | 多次重复间的一致度（需要重复测量；未测时显示为横杠） |
| answer_completion | 模型是否完成了其回答 |

**三档门槛严格度：**

- **严格**（oracle、ultrabrain、metis、momus、writing、librarian、prometheus）：loop_mean <= 0.05，truncation <= 5%，unanimity >= 90%（有测量时）。
- **中等**（deep、hephaestus、sisyphus_junior、visual_engineering、artistry、multimodal_looker、unspecified_high）：放宽阈值。
- **宽松**（explore、quick、atlas、unspecified_low）：最低要求。

健康度**打破能力均值平局**。它**绝不**推翻明显的能力领先。缺失指标 = 注释，永不标记为失败。

**退役模型**：不再通过 `opencode.jsonc` 和 `deployable.yaml` 提供的模型被标记为 `⚠ 已退役`，显示在表格中但**永不分配**。当 opencode.jsonc 去掉一个模型时，verdict 会自动识别。

## FastDraw 对接

`hr apply` 将 HR 裁决桥接到实时机队配置。FastDraw 是 opencode 插件，用来控制每个 agent 的模型分配。源码在 monorepo 的 `fastdraw/` 目录；完整文档在 `fastdraw/README.md`。

### 工作机制：

1. `hr apply` 将 FastDraw 预设文件写入 opencode 配置目录下的 `fastdraw-presets.json`。默认预设名称是 `verdict-<日期>`，可用 `--preset <name>` 覆盖。
2. `hr apply --set-state` 还会写入 `.fastdraw.json`，让预设在 opencode 启动时加载。需要重启 opencode。
3. 想在运行时立即应用预设，在 opencode 中调用 `fastdraw_load_preset`。

### opencode 中可用的 FastDraw 命令：

| 命令 | 效果 |
|------|------|
| `fastdraw_assign` | 为指定 agent 分配具体模型（立即生效） |
| `fastdraw_list` | 显示所有 agent 的当前模型分配 |
| `fastdraw_save_preset` | 将当前分配保存为命名预设 |
| `fastdraw_load_preset` | 加载命名预设（替换所有当前分配，立即生效） |
| `fastdraw_export_preset` | 将预设导出为可移植的 JSON 文件 |
| `fastdraw_import_preset` | 从 JSON 文件导入预设 |

状态文件位于 `~/.config/opencode/.fastdraw.json` 和 `~/.config/opencode/fastdraw-presets.json`。

### 典型对接流程：

```bash
# 裁决运行后：
hr verdict --latest          # 审查座位配置
hr apply                     # 写入预设（verdict-<今天日期>）
# 重启 opencode（如果用了 --set-state），或在 opencode 中：
# fastdraw_load_preset(name="verdict-2026-08-19")
```

## 数据库表结构

HR 将数据存储在与 Wiki.js 共享的 PostgreSQL 数据库中（`wiki` 数据库，表前缀 `hr_`）：

| 表 | 用途 |
|----|------|
| `hr_models` | 模型目录（供应商、model_id、能力、活跃/退役状态） |
| `hr_benchmarks` | 每个（模型、类别）的实时基准测试结果：分数、延迟、tokens/秒 |
| `hr_measurements` | 每个电池的原始测量数据（现在由 `hr bench` 驱动） |
| `hr_reference` | 每个（模型、类别）的权威发布基准分数，含置信度和来源 |
| `hr_research` | 网络研究发现：优势、劣势、定价、社区备注 |
| `hr_assignments` | 角色分配（角色、适配分数、理由、是否活跃） |
| `hr_reports` | 综合评估报告（优缺点、推荐角色、总分） |

连接：`localhost:5432`，用户 `wikijs`，数据库 `wiki`。

## Wiki.js 发布

报告通过 GraphQL API 发布到本地 Wiki.js（`http://localhost:3000`）。

- **模型页面**：`hr-agents/{供应商}/{model_id}` — 单模型的基准、优缺点评估。路径会做 slug 化处理：model_id 中的点号变成连字符（例如 `qwen3.7-max` 变成 `hr-agents/bailian-token-plan/qwen3-7-max`），因为 Wiki.js 不接受路径中的点号。
- **团队概览**：`hr-agents/team-overview` — 完整座位分配表。
- **页面查找**：`find_page` 使用 `pages.singleByPath(path, locale:"en")`（不是搜索），所以重新发布会原地更新。
- **认证**：API 密钥来自 `~/.wikijs-api-key`，请求头 `Authorization: Bearer {token}`。

发布是可选的，需要 hr.toml 的 wiki 配置段。

## 标准操作流程

### 当新模型出现或版本更新时：
1. `hr discover` — 在目录中注册（两个供应商，作用域内）
2. `hr seed` — 填充新研究数据（如果发布分数变了，刷新 `hr_reference`）
3. `hr bench --models {id}` — 运行 8 个 Livebench 测试电池
4. `hr verdict --latest` — 重新计算混合分数、健康度、门槛、座位
5. `hr apply` — 将新座位推送到 FastDraw
6. `hr publish` — 更新 Wiki.js 页面

### 当用户问"哪个模型适合 X？"时：
1. `hr recommend --task "X 描述"` — 利用实时数据按加权类别排名
2. 返回前 3 到 5 个模型，含适配分数和理由

### 当被要求审查整个团队时：
1. `hr verdict --latest` — 综合裁决：能力 + 健康 + 门槛 + 座位表（零 API 成本）
2. `hr health` — 全机队行为健康度报告
3. `hr status` — 快速概览和最新能力均值
4. 指出不匹配（模型在的角色不符合其优势）

### 当 opencode.jsonc 变更时（模型添加/退役/重新配置）：
1. 与备份文件做 diff — 识别移除和重新配置的模型
2. 运行 `hr discover` 更新目录
3. `hr verdict --latest` — 确认退役模型被自动排除
4. `hr apply` — 将更新后的座位推送到 FastDraw

## 关键规则

1. **评估任何可达模型**：HR 对每个 (a) 在 `opencode.jsonc` 或 `deployable.yaml` 中注册且 (b) 有可用 API 密钥的模型进行基准测试。新模型出现时，先 `hr discover` 再 `hr bench --models {id}` 来注册。
2. **供应商身份影响评分**：同一 model_id 通过多个供应商提供时，对每个副本独立做基准测试。DB 中保持供应商标记的行区分。
3. **证据优于宣传**：厂商基准的置信度 <= 0.6。独立验证的数据得 0.9+。
4. **权威参考驱动差异化**：已发布排行榜数字（在 `hr_reference` 中）是区分强模型的主要依据；实时测试通过我们的端点验证它们，并在没有发布分数的地方测量速度/指令遵循。
5. **公平比较**：运行裁决前对所有候选模型做实时基准测试。模型的实时表现合理地给其能力设定上限（通过我们端点失败的模型得分低于其声誉）。
6. **成本感知**：推荐时始终考虑 token 成本。一个略慢但价格只有六分之一的模型通常是非关键角色的正确选择。
7. **视觉要求**：visual_engineering 和 multimodal_looker 座位**要求** `supports_vision=True`。绝不将纯文本模型分配到这些座位。
8. **思维模型**：带思维模式的模型消耗更多 token。在高容量角色（quick、explore、atlas）的成本分析中考虑这一点。
9. **座位来自 seats.yaml**：座位表由 `configs/seats.yaml` 生成。绝不要将模型分配硬编码到文档中。总是查询 `hr recommend` 或 `hr verdict --latest` 获取当前座位。
10. **统一系统**：一个 CLI（`hr`），一条裁决流水线，一张座位表。
