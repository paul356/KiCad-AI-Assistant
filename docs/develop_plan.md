# Develop Plan — 潜在问题与优化

> 状态:草案。本文记录 kcaa(kicad-mcp 核心)当前实现中发现的潜在问题与后续优化方向,供开发排期参考。
> 每个条目包含:现状(代码引用)、问题、方案、风险/决策点、验收标准。

---

## 0. 现状速览(问题背景)

| 能力 | Symbol 索引 | Footprint 索引 |
|---|---|---|
| 全局库表 | ✅ 只读 `~/.config/kicad/<ver>/sym-lib-table` | ✅ 全局 `fp-lib-table`(候选目录 `kicad/{10.0,9.0,8.0,7.0}`) |
| 项目级库表 | ❌ 不读取 | ✅ `sync_footprint_index(project_path)` |
| `${KIPRJMOD}` 展开 | ❌ | ✅ |
| 库表缺失兜底 | ❌ 抛 `FileNotFoundError` | ✅ live-scan 兜底 |

关键代码:
- 符号侧:`kcaa/utils/symbol_index_reader.py`(`SymbolIndexReader._parse_table` 只读 `config.symbol_table_file`)、`kcaa/tools/symbol_tools.py`(`sync_symbol_index` 只有 `force` 参数,无 `project_path`)、`kcaa/utils/config.py`(`symbol_table_file` → 全局配置目录)。
- 封装侧:`kcaa/utils/pcb_library_utils.py`(`find_fp_lib_tables` / `parse_fp_lib_table` / `build_effective_library_list`,项目表优先、按 nickname 去重、`${KIPRJMOD}` 支持)、`kcaa/tools/pcb_library_tools.py`(`sync_footprint_index(project_path)`)、`kcaa/utils/footprint_index_manager.py`(单例缓存 `project_path`)。

→ 符号索引是主要缺口,封装侧已具备可复用的模式。

---

## 1. 增加对项目级库表的支持(Symbol 侧)

### 问题

- `sync_symbol_index` 只索引全局 `sym-lib-table`。项目目录内的个人 symbol 库(项目级 `sym-lib-table`,常见于团队/单项目库)对 AI 完全不可见,无法 `search_symbols` / `get_symbol`。
- 与 footprint 侧能力不对称:封装已支持 `project_path`,符号没有。
- 项目级表常使用 `${KIPRJMOD}` 定位库文件,当前 `SymbolIndexReader._expand_env_vars` 只展开 `get_env_vars()` 返回的 `KICAD{ver}_*`,不含 `KIPRJMOD`。

### 方案(对齐 footprint 侧实现)

1. `SymbolIndexReader` 增加 `project_path: str | None` 构造参数:
   - `project_path` 给出时,先解析 `<project_dir>/sym-lib-table`,再解析全局表;
   - 按 **nickname 去重,项目优先**(与 `build_effective_library_list` 语义一致:首次出现获胜);
   - 环境变量展开时并入 `KIPRJMOD=<project_dir>`(有项目路径时)。
2. `SymbolIndexManager.sync` 无需改动——它已按条目逐库索引;`lib_name` 冲突由读取层去重解决。
3. 工具层:`sync_symbol_index` 增加可选参数 `project_path: str | None = None`,透传给后台线程与单例(`symbol_tools._get_index_manager` 需按 footprint 侧 `get_footprint_index_manager` 的单例模式改造)。
4. 兼容性:不传 `project_path` 时行为与现状完全一致(只读全局表)。

### 风险 / 决策点

- **nickname 冲突语义**:项目表覆盖全局表(推荐,与 KiCad 行为、footprint 侧一致)vs 两者都索引。需在文档中明确。
- DB 里 symbol 库按**文件路径**为 key(`db_known`/`current_paths`),若项目表与全局表指向同一文件,会出现重复条目——需在读取层按 realpath 去重。
- 项目表缺失时行为:直接跳过(不要抛错),避免旧项目无表导致 sync 失败。

### 验收

- 单测(fixture 构造项目级 `sym-lib-table`):项目表条目被索引;同名 nickname 项目优先;`${KIPRJMOD}` 正确展开;无 `project_path` 时行为不变。
- 集成:`sync_symbol_index(project_path=...)` 后 `search_symbols` 可命中项目库符号。

---

## 2. 考虑增加全局网表 / footprint 描述文件(项目状态 digest)

### 问题

- LLM 冷启动理解一个项目需要多次工具往返:netlist 提取 → BOM → symbol 查询 → PCB 查询,上下文碎片化、成本高,且多次独立解析结果可能不一致。
- 没有"项目全貌"的单点入口;新会话(或会话恢复,见 plugin 会话存档)无法快速获得项目快照。

### 方案(考虑项)

生成项目级"描述文件"(digest),由统一的解析代码一次性产出,LLM 一次读取获得项目状态:

- **内容**:
  - 网表摘要:组件清单(ref / value / library_id / footprint)、net 列表(含成员引脚)、层级 sheet 树;
  - Footprint 摘要:板上 footprint(ref → 库/名称/位置/层/pad 数/3D 模型)、已用封装库列表;
  - 元信息:生成时间、**各源文件 mtime**(`.kicad_sch` / `.kicad_pcb` / 两个 lib-table),用于新鲜度判断。
- **载体**:JSON + 可选 Markdown 概览,放在项目 `.kicad_prl/` 目录(已被 `.gitignore` 覆盖,不污染版本库)。
- **入口**:新增工具 `generate_project_digest(project_path, force=False)`;并可暴露为资源 `kicad://digest/<project_path>` 供会话上下文直接加载。
- **复用**:基于现有 `netlist_parser.py`、`pcb_query_tools` / `pcb_footprint_utils` 的解析结果,不新写格式解析。

### 收益

- 冷启动:会话加载 digest 一次即获全貌,工具往返大幅减少。
- 一致性:digest 与 tool 查询共用同一解析代码。

### 可选建议(评审后采纳;可作为上述文件方案的替代或简化)

- **优先做成 `kicad://digest/{project_path}` 资源,而非"文件 + 生成工具"**。服务端已有资源层(`kcaa/resources/` 下 8 个 `kicad://` 资源:netlist / project_netlist / bom / drc / patterns / project / schematic / component)。新增 `digest_resources.py`,照抄 `netlist_resources.py` 的 `@mcp.resource` 写法,内部组合 `extract_project_netlist` + `get_board_info` + 库统计即可,约 100~150 行。理由:
  - 无过期问题:每次请求现算,永远是当前状态;文件方案必然引入"AI 读到旧 digest"的陈旧风险。
  - 无落盘/清理负担,不依赖 `.kicad_prl`。
  - 与现有 UX 一致(AI 已习惯读 `kicad://netlist` 等)。
- **无状态优先,不落盘**:普通项目解析为毫秒级;若超大型板解析变慢,再退化"以源文件 mtime 为 key"的内存缓存,不写文件。
- **摘要优先、明细按需**:digest 只放概览(组件数/值/footprint 分布、net 数、sheet 树、已用库、DRC 状态),不带全量 netlist(那是 `kicad://netlist` 的职责)。Markdown 格式与现有资源一致;schema 加 `digest_v1` 版本字段;字段定稿前先采样 2~3 个真实项目。
- **真正的价值在插件侧自动加载**:`kicad_plugin/context_bridge.py` 已有 `active_project`,检测到项目打开/切换时自动拉取 digest 注入对话上下文(先 opt-in,有 token 成本)。resource 只是地基,自动加载才是功能;这步不做,资源本身仍可用,但要 AI 记得主动读。

**背景:resource 解决什么问题**(供评审时参考)

| | tool | resource | 文件 |
|---|---|---|---|
| 本质 | 动作(计算/修改) | 数据(只读内容) | 数据(落盘) |
| AI 获取 | 记得要调、传参 | 按地址读 | 先生成再读 |
| 客户端可自动加载 | 不能 | 可以 | 需插件自行解析 |
| 陈旧风险 | 无 | 无(现算) | 有 |

Resource 的核心价值:让 AI **按需、无副作用、可被客户端自动加载**地获取上下文——读数据不占"动作"位,宿主(插件)可在"项目打开"事件里自动注入,这是 tool 和文件都给不了的。

### 风险 / 决策点

- **陈旧 digest 误导 AI**:必须携带源文件 mtime 并在文档中提示"文件变更后需重新生成";默认拒绝加载过期 digest(或强制时明确标注 stale)。
- **token 预算**:全量 netlist 可能过大。建议 digest = 概要 + 定位信息,明细仍按需 tool 查询(分级读取)。
- **范围**:哪些字段是 LLM 推理必需、哪些是噪音(如 3D 模型路径),需要一次实际项目采样后收敛。
- 生成触发:手动工具调用 vs 写入时自动刷新 vs 会话开始时按需生成——建议先做手动 + 会话内提示。

### 验收

- 对真实/构造项目:`generate_project_digest` 产出文件字段与 `extract_project_netlist` + PCB 查询结果一致;
- 修改源码文件后 digest 被标记 stale;重新生成后刷新。

---

## 3. 增加典型设计流程,帮助 LLM 如何开始设计

### 问题

- 工具面广(符号/封装/网表/布线/DRC/BOM/模式识别),但缺少"从 0 开始一个设计"的编排指引。LLM 接到空项目或新需求时,不知道正确的调用顺序与检查点,容易跳过索引同步、先布后检等关键步骤。

### 方案(考虑项)

新增面向 LLM 的流程指南(放在 `docs/`,后续可视情况做成 skill 或注入 prompt):

1. **初始化**:定位/创建 `.kicad_pro` → `sync_symbol_index` + `sync_footprint_index(project_path)` → 校验索引状态(`get_symbol_sync_status` / `get_footprint_sync_status`)。
2. **需求澄清**:解析用户需求 → 拆分成模块/信号域 → 确定主要器件约束(电压/电流/封装尺寸)。
3. **器件选型**:`search_symbols` → `get_symbol`(引脚/方向)→ `search_footprints`(封装约束)→ 按 `default_footprints` 建议兜底。
4. **原理图搭建**:建 sheet(`create_child_sheet`)→ 放置符号 → 连线/标签(`add_wire_to_schematic`、标签工具)→ 电源符号。
5. **校验**:`extract_project_netlist`(连接性)→ `analyze_bom`(器件齐备)→ `identify_patterns`(电源/MCU 等常见模块自检)。
6. **PCB**:更新 footprint → 布局 → `pcb_route_pad_to_pad` / 自动布线(带 netclass 约束)→ `run_drc_check` → 修错迭代(DRC 0 错误为完成标准)。
7. **输出**:BOM CSV / Gerber 导出。

每个阶段写明:**触发条件、工具调用序列、验收标准**(如"DRC 0 错误""netlist 无悬空引脚"),并给出失败分支(索引为空怎么办、库找不到怎么办、DRC 报错先查哪)。

### 落地方式(决策点)

- 起步:静态 Markdown 文档,放 `docs/`,在系统提示词中引用;
- 进阶:做成 skill(参考 `kicad_plugin/skills/` 与 `kcaa/prompts/` 现有结构),按项目类型(电源/MCU/传感器板)提供不同流程模板;
- 避免过度工程:先交付 1 份通用流程 + 2~3 个典型模板,验证效果再扩展。

### 可选建议(评审后采纳)

- **做成 skill,而非纯静态文档**:新增 `kicad_plugin/skills/design_workflow.md`(front-matter 带 `name` / `description` / `priority`),`llm_client.py` 的 `build_system_prompt()` 会把所有 `skills/*.md` 自动编入 `# Skills` 目录并注入系统提示词,LLM 可随时按需取用;同时放一份到 `docs/` 供人阅读。
- **流程每一步必须映射到已注册的真实工具名**:已核对 `kcaa/tools/` 全部工具,链条齐全(sync 索引 → search/get 选型 → sheet/wire 建图 → netlist/bom/pattern 校验 → pcb 布局布线 → drc → 导出),但发现一个缺口:**没有 Gerber 导出工具**(仅 `export_bom_*` 和 kicad-cli 缩略图)。第 7 步需二选一:在 `kcaa/tools/export_tools.py` 补一个 Gerber 导出(可复用 `kcaa/utils/kicad_cli.py`),或从流程中删掉该步。
- **流程格式:前置条件 + 显式失败分支 + 验收标准**,而非叙事。例:"索引完成才能 search""`update_pcb_from_schematic` 要求原理图已保存""DRC 0 错误才能继续导出,DRC>0 则报告并停"。与"无默认降级路径"原则一致,失败必须可见。
- **先做一条通用流程,不做板型矩阵**;通用版被实证薄弱后再扩展电源/MCU/传感器模板。

### 验收

- 用一个真实小项目(如 STM32 最小系统)按文档流程从头到尾走通,所有工具调用序列无需人工修正;
- LLM 在空项目场景下能自行调用第 1 步索引同步,而不是直接开始画图。

---

## 4. 实施顺序与依赖(可选建议)

- **T3 依赖 T1**:工作流第 1 步(索引同步)对项目级符号库目前不生效——T1 不先做,工作流文档会教 LLM 走错路径(同步后搜不到项目库符号)。
- 建议顺序:**T1(小、正确性缺口)→ T3(便宜、立即见效)→ T2(最大,且全价值依赖插件侧自动加载集成)**。
- T3 的冷启动步骤可先引用现有 `kicad://project_netlist` / `kicad://bom` 资源,不必等 T2;T2 与 T3 可并行开发。

---

## 附录:可能一并处理的小问题

- **相对 URI 解析**:symbol 与 footprint 两侧对库表中的相对 URI 都按服务器 CWD 解析,不相对表文件目录。KiCad 允许相对 URI,遇到即解析错误。建议两侧统一为"相对表文件所在目录解析"(footprint 侧 `_resolve_uri` 与 symbol 侧 `_expand_env_vars` 一起改)。
- **符号侧缺失兜底**:全局 `sym-lib-table` 不存在时 `get_libraries()` 抛 `FileNotFoundError`,不如 footprint 侧 live-scan。可加降级提示或候选目录扫描(需用户确认是否要,遵循"无默认降级路径"原则)。