# KiCad AI Assistant

KiCad AI Assistant 是一个 KiCad 动作插件，在 KiCad 内部直接嵌入了由大语言模型（LLM）驱动的聊天面板。插件内置 [MCP](https://modelcontextprotocol.io/) 服务器，并暴露了丰富的工具集，让 LLM 能够通过自然语言对话读取和编辑原理图与 PCB。

已在 **KiCad 10.0 / Linux & Windows** 上验证。

[![CI](https://github.com/paul356/KiCad-AI-Assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/paul356/KiCad-AI-Assistant/actions/workflows/ci.yml)


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
    - [原理图分析](#原理图分析)
    - [层次化图纸](#层次化图纸)
    - [PCB 封装库](#pcb-封装库)
    - [PCB 查询](#pcb-查询)
    - [PCB 编辑](#pcb-编辑)
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
  - `curl -Lsf https://astral.sh/uv/install.sh | sh`
- OpenAI、Anthropic 或兼容 LLM 提供商的 API Key

## 安装

### 1. 克隆仓库（可选）

仅当你需要从源码构建插件或参与项目开发时才需要此步骤。如果从 Releases 页面下载预构建的插件，可跳过此步骤。

```bash
git clone https://github.com/paul356/kcaa.git
cd kcaa
```

### 2. 安装插件

从 [Releases 页面](https://github.com/paul356/KiCad-AI-Assistant/releases) 下载 `kicad-ai-assistant.zip`，解压到 KiCad 插件目录：

**Linux:**
```bash
KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip kicad-ai-assistant.zip -d "$KICAD_PLUGIN_DIR"
```

**Windows (PowerShell):**
```powershell
$KICAD_PLUGIN_DIR = "$env:USERPROFILE\Documents\KiCad\10.0\scripting\plugins"
New-Item -ItemType Directory -Force -Path $KICAD_PLUGIN_DIR
Expand-Archive -Path kicad-ai-assistant.zip -DestinationPath $KICAD_PLUGIN_DIR
```

或从源码构建：

```bash
# 在 kcaa 仓库根目录执行：
make dist-plugin          # 生成 dist/kicad_ai_assistant.zip

# Linux:
KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip dist/kicad_ai_assistant.zip -d "$KICAD_PLUGIN_DIR"
```

### 3. 创建插件虚拟环境

在已安装的插件目录中运行安装脚本，创建 `.venv`、从 PyPI 安装 `kcaa`，并下载 freerouting JAR。`uv` 会自动安装所需的 Python 版本。

**Linux:**
```bash
cd ~/.local/share/kicad/10.0/scripting/plugins/kicad_ai_assistant
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

脚本会从插件目录路径自动检测 KiCad 版本。KiCad 已提供所有必要的环境变量（`KICAD_VERSION`、`KICAD*_DIR` 等），无需 `.env` 文件。

### 4. 在 KiCad 中加载插件

1. 打开 KiCad 并加载你的项目。
2. 打开 **PCB 编辑器**。
3. 依次点击 **Tools → External Plugins → Refresh Plugins**。
4. 在插件列表中点击 **KiCad AI Assistant**，打开聊天面板。
5. 进入 **Options → Settings**，输入你的 LLM API Key。

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
| `list_symbol_libraries` | 列出索引中的符号库 |
| `search_symbols` | 全文搜索已索引的符号 |
| `get_symbol` | 按库名和符号名查找符号 |
| `get_library_symbols` | 获取指定库中的符号列表 |
| `get_symbol_pins` | 获取符号的引脚定义 |

### 原理图编辑

| 工具 | 说明 |
|------|------|
| `add_symbol_to_schematic` | 在原理图上放置符号 |
| `place_symbol_relative` | 相对于已有元件放置符号 |
| `remove_symbol_from_schematic` | 按位号删除已放置的符号 |
| `move_component` | 移动/旋转已放置的元件 |
| `set_symbol_property` | 设置已放置符号的属性字段 |
| `rename_symbol` | 重命名已放置符号的位号 |
| `list_symbol_properties` | 列出已放置符号的所有属性 |
| `delete_symbol_property` | 删除已放置符号的属性 |
| `check_reference_conflicts` | 检查重复的位号 |
| `connect_points_with_wire` | 在两点之间智能正交布线 |
| `add_wire_to_schematic` | 按端点添加单条导线段 |
| `connect_pins_with_wire` | 用导线连接两个引脚 |
| `delete_wire_from_schematic` | 按端点删除导线段 |
| `add_label_to_schematic` | 添加局部网络标签 |
| `list_labels_in_schematic` | 列出所有局部网络标签 |
| `delete_label_from_schematic` | 删除网络标签 |
| `get_schematic_sheet_info` | 获取图纸尺寸和网格信息 |
| `find_free_area` | 查找可放置模块的候选区域 |

### 原理图分析

| 工具 | 说明 |
|------|------|
| `extract_schematic_netlist` | 从原理图提取网表 |
| `extract_project_netlist` | 提取整个项目的网表 |
| `find_component_connections` | 查找元件的所有连接 |
| `identify_circuit_patterns` | 识别常见电路模式 |
| `analyze_project_circuit_patterns` | 分析项目中的电路模式 |
| `validate_project` | 项目基本验证 |
| `validate_project_boundaries` | 验证元件边界 |
| `generate_validation_report` | 生成综合验证报告 |

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
| `search_footprints` | 按名称/描述/标签搜索封装 |
| `get_footprint_details` | 获取封装详情（焊盘、边界框等） |
| `find_footprints_not_in_libraries` | 列出板上缺失于所有已索引库的封装 |
| `create_footprint_library` | 创建全局 3rdparty 或项目级封装库 |
| `add_footprints_to_library` | 将显式指定的板载封装导出到库 |

### PCB 查询

| 工具 | 说明 |
|------|------|
| `get_board_info` | 获取 PCB 板基本信息 |
| `list_footprints` | 列出板上已放置的所有封装 |
| `get_footprint` | 获取单个已放置封装的详情 |
| `get_footprint_bbox` | 获取封装的 courtyard 边界框 |
| `get_board_bounding_box` | 获取全板封装的联合边界框 |
| `list_nets` | 列出板上所有网络 |
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
| `update_pcb_from_schematic` | 通过 KiCad IPC 触发"从原理图更新 PCB" |

### PCB 摆放

| 工具 | 说明 |
|------|------|
| `set_footprint_position` | 移动/旋转单个封装 |
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
| `save_file_version` | 保存版本快照以便回退 |
| `list_file_versions` | 列出已保存的版本快照 |
| `restore_file_version` | 恢复到之前保存的版本 |
| `generate_pcb_thumbnail` | 渲染 PCB 缩略图 |
| `generate_project_thumbnail` | 渲染项目缩略图 |

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
- 确认插件目录名称为 `kicad_ai_assistant`（注意不要写错）。
- 安装后执行 **Tools → External Plugins → Refresh Plugins**。
- Linux：检查 `setup_plugin.sh` 是否成功完成，并确认插件目录下存在 `.venv/bin/python`。
- Windows：检查 `setup_plugin.bat`（或 `setup_plugin.ps1`）是否成功完成，并确认插件目录下存在 `.venv/Scripts/python.exe`。

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
