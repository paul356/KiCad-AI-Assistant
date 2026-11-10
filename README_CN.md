# KiCad AI Assistant

KiCad AI Assistant 是一个 KiCad 动作插件，在 KiCad 内部直接嵌入了由大语言模型（LLM）驱动的聊天面板。插件内置 [MCP](https://modelcontextprotocol.io/) 服务器，并暴露了丰富的工具集，让 LLM 能够通过自然语言对话读取和编辑原理图与 PCB。

已在 **KiCad 10.0 / Linux & Windows** 上验证。

[![CI](https://github.com/paul356/KiCad-AI-Assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/paul356/KiCad-AI-Assistant/actions/workflows/ci.yml)

**欢迎贡献！** Bug 报告、功能建议和 Pull Request 都非常欢迎——参见[贡献指南](#贡献指南)了解如何开始。

## 目录

- [KiCad AI Assistant](#kicad-ai-assistant)
  - [目录](#目录)
  - [前置条件](#前置条件)
  - [安装](#安装)
    - [1. 克隆仓库（可选）](#1-克隆仓库可选)
    - [2. 安装插件](#2-安装插件)
    - [3. 创建插件虚拟环境](#3-创建插件虚拟环境)
    - [4. 在 KiCad 中加载插件](#4-在-kicad-中加载插件)
  - [插件配置](#插件配置)
  - [独立 MCP 服务器](#独立-mcp-服务器)
  - [功能概览](#功能概览)
  - [工具列表](#工具列表)
    - [项目工具](#项目工具)
    - [符号库](#符号库)
    - [原理图编辑](#原理图编辑)
    - [原理图分组](#原理图分组)
    - [原理图分析](#原理图分析)
    - [层次化图纸](#层次化图纸)
    - [PCB 封装库](#pcb-封装库)
    - [PCB 查询](#pcb-查询)
    - [PCB 编辑](#pcb-编辑)
    - [PCB 布线](#pcb-布线)
    - [PCB 摆放](#pcb-摆放)
    - [PCB 分组](#pcb-分组)
    - [PCB 区域](#pcb-区域)
    - [DRC 与设计规则](#drc-与设计规则)
    - [版本管理与导出](#版本管理与导出)
    - [技能系统](#技能系统)
    - [KiCad IPC](#kicad-ipc)
  - [项目结构](#项目结构)
  - [常见问题](#常见问题)
  - [贡献指南](#贡献指南)
  - [许可证](#许可证)

## 前置条件

- KiCad 10.0 或更高版本
- [`uv`](https://github.com/astral-sh/uv) — 管理 Python 虚拟环境并自动安装所需 Python 版本
  - Linux/macOS：`curl -Lsf https://astral.sh/uv/install.sh | sh`
  - Windows：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- OpenAI、Anthropic 或兼容 LLM 提供商的 API Key（Ollama 不需要）

## 安装

### 1. 克隆仓库（可选）

仅当你需要从源码构建插件或参与项目开发时才需要此步骤。如果从 Releases 页面下载预构建的插件，可跳过此步骤。

```bash
git clone https://github.com/paul356/KiCad-AI-Assistant.git
cd KiCad-AI-Assistant
```

### 2. 安装插件

发布页提供两个压缩包：

- `kicad_ai_assistant.zip` — 用于手动安装和面板内更新器。
- `kicad_ai_assistant_pcm.zip` — 用于 KiCad 的插件与内容管理器（Plugin and Content Manager，"Install from File…"）。

两者安装的是同一个插件目录，只是目标位置不同。下文的 `10.0` 是你的 KiCad `<major>.<minor>` 版本。设置 `KICAD_DOCUMENTS_HOME` 可在所有系统上覆盖文档目录。

| 系统 | 手动安装：`scripting/plugins` | PCM 安装：`3rdparty/plugins` |
|------|-------------------------------|------------------------------|
| Linux | `~/.local/share/kicad/10.0/scripting/plugins` | `~/.local/share/kicad/10.0/3rdparty/plugins` |
| macOS | `~/Documents/KiCad/10.0/scripting/plugins` | `~/Documents/KiCad/10.0/3rdparty/plugins` |
| Windows | `%USERPROFILE%\Documents\KiCad\10.0\scripting\plugins` | `%USERPROFILE%\Documents\KiCad\10.0\3rdparty\plugins` |

从 [Releases 页面](https://github.com/paul356/KiCad-AI-Assistant/releases) 下载 `kicad_ai_assistant.zip`，解压到上表对应的手动安装目录：

**Linux:**
```bash
KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip kicad_ai_assistant.zip -d "$KICAD_PLUGIN_DIR"
```

**macOS:**
```bash
KICAD_PLUGIN_DIR=~/Documents/KiCad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip kicad_ai_assistant.zip -d "$KICAD_PLUGIN_DIR"
```

**Windows (PowerShell):**
```powershell
$KICAD_PLUGIN_DIR = "$env:USERPROFILE\Documents\KiCad\10.0\scripting\plugins"
New-Item -ItemType Directory -Force -Path $KICAD_PLUGIN_DIR
Expand-Archive -Path kicad_ai_assistant.zip -DestinationPath $KICAD_PLUGIN_DIR
```

压缩包已经包含 `kicad_ai_assistant/` 根目录，**必须保持这个名称**：重命名后 KiCad 将看不到插件。不要把这个压缩包手工解压到 `3rdparty/plugins`——该目录由插件与内容管理器管理，手工复制的文件夹不算 PCM 安装。

**插件与内容管理器（PCM）：** 打开 **Preferences → Plugin and Content Manager → Install from File…**，选择 `kicad_ai_assistant_pcm.zip`。KiCad 会把它解压到 `<3rdparty>/plugins/com_github_paul356_kicad-ai-assistant/`。不要自行解压这个压缩包。

或从源码构建：

```bash
# 在仓库根目录执行：
make dist-plugin          # 生成 dist/kicad_ai_assistant.zip（手动安装 + 更新器）
make dist-pcm             # 生成 dist/kicad_ai_assistant_pcm.zip（PCM 安装）

# Linux:
KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip dist/kicad_ai_assistant.zip -d "$KICAD_PLUGIN_DIR"

# macOS:
KICAD_PLUGIN_DIR=~/Documents/KiCad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip dist/kicad_ai_assistant.zip -d "$KICAD_PLUGIN_DIR"
```

### 3. 创建插件虚拟环境

在已安装的插件目录中运行安装脚本，创建 `.venv`、从 PyPI 安装 `kcaa`、安装 **PyMuPDF**（以 `fitz` 导入，用于 PDF 导入），并下载 freerouting JAR。此步骤需要 `uv`；缺少时脚本会打印错误并以非零状态退出，请按 <https://docs.astral.sh/uv/getting-started/installation/> 或[前置条件](#前置条件)中的命令安装。`uv` 会自动安装所需的 Python 版本。

**Linux:**
```bash
cd ~/.local/share/kicad/10.0/scripting/plugins/kicad_ai_assistant
./setup_plugin.sh
```

**macOS:**
```bash
cd ~/Documents/KiCad/10.0/scripting/plugins/kicad_ai_assistant
./setup_plugin.sh
```

**Windows (PowerShell):**
```powershell
cd "$env:USERPROFILE\Documents\KiCad\10.0\scripting\plugins\kicad_ai_assistant"
.\setup_plugin.ps1
```

**Windows (命令提示符):**
```cmd
cd /d "%USERPROFILE%\Documents\KiCad\10.0\scripting\plugins\kicad_ai_assistant"
setup_plugin.bat
```

PCM 安装时，请在 `...\3rdparty\plugins\com_github_paul356_kicad-ai-assistant` 中运行同一个脚本。脚本会从插件目录路径自动检测 KiCad 版本。KiCad 已提供所有必要的环境变量（`KICAD_VERSION`、`KICAD*_DIR` 等），无需 `.env` 文件。

这些说明的简版也会随插件压缩包一起提供（`kicad_plugin/README.md`）。

### 4. 在 KiCad 中加载插件

1. 打开 KiCad 并加载你的项目。
2. 打开 **PCB 编辑器**。
3. 进入 **Preferences → Plugins**，启用 **Enable KiCad API**。面板需要该 IPC 套接字来执行 **Reload PCB** 和 `update_pcb_from_schematic` 工具；未启用时这两项会失败，但直接编辑文件的功能仍然可用。通过 PCM 安装插件包后 KiCad 会自动提示启用；手动安装不会提示。
4. 依次点击 **Tools → External Plugins → Refresh Plugins**。
5. 在插件列表中点击 **KiCad AI Assistant**，打开聊天面板。
6. 进入 **Options → Settings**，输入你的 LLM API Key。

## 插件配置

插件配置文件路径：`~/.config/kicad/kicad_ai_assistant.json`

所有设置均可通过插件面板的 **Options → Settings** 修改：

| 设置项 | 说明 | 默认值 |
|--------|------|--------|
| `llm_provider` | LLM 提供商：`openai`、`anthropic` 或 `custom` | `openai` |
| `llm_api_key` | LLM API Key（以仅所有者可读权限存储） | *(空)* |
| `llm_model` | 模型名称 | `gpt-4o` |
| `llm_base_url` | 自定义接口地址（`llm_provider` 为 `custom` 时使用） | *(使用提供商默认值)* |
| `server_port` | 内置 MCP 服务器固定端口（`0` = 自动选择） | `0` |
| `mcp_tool_timeout_seconds` | MCP 工具调用的单次超时（秒）；超过后本轮对话以明确的超时错误结束。重启插件后生效 | `300` |
| `show_tool_log` | 默认显示工具调用日志面板 | `true` |
| `llm_context_tokens` | LLM 上下文窗口总 token 数 | `128000` |
| `llm_compact_threshold` | 上下文使用率超过此比例时触发压缩 | `0.70` |

## 独立 MCP 服务器

你也可以将 `kcaa` 作为独立 MCP 服务器运行，无需 KiCad 插件。适用于与其他 MCP 客户端集成（如 Claude Desktop、Cursor）。

在工作目录创建 `.env` 文件：

```dotenv
KICAD_SEARCH_PATHS=/home/user/pcb
KICAD_APP_PATH=/usr/share/kicad
KICAD_VERSION=10.0
KICAD_CONFIG_DIR=~/.config/kicad/10.0
KICAD_3RD_PARTY=~/.local/share/kicad/10.0/3rdparty
MCP_TRANSPORT=streamable-http
```

然后启动服务器：

```bash
kcaa
```

## 功能概览

- **原理图编辑** — 添加/删除符号、设置/重命名属性、绘制和删除导线、自动连接引脚
- **层次化图纸** — 创建、读取、更新、删除层次化图纸符号和图纸引脚
- **PCB 封装库** — 按名称、描述或标签搜索系统封装库索引；为原理图符号设置封装
- **PCB 同步** — 通过 KiCad IPC 接口触发"从原理图更新 PCB"操作
- **PCB 摆放** — 查询、移动、旋转、翻转、对齐、等间距分布封装；定义或清除板框
- **设计规则** — 查看和修改板级设计规则、网络类以及自定义 DRC 规则
- **DRC** — 直接从插件运行设计规则检查
- **上下文管理** — 当 LLM 上下文接近上限时自动压缩历史消息
- **会话管理** — 保存、恢复、重置当前会话；保存快照以便回退修改
- **技能系统** — 通过用户可定义的 Markdown 技能文件，为 LLM 提供按需工作流指导

## 工具列表

**命名约定** — 前缀 `library_` 表示**库中的定义**；无前缀表示**已放置在板上或图中的实例**。带 `library_` 的工具不返回放置信息。

- `get_library_footprint` — 封装库中的封装**定义**；键为 `library_name` + `footprint_name`，返回焊盘/courtyard/属性；焊盘坐标为封装本地（footprint-local）坐标，没有板级放置锚点。
- `get_footprint` — **PCB 上已放置的封装**；键为 `pcb_path` + `references[]`（世界坐标、旋转、层、属性、焊盘）。
- `list_symbol_properties` — **原理图中已放置的符号**；键为 `schematic_path` + `references[]`（仅属性，不含几何、位置或网络）。
- `extract_schematic_netlist` — 返回**整份网表**，因此带有**每个**已放置符号的放置信息（`components[ref].units[n].position`、`rotation`、每个 unit 的 `body_bbox`、世界坐标 `pins`）；不是廉价的单点坐标查询。
- `find_component_connections` — **单个位号**的放置信息与连接（`project_path` + `component_ref`）；只返回该位号自己的 `component_info`（同样含 `units[n].position`），不返回整份网表。

### 项目工具

| 工具 | 说明 |
|------|------|
| `list_projects` | 查找并列出所有 KiCad 项目 |
| `get_project_structure` | 获取项目结构和文件列表 |
| `open_project` | 在 KiCad 中打开项目 |

### 符号库

| 工具 | 说明 |
|------|------|
| `sync_symbol_index` | 构建或刷新符号库索引 |
| `get_symbol_sync_status` | 查询符号索引构建进度 |
| `get_symbol_index_stats` | 获取符号索引统计信息 |
| `list_symbol_libraries` | 列出所有符号库；`list_library_symbols` 列出单个库中的符号（按 `library_name` 指定） |
| `search_library_symbols` | 全文搜索已索引的符号 |
| `get_library_symbol` | 按库名和符号名查找符号 |
| `list_library_symbols` | 列出单个库中的符号（按 `library_name` 指定）；`list_symbol_libraries` 列出所有符号库 |
| `get_library_symbol_pins` | 获取符号的引脚定义 |
| `create_symbol_library` | 新建 `.kicad_sym` 符号库（全局第三方或项目级）并在库表中注册 |
| `delete_symbol_library` | 删除空符号库（连同文件、库表条目和索引） |
| `add_symbols_to_library` | 将原理图缓存 `lib_symbols` 中的符号复制到已有符号库 |
| `remove_symbols_from_library` | 从符号库中删除指定符号定义并重新建立索引 |
| `find_symbols_not_in_libraries` | 列出在所有可用符号库中都解析不到的原理图符号（只读） |

### 原理图编辑

| 工具 | 说明 |
|------|------|
| `add_symbol_to_schematic` | 在原理图上放置符号 |
| `create_symbol` | 在符号库中定义新符号（仅定义，不在原理图中放置） |
| `place_symbol_relative` | 相对于已有元件放置符号 |
| `remove_symbol_from_schematic` | 按位号删除已放置的符号 |
| `move_component` | 移动/旋转已放置的元件 |
| `set_symbol_property` | 设置已放置符号的属性字段 |
| `rename_symbol` | 重命名已放置符号的位号 |
| `list_symbol_properties` | 列出已放置原理图符号的属性（名称 → 值），不含几何、位置或网络 |
| `delete_symbol_property` | 删除已放置符号的属性 |
| `check_reference_conflicts` | 检查重复的位号 |
| `connect_points_with_wire` | 在两点之间智能正交布线 |
| `connect_pins_with_wire` | 用导线连接两个引脚 |
| `delete_wire_from_schematic` | 按端点删除导线段 |
| `add_label_to_schematic` | 添加局部网络标签 |
| `list_labels_in_schematic` | 列出所有局部网络标签 |
| `delete_label_from_schematic` | 删除网络标签 |
| `add_no_connect` | 在引脚坐标处添加不连接（no-connect）标志 |
| `list_no_connects` | 列出所有不连接标志 |
| `remove_no_connect` | 按坐标删除不连接标志 |
| `get_schematic_sheet_info` | 获取图纸尺寸和网格信息 |
| `find_free_area` | 查找可放置模块的候选区域 |

### 原理图分组

| 工具 | 说明 |
|------|------|
| `assign_symbols_to_group` | 将符号分配到命名摆放分组（保存在 `placement_group` 属性中） |
| `list_symbol_groups` | 列出所有符号摆放分组及其成员数与边界框 |
| `get_symbol_group` | 获取分组的成员、位置、锚点和边界框 |
| `score_symbol_group` | 评估分组内间距质量（平均最近邻距离和离散程度） |
| `place_symbol_group` | 以锚点为中心按网格排布分组，并自动放到图纸空白区域 |
| `move_symbol_group` | 按新锚点位置整体平移分组，保持组内相对布局不变 |
| `rotate_symbol_group` | 绕锚点整体旋转分组（逆时针为正） |

### 原理图分析

| 工具 | 说明 |
|------|------|
| `extract_schematic_netlist` | 从原理图提取网表 |
| `find_component_connections` | 查找元件的所有连接 |
| `run_erc` | 运行电气规则检查（调用无界面的 `kicad-cli`）并返回违规项 |
| `identify_circuit_patterns` | 识别常见电路模式 |
| `validate_project` | 项目基本验证 |

### 层次化图纸

| 工具 | 说明 |
|------|------|
| `list_sheet_symbols` | 列出原理图中所有层次化图纸符号 |
| `get_sheet_hierarchy` | 获取完整的图纸层次树 |
| `add_sheet_symbol` | 添加新的层次化图纸符号 |
| `remove_sheet_symbol` | 删除图纸符号（可选删除子文件） |
| `update_sheet_symbol` | 更新图纸符号的属性和几何信息 |
| `add_sheet_pin` | 向图纸符号添加层次化引脚 |
| `remove_sheet_pin` | 从图纸符号删除层次化引脚 |

### PCB 封装库

| 工具 | 说明 |
|------|------|
| `sync_footprint_index` | 构建或刷新封装库索引 |
| `get_footprint_sync_status` | 查询封装索引构建进度 |
| `list_footprint_libraries` | 列出所有可用封装库 |
| `search_library_footprints` | 按名称/描述/标签搜索封装 |
| `get_library_footprint` | 按库名 + 封装名获取封装库中的封装**定义**（焊盘、边界框等），不是板上的封装实例 |
| `list_library_footprints` | 列出单个库中的全部封装（名称及简要元数据）（支持 offset 分页） |
| `find_footprints_not_in_libraries` | 列出板上缺失于所有已索引库的封装 |
| `create_footprint_library` | 创建全局 3rdparty 或项目级封装库 |
| `add_footprints_to_library` | 将显式指定的板载封装导出到库 |
| `delete_footprint_library` | 删除空封装库（连同目录、库表条目和索引） |
| `remove_footprints_from_library` | 从封装库中删除指定封装并重新建立索引 |

### PCB 查询

| 工具 | 说明 |
|------|------|
| `get_board_info` | 获取 PCB 板基本信息 |
| `list_footprints` | 列出板上已放置的所有封装 |
| `get_footprint` | 获取板上已放置封装的详情（按 `references` 列表，每个位号一份结果；含焊盘、属性、Edge.Cuts 几何） |
| `get_footprint_bbox` | 获取封装的 courtyard 边界框 |
| `get_board_bounding_box` | 获取全板封装的联合边界框 |
| `list_nets` | 列出板上所有网络 |
| `list_tracks` | 列出走线段和圆弧，按连通性归并为走线，可按网络和层过滤 |
| `list_vias` | 列出过孔的位置、外径、孔径、层和网络，可按网络过滤 |
| `get_ratsnest` | 获取未布线飞线（ratsnest） |
| `score_placement` | 评估当前 PCB 摆放质量 |
| `suggest_placement_order` | 获取推荐的封装放置顺序 |

### PCB 编辑

| 工具 | 说明 |
|------|------|
| `get_board_outline` | 读取板框（Edge.Cuts）图形元素 |
| `clear_board_outline` | 清除板框 |
| `add_board_outline_segment` | 向板框添加直线段 |
| `add_board_outline_arc` | 向板框添加圆弧 |
| `set_board_outline_rect` | 设置矩形板框（支持圆角） |
| `set_footprint_property` | 设置封装的属性字段 |
| `add_footprints_to_pcb` | 从封装库批量导入封装并按焊盘逐一指定网络后放到板上 |
| `remove_footprints_from_pcb` | 按位号从板上删除封装（支持批量） |
| `update_pcb_from_schematic` | 通过 KiCad IPC 触发"从原理图更新 PCB" |

### PCB 布线

| 工具 | 说明 |
|------|------|
| `pcb_route_pad_to_pad` | 用带避障的布线器（PNS 推挤或网格 A*）在两个焊盘之间布一条走线，可跨层并通过过孔衔接 |
| `pcb_add_vias` | 一次写入添加一个或多个通孔过孔，写入前按网络类和间距规则校验 |
| `pcb_delete_tracks` | 按端点坐标删除走线段和圆弧 |
| `pcb_delete_vias` | 按 (x, y) 坐标删除过孔 |

### PCB 摆放

| 工具 | 说明 |
|------|------|
| `set_footprint_position` | 批量移动/旋转一个或多个封装 |
| `flip_footprint` | 将封装在顶层/底层之间翻转 |
| `align_footprints` | 将封装对齐到同一坐标轴 |
| `distribute_footprints` | 沿坐标轴等间距分布封装 |
| `move_footprints_by_delta` | 将封装整体平移 (dx, dy) |
| `find_free_pcb_area` | 搜索板上不与已有封装重叠的空闲区域 |

### PCB 分组

| 工具 | 说明 |
|------|------|
| `assign_footprints_to_group` | 将封装分配到摆放分组 |
| `list_footprint_groups` | 列出板上所有摆放分组 |
| `get_footprint_group` | 获取分组详情 |
| `score_footprint_group` | 评估分组内摆放质量 |
| `place_footprint_group` | 放置分组中的所有成员 |
| `move_footprint_group` | 平移已放置的分组 |
| `rotate_footprint_group` | 绕锚点旋转已放置的分组 |

### PCB 区域

| 工具 | 说明 |
|------|------|
| `list_zones` | 列出所有铜皮和禁布区 |
| `add_zone` | 添加铜皮或禁布区 |
| `delete_zone` | 按 UUID 删除区域 |
| `refill_zones` | 重新填充所有铜皮区域 |

### DRC 与设计规则

| 工具 | 说明 |
|------|------|
| `get_effective_design_rules` | 获取所有设计约束（板级规则、网络类、自定义规则） |
| `set_design_rules` | 更新板级设计规则最小值 |
| `set_net_class_rules` | 创建或更新网络类的设计参数 |
| `assign_nets_to_class` | 将网络分配到网络类 |
| `remove_nets_from_class` | 从网络类中移除网络（恢复为 Default） |
| `delete_net_class` | 删除网络类 |
| `add_custom_rule` | 向项目添加自定义 DRC 规则 |
| `del_custom_rule` | 按名称删除自定义 DRC 规则 |
| `run_drc_check` | 打开 KiCad DRC 对话框 |
| `analyze_bom` | 分析物料清单 |
| `export_bom_csv` | 导出 BOM 为 CSV |

### 版本管理与导出

| 工具 | 说明 |
|------|------|
| `save_project_version` | 保存项目版本归档（原理图+PCB+项目文件） |
| `list_project_versions` | 列出已保存的项目版本归档 |
| `restore_project_version` | 将项目文件一起恢复到已保存的归档 |
| `generate_pcb_thumbnail` | 渲染 PCB 缩略图 |
| `export_pcb_layer_image` | 将整板或单个铜层渲染为图片返回给模型，仅在传入 `output_dir` 时才写出 PNG 文件 |

### 技能系统

| 工具 | 说明 |
|------|------|
| `list_skills` | 列出所有可用的技能文档 |
| `get_skill` | 获取技能文档的完整内容 |
| `add_skill` | 创建新的技能文档 |
| `append_to_skill` | 向已有技能追加内容 |
| `delete_skill` | 软删除技能文档 |

### KiCad IPC

| 工具 | 说明 |
|------|------|
| `check_kicad_ipc_connection` | 检查 KiCad IPC 套接字是否响应 |
| `save_document` | 保存 KiCad 中的活动文档 |
| `reload_kicad` | 在 KiCad 编辑器中重新加载文档 |

## 项目结构

```
kcaa/
├── main.py                  # MCP 服务器入口
├── pyproject.toml           # 包元数据和依赖
├── run_tests.py             # 测试运行脚本
├── kcaa/                    # MCP 服务器包
│   ├── server.py            # 服务器初始化和工具注册
│   ├── config.py            # 配置和 KiCad 路径检测
│   ├── context.py           # 请求上下文管理
│   ├── tools/               # 所有 MCP 工具实现
│   ├── resources/           # MCP 资源处理器
│   ├── prompts/             # MCP 提示词模板
│   └── utils/               # 工具函数
├── kicad_plugin/            # KiCad 动作插件
│   ├── __init__.py          # 插件入口（KiCadAIPlugin）
│   ├── server_manager.py    # 启动/停止 kcaa 子进程
│   ├── llm_client.py        # LLM 代理工具调用循环（OpenAI / Anthropic）
│   ├── context_bridge.py    # 从 KiCad 收集当前项目路径
│   ├── settings.py          # 加载/保存插件配置
│   ├── autorouter.py        # FreeRouting 集成
│   ├── tool_registry.py     # 工具元数据和分类
│   ├── setup_plugin.sh      # Linux/macOS 安装脚本
│   ├── setup_plugin.ps1     # Windows PowerShell 安装脚本
│   ├── setup_plugin.bat     # Windows 批处理安装脚本
│   └── ui/                  # wxPython 聊天面板和设置对话框
├── docs/                    # 功能文档
└── tests/                   # 单元测试
```

## 常见问题

**插件未出现在 KiCad 中：**
- 手动安装：确认目录为 `<documents>/kicad/<ver>/scripting/plugins/kicad_ai_assistant`，且没有被重命名（旧说明使用的是 `kicad_ai_plugin`）。
- PCM 安装：确认 `<documents>/kicad/<ver>/3rdparty/plugins/com_github_paul356_kicad-ai-assistant` 存在，并且来自 `kicad_ai_assistant_pcm.zip`。
- 安装后执行 **Tools → External Plugins → Refresh Plugins**。
- Linux：检查 `setup_plugin.sh` 是否成功完成，并确认插件目录下存在 `.venv/bin/python`。
- Windows：检查 `setup_plugin.bat`（或 `setup_plugin.ps1`）是否成功完成，并确认插件目录下存在 `.venv/Scripts/python.exe`。
- 如果安装脚本失败，参见 [安装脚本排查说明](docs/troubleshooting.md#setup-script-fails-and-the-window-closes)。

**MCP 服务器启动失败：**
- Linux：查看 `~/.config/kicad/` 目录下的插件日志，排查 Python 报错信息。
- Windows：查看 `%APPDATA%/kicad/` 目录下的插件日志，排查 Python 报错信息。

**原理图编辑器编辑后未刷新：**
- 这是当前 KiCad IPC 接口的已知限制。请使用 **File → Reload** 或按 **Ctrl+Z / Ctrl+Y** 触发原理图编辑器刷新。

**LLM API 报错：**
- 在 **Options → Settings** 中确认 API Key 填写正确。
- 检查 `llm_model` 是否为所选提供商支持的有效模型名称。

## 贡献指南

1. Fork 本仓库
2. 创建功能分支
3. 提交包含测试的修改
4. 发起 Pull Request **指向 `develop` 分支**

> **PR 要求：**
> - 一个 Pull Request 只能包含**一个**修复（fix）、增强（enhancement）或功能（feature）。如果有多个独立的修改，请分别提交为多个 Pull Request。
> - 修改需要包含测试。

> **分支说明：**
> - `develop` 分支用于稳定新特性——请先把修改通过 PR 提交到这里，经过测试和评审后再合入。
> - `main` 分支用于发布版本，在测试通过后，从 `develop` 合并到 `main` 完成发布。

## 许可证

本项目基于 MIT 许可证开源。
