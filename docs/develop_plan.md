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

### 验收

- 用一个真实小项目(如 STM32 最小系统)按文档流程从头到尾走通,所有工具调用序列无需人工修正;
- LLM 在空项目场景下能自行调用第 1 步索引同步,而不是直接开始画图。

---

## 附录:可能一并处理的小问题

- **相对 URI 解析**:symbol 与 footprint 两侧对库表中的相对 URI 都按服务器 CWD 解析,不相对表文件目录。KiCad 允许相对 URI,遇到即解析错误。建议两侧统一为"相对表文件所在目录解析"(footprint 侧 `_resolve_uri` 与 symbol 侧 `_expand_env_vars` 一起改)。
- **符号侧缺失兜底**:全局 `sym-lib-table` 不存在时 `get_libraries()` 抛 `FileNotFoundError`,不如 footprint 侧 live-scan。可加降级提示或候选目录扫描(需用户确认是否要,遵循"无默认降级路径"原则)。