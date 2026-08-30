# HR: Agent Seat Matching and Capability Benchmarking

统一工具链，用于将自主 LLM 编码代理匹配至任务适当的席位、跨模型集群执行能力基准测试、并发布部署判定。单一安装包。13 条 CLI 命令。CLI 本身运行时不需要 API 密钥。

本文档（README.zh-CN.md）是英文规范版本的忠实镜像。当两者出现差异时，以英文版本为准。

## HR 实际做什么

HR 是为自主代理席位选择模型的决策辅助插件，不宣称某个模型“普遍最好”。它针对一个明确的席位或任务给出有边界的推荐；该推荐必须能追溯到版本化题库、记录的模型响应、健康门槛与 `configs/` 中的策略。

决策流水线为：`discover` 从 OpenCode 配置派生候选机队，`seed` 注册数据模型，`calibrate` 验证题库难度锚点，`bench` 或 Stage 0/1 扫描记录测量，`health`/`verdict`/`recommend` 应用能力与可靠性门槛，最后由 `apply` 将明确接受的席位分配导出为 FastDraw preset。除非显式调用 `--set-state`，HR 不会自行修改模型绑定。

## 结论状态

| 状态 | 含义 | 能否用于排名或分配 |
|------|------|--------------------|
| `pass` | 全部必需题目已测量且规则通过。 | 可以，仍需通过席位门槛。 |
| `fail` | 全部必需题目已测量但规则失败。 | 不可以。 |
| `inconclusive` | 样本不完整，或出现适配器/基础设施失败。 | 不可以；应重试或安全恢复。 |
| `invalid` | 题库或配置无法支持该规则。 | 不可以；先修复题库/配置。 |
| `not_applicable` | 模型没有所需模态或工具协议能力。 | 仅对该能力不可用，绝不能伪装为零分能力失败。 |

校准已使用 `pass`、`fail`、`inconclusive` 与 `invalid`。token cap、部分轮次或恢复后缺项表示“不确定”，而不是模型失败。

## 方法论

### 题库与评分

`itemrepo/` 是 Git 版本化的评测材料。每个题目包含 item key、类型、tier、payload 和评分说明；battery 将题目组成 reasoning、factuality/hallucination、vision、`tool_a`、`tool_b` 等能力组。可以确定性判断的内容使用 exact-match、schema、constraint、citation 与沙箱 unit-test grader；LLM judge 会显式标记，因为它引入第二个模型和第二个不确定性来源。

`hr calibrate` 的目标不是选择生产模型，而是检查锚点模型与题库难度区间是否仍然匹配。只有 tier 完整测量才能通过；题库缺项、解析失败或基础设施错误不能形成“空集通过”。

### 重复测量与分离度

Stage 0 低成本缩小模型池，Stage 1 用完整题库复测 finalist。测量按 model、battery、round、item、repetition 记录，支持审计与恢复。模型比较应使用匹配的 model/item 观测，题目是主要独立单位，重复调用估计网关和生成波动。

小的点分差不是席位决策。只有相关题目完整、置信/分离规则满足、候选都通过硬门槛时，一个候选才可替换另一个。当前实现记录 bootstrap separation 和 sequential precision；后续的每模型停止、完整配对轮次与多重比较控制会在 `docs/en/capability-prior.md` 持续说明。

### 健康、约束与推荐

健康数据是独立证据，包括答案完成度、自一致性、工具可靠性和已观测失败。席位可要求 capability、context 限制和健康门槛。缺少必需模态、样本不足或结论区间重叠时，应当 unassigned/indeterminate，而不是被平均分硬推为第一。

成本、延迟、时效与不确定性同样属于推荐问题。`configs/models.yaml` 提供已知价格/能力事实，实测提供行为证据；`configs/knowledge.yaml` 的参考分数只是先验，不能替代对必需实时能力的测量。

### 可复现与审计

数据库保存 sweep、run、measurement、infra incident、separation 与 calibration event。任何对外结论都应保留题库哈希、配置版本、端点/模型标识、超时与重试策略、grader 版本与随机种子；缺少这些溯源信息的报告只能算运行提示，不能算可复现实验。

## 安全规则

- 默认 `bash scripts/test.sh` 和 `--ci` 会移除继承的数据库凭证，不能静默连接环境中的生产 DSN。
- `bash scripts/test.sh --with-db` 只接受 `hr_test_*` scratch 数据库，拒绝 `wiki` 等名称。
- 运行工件默认不写入仓库；测试会将 HOME、OpenCode 配置、HR 配置、题库和输出路径隔离到临时目录。
- 测试前后会比较 `git status --porcelain`，仓库中的意外写入会导致测试失败。
- provider 密钥只能位于环境变量或本地覆盖层，不能位于被跟踪 YAML、`hr.toml`、测试夹具或报告中。

## Install

```bash
pip install .
```

源码、可编辑和 Wheel 安装均受支持；打包配置从安装目录的 `share/aihr` 解析。可执行代码基准在缺少 Bubblewrap（`bwrap`）时会安全拒绝运行；Debian/Ubuntu 可执行 `sudo apt-get install bubblewrap`。如果旧的 `hr-cli` 或 `hr-bench` 包已安装，请先卸载：

```bash
pip uninstall hr-cli hr-bench -y
pip install .
```

### Environment Variables

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `HR_DSN` | 覆盖 PostgreSQL 连接字符串（优先于 `HR_DB_PASSWORD` + `db_*` 字段） | 未设置 |
| `HR_HOME` | 强制指定备用配置根目录（configs/、`hr.toml`、`itemrepo` 均从此处解析） | 仓库根目录 |
| `HR_COMPOSE_FILE` | 覆盖 DB 密码解析所探测的 `docker compose` 清单文件路径 | 未设置 |
| `HR_ITEMREPO` | 覆盖基准题库目录 | `HR_HOME/itemrepo` |
| `HR_OUTPUT_DIR` | 覆盖运行工件的运行时输出根目录 | 平台缓存目录（见下文） |

### Output root（运行工件）

生成的工件（bench 导出、校准报告、扫描转储等）绝不落入仓库目录树。它们统一经 `hr.config.output_root()` 解析：`HR_OUTPUT_DIR` 环境变量优先，否则取平台缓存目录（`$XDG_CACHE_HOME/hr`、`~/Library/Caches/hr`、`%LOCALAPPDATA%/hr\Cache`），再否则取系统临时目录。显式指定输出路径的 CLI 标志在调用点始终优先。

### Configuration

如需 DB / Wiki.js 配置项，请复制示例 `hr.toml`：

```bash
cp configs/hr.toml.example hr.toml
```

不存在单一的"唯一事实来源"文件——配置按关注点拆分在 `configs/` 各文件中，外加运行时 opencode 配置：

### 本地覆盖层（`configs/*.local.yaml`）

被跟踪的配置文件零真实部署值（机器相关的值只以示例占位符形式存在）。真实值存放在被
gitignore 的本地覆盖层中——`configs/seats.local.yaml`、`configs/fleet.local.yaml`、
`configs/deployable.local.yaml`、`configs/models.local.yaml`——由
`hr.config.load_yaml` 自动深合并到被跟踪文件之上：**本地逐键获胜；字典递归合并；列表整体替换、绝不合并。**
缺少覆盖层是正常情况（直接使用被跟踪文件）。

首次安装：`cp configs/seats.yaml configs/seats.local.yaml`、
`cp configs/fleet.yaml configs/fleet.local.yaml`、
`cp configs/deployable.yaml configs/deployable.local.yaml`（若你的网关能力事实不同，
也请 `cp configs/models.yaml configs/models.local.yaml`），然后把真实的锚点、wire 覆盖、
网关 URL 与 `extra_deployable` 列表填入覆盖层。切勿把部署值写进被跟踪文件——
只有 `*.local.yaml` 可安全存放真实值。

按文件拆分如下：

- `configs/thresholds.yaml` — 数值扫描与门控阈值（stage0 预算、half-width、验收区间）。
- `configs/models.yaml` — 模型定价与能力覆盖层（thinking/vision），以裸模型 slug 为键；未知模型使用安全默认值。
- `configs/knowledge.yaml` — 策展参考分数与定性研究结论，以裸模型 slug 为键（未知模型自动跳过）。
- `configs/fleet.yaml` — 动态机队的可选覆盖：`wire_overrides`、`scope_excludes`、`gateway_urls`（仅注册表提供者的 base URL）。
- `configs/seats.yaml` — 席位定义、每席位 `primary_capabilities`、stage-0 `calibration_anchors`。
- `configs/deployable.yaml` — `extra_deployable`：在 opencode 配置之外提供的模型（唯一手工维护的模型列表）。
- `configs/hr.toml.example` — 根 `hr.toml` 模板（DB 连接 + 可选 Wiki.js 发布目标）。密钥绝不存于此文件：一律来自环境变量（`HR_DSN`、`HR_DB_PASSWORD`、provider 密钥）。

模型机队本身不在此仓库声明：运行时从 opencode 配置（`opencode.jsonc` 的 provider 块）推导，并与 `deployable.yaml` 的 extras 合并——见下文 Universality。

## CLI Map

十三条命令，每条针对一项具体职责。旧 v1 命令（`evaluate`、`report`、`run_all`）已淘汰。`hr verdict` 取代了退役的评估路径。

| 命令 | 用途 |
|------|------|
| `hr discover` | 从 `opencode.jsonc` 将提供者/模型枚举进 `hr`（scope + auth 存在性） |
| `hr seed` | 初始化数据库结构并写入规范座位定义 |
| `hr bench` | 运行实时能力基准测试并记录 `hr.measurement` 行 |
| `hr verdict` | 综合判定：能力均值 + 健康检查 + 门控 + 席位分配 |
| `hr health` | 全池行为健康 Markdown 表（纯 DB，零 API 调用） |
| `hr sweeps` | 列出 DB 中的扫描及其运行/模型/测量数量 |
| `hr calibrate` | Stage-0 锚点校准引擎（dry-run 规划 + 实时 API 各轮） |
| `hr reference` | 从 `configs/knowledge.yaml` 读取各模型策展基准分数 |
| `hr research` | 从同一知识库读取各模型定性研究结论 |
| `hr publish` | 报告发布至 Wiki.js（可选目标；未配置时以 exit 0 跳过） |
| `hr recommend` | 基于 `configs/seats.yaml` + 近期测量的席位推荐 |
| `hr status` | DB 状态：扫描 + 最新扫描能力均值（纯 DB） |
| `hr apply` | 将最新判定结果桥接为 FastDraw 预设 |

CLI 没有全局 `--config` 选项：配置从环境变量（见上表）以及相对 HR_HOME 的 `configs/` 解析。运行 `hr --help` 与 `hr <命令> --help` 查看各命令的完整参数列表。

## FastDraw Seam

FastDraw 是捆绑在 `fastdraw/` 的模型选择子包。它提供基于 TUI 的预设管理，用于代理模型分配，并通过 `hr apply` 与 opencode 集成。

### Subpackage Layout

```
fastdraw/
  server.ts     # FastDraw HTTP 服务器（预设 API）
  tui.ts        # 预设管理的终端 UI
  package.json  # npm 清单（独立安装）
  test/         # 测试套件
  README.md     # FastDraw 专属文档
```

### The `hr apply` Contract

`hr apply` 是裁决座位与 FastDraw 预设之间的桥梁。它分三步工作：

1. 计算最新的逐座位裁决分配
2. 将命名预设写入 `<opencode 配置目录>/fastdraw-presets.json`
3. 使用 `--set-state` 时，另写 `.fastdraw.json` 供启动时激活

### Dual-File Registration

FastDraw 包含服务端与 TUI 两部分。必须在两个 opencode 配置文件中都注册插件；只注册一个文件会静默缺失另一部分。

```jsonc
// ~/.config/opencode/opencode.jsonc
{ "plugin": ["opencode-fastdraw"] }
```

```json
// ~/.config/opencode/tui.json
{ "plugin": ["opencode-fastdraw"] }
```

前者加载 `fastdraw_*` Agent 工具，后者加载 `/fastdraw` 命令与 `<leader>m` 快捷键。

## Layout

```
harness/hr/               # 仓库根目录（pip install -e .）
  configs/                # YAML 配置：deployable.yaml、fleet.yaml、hr.toml.example、knowledge.yaml、models.yaml、seats.yaml、thresholds.yaml（+ 被 gitignore 的 *.local.yaml 覆盖层）
  docs/                   # 双语文档（en/、zh-CN/）
  exports/                # 生成的工件（gitignore）
  fastdraw/               # npm 子包：FastDraw 服务器、TUI、预设管理
  hr/                     # Python 包：CLI 和所有业务逻辑
    adapters/             # 提供者适配器（anthropic-compat、openai-compat）+ 机队路由
    bench/                # 基准测试电池 + stage0/stage1 扫描引擎
    graders/              # 评分函数（factuality、reasoning、vision、tools）
    items/                # 基准测试题目的加载器
    scheduler/            # 任务调度（按 Metis C1 保留）
    seats/                # 席位分类和档案辅助
    stats/                # 扫描结果的统计汇总
  itemrepo/               # 按类别组织的 Git 版本化基准测试题库
  scripts/                # 运维脚本（check_universal.sh、register_livebench_batteries.py、spread_probe.py、...）
  tests/                  # pytest 测试套件
  pyproject.toml          # 包含 CLI 入口点的包清单
```

## Tests

```bash
python -m pytest tests/ -q
```

所有测试均在离线、隔离的夹具与逐测试暂存工作区中运行：共享的 `hr_sandbox` 夹具（tests/conftest.py）将 `HOME`/`OPENCODE_CONFIG_DIR`/`HR_HOME`/`HR_ITEMREPO`/`HR_OUTPUT_DIR` 全部封印进 pytest 临时目录；会话级**清洁守卫**在会话开始时快照 `git status --porcelain`，若任何测试在会话结束后污染了仓库，则以失败收尾并列出违规路径。Stage0 测试覆盖能力先验流水线、层级感知阈值和判定排序器。Bench 测试覆盖发现加载器、电池解析器和 stage0 预算合约。

实时 API 基准测试需要 opencode 配置中的真实 provider 凭证：

```bash
hr bench --model gpt-4o --battery reasoning
```

## Universality

本代码库针对自主 LLM 编码代理这个通用类别，而非特定产品。席位分类（tier 1 至 tier 4）、基准测试题目类别（factuality、reasoning、vision、tool_a、tool_b）、判定流水线（discover、bench、assign、verdict）适用于任何消费 LLM 输出并产出代码工件的代理。

统一过程中已移除提供者特定的硬编码。模型机队在运行时从 opencode 的实时配置推导（`opencode.jsonc` 的 provider 块：每个 `provider.*.models` 条目即一个机队模型，`npm` 字段推导 wire 类型）；`configs/fleet.yaml` 只保留可选覆盖（`wire_overrides` 用于仅注册表提供的模型、`scope_excludes`、`gateway_urls`），`configs/deployable.yaml` 的 `extra_deployable` 是唯一手工维护的模型列表（在 opencode 配置之外提供的模型）。在 opencode 配置中新增模型即自动进入扫描池、discover 与路由，此处零改动。知识数据存于 `configs/models.yaml`（定价/能力）与 `configs/knowledge.yaml`（参考分数、研究结论），未知模型均有安全默认值。

## License

见 `LICENSE`。
