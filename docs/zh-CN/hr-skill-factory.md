# HR 技能工厂（`hr skill`）

**[English](../en/hr-skill-factory.md)** | 简体中文

技能工厂负责创建、校验、安装、巡检本仓库随附的 OpenCode 技能。整个命令族由一条规则驱动：
**每个技能的权威副本就是本仓库 `opencode-config/skills/` 下的那份文件；任何机器上的其余副本一律是派生副本。**
`hr skill doctor` 度量权威与各副本之间的漂移；其余位置永远不会被当作来源。

## 为什么需要它（它消灭的缺陷）

引擎只有在技能 frontmatter 携带非空 `description` 时才会把技能广告给模型
（[官方规则](https://opencode.ai/docs/skills)）。随产品发布的 `hr-workflow` 技能长期没有
frontmatter：安装没问题、手工加载也没问题，但对模型完全不可见——而且没有任何工具会察觉，
因为安装方式是 README 里手抄的 `cp`，也不存在校验器。这一个技能的三份副本（仓库、`~/.config`、
第二个代理工具）就这样无人察觉地各自漂移。`hr skill` 把两个洞都堵上：fail-closed 校验器 +
唯一权威与漂移度量。

## 布局

```text
opencode-config/
  skills/
    <skill-id>/
      SKILL.md        # 权威文件；frontmatter：name + description
  agents/
    hr.md             # hr 子代理（用复制安装）
```

V2 目录布局（`<skill-id>/SKILL.md`）是 OpenCode 与 Codex 的共同目标布局，
技能在两个工具之间搬运就是纯文件复制，不需要改写。旧的扁平 `<skill-id>.md`
仍会被**读取**（validate / doctor 认识它，install 会把它归一化为目录形态），但永远不会被写出。

技能 ID 由路径推导（`skills/foo/SKILL.md` → ID `foo`）；frontmatter 的 `name` 只是显示标签，
且必须等于该 ID。

## 命令

### `hr skill validate [PATH | --all]`

fail-closed 的 frontmatter 闸。任何缺陷 → 退出码 1，点名文件与缺项。绝不允许"警告后放行"。检查项：

| 检查项 | 规则 |
|---|---|
| frontmatter 块 | 存在、正确终止、合法 YAML 映射 |
| `name` | 必填；`^[a-z0-9]+(-[a-z0-9]+)*$`，1–64 字符，**且等于路径推导的 ID** |
| `description` | 必填且非空——缺了它引擎永不广告该技能 |
| `disable-model-invocation` | 可选；出现时必须是布尔值 |

```bash
hr skill validate opencode-config/skills/hr-workflow   # 单个技能（目录或 SKILL.md 均可）
hr skill validate --all                                # 整个权威面
```

### `hr skill list [--json]`

列出权威面技能，并逐项报告本机引擎是否将其广告给模型。引擎面数据来自

```bash
opencode api get "/api/skill?location%5Bdirectory%5D=<skills-dir>"
```

并按实测处理了 v2.2.7 的三个坑（见下文"引擎探测"）。引擎不可达时，本地事实照常列出，
引擎列如实标注 `UNREACHABLE`——绝不静默留白。退出码恒为 0（列出不定义失败语义）；
`--json` 输出严格可解析的 JSON。

### `hr skill new NAME [--dir DIR]`

在 `<root>/NAME/SKILL.md` 生成骨架（默认根目录 = 权威面）。骨架自带合格 frontmatter，
出生即通过 `validate`。非法或重名 ID 一律拒绝，且不留任何写入。

### `hr skill install NAME [--dest DIR] [--dry-run] [--force]`

把一个权威技能复制到目标技能目录（默认：opencode 配置目录下的 `skills/`，
即 `~/.config/opencode/skills`）。执行顺序，每一步都承担语义：

1. **先校验。** 不合格技能带缺陷清单被拒绝；安装一个引擎不会广告的技能毫无意义。
2. **打印哈希。** 先给来源 `sha256`，再给目标现状。
3. **幂等。** 对内容一致的目标重复执行：零写入、退出码 0。
4. **分叉即失败。** 目标与权威不一致时报错；`--force` 才替换
   （并把旧扁平副本从目标命名空间中归一化移除）。
5. **`--dry-run` 永不写入**，并如实报告真实运行将得出的结论（包括"将会失败"）。

### `hr skill doctor [--copy DIR ...] [--json]`

对权威与所有副本根（默认安装目录 + `HR_SKILL_COPIES` + 显式 `--copy`）做哈希对照表。
每个技能给出：`in sync`、`not installed`、`DRIFT`，或 `ORPHAN`（副本的 ID 在权威面不存在——
大声报告，但装在我们旁边的第三方技能不是我们的漂移）。**任何漂移 → 退出码 1。**
比对以整个目录为单位（逐文件 SHA-256），附属文件被改动同样算漂移。

## 引擎探测（opencode v2.2.7 实测）

探测代码按以下三条实测事实处理；任何一条不再成立都会大声失败：

1. 返回体外层键是 `data`，不是 `skills`。
2. 查询参数必须保持 URL 编码：`location%5Bdirectory%5D=`。
3. 对从未打开过的目录，第一次查询合法地返回空表。探测会重试（3 次、间隔 1 秒）；
   重试预算用尽后的空答案报告为"没有"，绝不报告为"不可达"。

## 环境变量接口

| 变量 | 作用 |
|---|---|
| `HR_SKILL_SOURCE` | 覆盖权威根目录（测试、实验） |
| `HR_SKILL_COPIES` | 为 `doctor` 注册额外副本根（`os.pathsep` 分隔） |
| `HR_HOME` | 标准资源解析；权威面 = `<HR_HOME>/opencode-config/skills` |

## 生命周期对称

`hr setup --uninstall` 会撤销两种布局的技能安装：`hr skill install` 装出的
`<skills>/hr-workflow/` 目录，以及迁移前的扁平 `skills/hr-workflow.md` 残留；外加
`agents/hr.md` 副本。

## 面向脚本的机器面

`hr skill list --json` 与 `hr skill doctor --json` 向 stdout 写严格 JSON
（doctor 报告含逐文件哈希表）。人读表格对每个技能目录使用 12 位十六进制的合并摘要哈希。
