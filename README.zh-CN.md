# HR: Agent Seat Matching and Capability Benchmarking

统一工具链，用于将自主 LLM 编码代理匹配至任务适当的席位、跨模型集群执行能力基准测试、并发布部署判定。单一可编辑安装。13 条 CLI 命令。CLI 本身运行时不需要 API 密钥。

本文档（README.zh-CN.md）是英文规范版本的忠实镜像。当两者出现差异时，以英文版本为准。

## Install

```bash
pip install -e .
```

可编辑安装是唯一支持的模式。Wheel 或二进制安装会破坏依赖路径的配置解析。如果旧的 `hr-cli` 或 `hr-bench` 包已安装，请先卸载：

```bash
pip uninstall hr-cli hr-bench -y
pip install -e .
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
cp configs/hr.toml.example hr/hr.toml
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
- `configs/knowledge.yaml` — 策展参考分数与定性研究结论，以裸模型 slug 为键（未知模型自动跳过；用 `hr reference --seed` / `hr research --seed` 入库）。
- `configs/fleet.yaml` — 动态机队的可选覆盖：`wire_overrides`、`scope_excludes`、`gateway_urls`（仅注册表提供者的 base URL）。
- `configs/seats.yaml` — 席位定义、每席位 `primary_capabilities`、stage-0 `calibration_anchors`。
- `configs/deployable.yaml` — `extra_deployable`：在 opencode 配置之外提供的模型（唯一手工维护的模型列表）。
- `configs/hr.toml.example` — 根 `hr.toml` 模板（DB 连接 + 可选 Wiki.js 发布目标）。密钥绝不存于此文件：一律来自环境变量（`HR_DSN`、`HR_DB_PASSWORD`、provider 密钥）。

模型机队本身不在此仓库声明：运行时从 opencode 配置（`opencode.jsonc` 的 provider 块）推导，并与 `deployable.yaml` 的 extras 合并——见下文 Universality。

## CLI Map

二十三条命令，每条针对一项具体职责。旧 v1 命令（`evaluate`、`report`、`run_all`）已淘汰。`hr verdict` 取代了退役的评估路径。

| 命令 | 用途 |
|------|------|
| `hr discover` | 从 `opencode.jsonc` 将提供者/模型枚举进 hr2（scope + auth 存在性） |
| `hr seed` | 将研究结论与参考分数入库（v1 遗留路径） |
| `hr bench` | 运行 10 个实时能力基准测试并记录 `hr2.measurement` 行 |
| `hr verdict` | 综合判定：能力均值 + 健康检查 + 门控 + 席位分配 |
| `hr health` | 全池行为健康 Markdown 表（纯 DB，零 API 调用） |
| `hr sweeps` | 列出 DB 中的扫描及其运行/模型/测量数量 |
| `hr calibrate` | Stage-0 锚点校准引擎（dry-run 规划 + 实时 API 各轮） |
| `hr reference` | 各模型策展已发布基准分数（`--seed` 写入 `hr_reference`） |
| `hr research` | 各模型定性研究结论（`--seed` 写入 `hr_research`） |
| `hr publish` | 报告发布至 Wiki.js（可选目标；未配置时以 exit 0 跳过） |
| `hr recommend` | 基于 `configs/seats.yaml` + 近期测量的席位推荐 |
| `hr status` | DB 状态：扫描 + 最新扫描能力均值（纯 DB） |
| `hr apply` | 将最新判定结果桥接为 FastDraw 预设 |
| `hr apply-preview` | 写入前预览席位分配预设（dry run） |
| `hr apply-rollback` | 回滚上一个生效的 FastDraw 预设 |
| `hr apply-backups` | 列出为回滚保留的 FastDraw 预设备份 |
| `hr apply-prune` | 清理过期的 FastDraw 预设备份 |
| `hr release-build` | 构建发布候选（清单表面 + configs + itemrepo） |
| `hr release-verify` | 校验候选的哈希链（篡改即删除） |
| `hr release-activate` | 激活发布：原子切换 + 备份 + 插件注册（幂等） |
| `hr release-rollback` | 回滚到上一个发布（符号链接与配置逐字节还原） |
| `hr release-list` | 列出发布候选及校验标记 |
| `hr release-prune` | 按保留策略清理过期候选 |

CLI 没有全局 `--config` 选项：配置从环境变量（见上表）以及相对 HR_HOME 的 `configs/` 解析。运行 `hr --help` 与 `hr <命令> --help` 查看各命令的完整参数列表。

### 发布生命周期（+ Apply）

`hr apply` 及其 `apply-*` 辅助命令将最新判定结果桥接为 FastDraw 预设：
先 `apply-preview` 预览再应用；`apply-rollback`/`apply-backups`/`apply-prune`
管理备份链。`release-*` 命令运行发布生命周期，交付本仓库的制品：
`release-build` 依据已发布 CLI 的运行时导入闭包在 releases 根下组装候选
（权威构建清单见 `hr/release_manifest.py`），`release-verify` 校验候选的
哈希链并在篡改时将其移除，`release-activate` 原子切换运行时符号链接并注册
发布插件（幂等；上一状态被保留以供 `release-rollback` 还原），
`release-list`/`release-prune` 管理候选。

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

`hr apply` 是 FastDraw 预设与 opencode 运行时之间的桥梁。它分三步工作：

1. 从 `~/.config/opencode/fastdraw/presets.json` 读取 FastDraw 预设
2. 将模型分配写入 opencode 注册的配置文件（见下文）
3. 打印重启提示，让 opencode 加载新的分配

### Dual-File Registration

两个 opencode 文件必须将 FastDraw 指向正确的模型目录路径。FastDraw 服务器从这两个位置读取：

| 文件 | 键 | 值 |
|------|----|----|
| `.opencode/opencode.jsonc` | `models.catalog` | `../configs/models.yaml` |
| `.opencode/tui.json` | `models.catalog` | `../configs/models.yaml` |

两者都解析到 hr 目录树中的同一个 `configs/models.yaml`。这种双重注册是有意为之，必须保持一致。

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

所有测试均在离线、隔离的夹具与逐测试暂存工作区中运行：共享的 `hr_sandbox` 夹具（tests/conftest.py）将 `HOME`/`OPENCODE_CONFIG_DIR`/`HR_HOME`/`HR_ITEMREPO`/`HR_OUTPUT_DIR` 全部封印进 pytest 临时目录；会话级**清洁守卫**在会话开始时快照 `git status --porcelain`，若任何测试在会话结束后污染了仓库，则以失败收尾并列出违规路径。当前套件为 **577 通过 / 9 跳过 / 0 失败**（共 586 项收集）。Stage0 测试覆盖能力先验流水线、层级感知阈值和判定排序器。Bench 测试覆盖发现加载器、电池解析器和 stage0 预算合约。

实时 API 基准测试需要 opencode 配置中的真实 provider 凭证：

```bash
hr bench --model gpt-4o --battery reasoning
```

## Universality

本代码库针对自主 LLM 编码代理这个通用类别，而非特定产品。席位分类（tier 1 至 tier 4）、基准测试题目类别（factuality、reasoning、vision、tool_a、tool_b）、判定流水线（discover、bench、assign、verdict）适用于任何消费 LLM 输出并产出代码工件的代理。

统一过程中已移除提供者特定的硬编码。模型机队在运行时从 opencode 的实时配置推导（`opencode.jsonc` 的 provider 块：每个 `provider.*.models` 条目即一个机队模型，`npm` 字段推导 wire 类型）；`configs/fleet.yaml` 只保留可选覆盖（`wire_overrides` 用于仅注册表提供的模型、`scope_excludes`、`gateway_urls`），`configs/deployable.yaml` 的 `extra_deployable` 是唯一手工维护的模型列表（在 opencode 配置之外提供的模型）。在 opencode 配置中新增模型即自动进入扫描池、discover 与路由，此处零改动。知识数据存于 `configs/models.yaml`（定价/能力）与 `configs/knowledge.yaml`（参考分数、研究结论），未知模型均有安全默认值。

## 安全说明

发布完整性由自证式 SHA-256 记录保护：每个发布的 `metadata.json` 对自身载荷计算哈希，验证时在本地重新计算并比对。发布与备份**没有密码学签名**——能修改发布目录的本地攻击者可重算全部哈希使其匹配；控制发布的攻击者可将随附的插件路径注册进 opencode 配置，并在下次加载配置时执行代码。

因此信任模型为**同用户本地信任**：操作员与进程运行于同一账户。交付边界将所有调用方提供的名称与路径——发布名、备份名、清单文件键、激活账本中的符号链接目标——限制在发布目录与配置目录之内；越界或外来路径一律拒绝并给出明确错误，绝不跟随。无特权的远程用户无法触达这些表面。

完整的供应链保证（密钥签名的发布、由仓库之外的密钥验证）已列为后续工作，不属于本次发布范围。

## License

见 `LICENSE`。