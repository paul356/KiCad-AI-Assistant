# KiCad AI Assistant

KiCad AI Assistant 是一个 KiCad 动作插件，在 KiCad 内部直接嵌入了由大语言模型（LLM）驱动的聊天面板。插件内置 [MCP](https://modelcontextprotocol.io/) 服务器，并暴露了丰富的工具集，让 LLM 能够通过自然语言对话读取和编辑原理图与 PCB。

已在 **KiCad 10.0 / Linux** 上验证。

## 目录

- [前置条件](#前置条件)
- [安装](#安装)
  - [1. 克隆仓库](#1-克隆仓库)
  - [2. 配置环境](#2-配置环境)
  - [3. 构建并安装插件](#3-构建并安装插件)
  - [4. 创建插件虚拟环境](#4-创建插件虚拟环境)
  - [5. 在 KiCad 中加载插件](#5-在-kicad-中加载插件)
- [插件配置](#插件配置)
- [功能概览](#功能概览)
- [工具列表](#工具列表)
  - [原理图工具](#原理图工具)
  - [PCB 工具](#pcb-工具)
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

### 1. 克隆仓库

```bash
git clone https://github.com/paul356/kicad-mcp.git
cd kicad-mcp
```

### 2. 配置环境

在仓库根目录创建 `.env` 文件，告知服务器 KiCad 的安装位置和项目目录：

```bash
cp .env.example .env
vim .env
```

需要设置的关键变量：

| 变量 | 说明 | 示例 |
|------|------|------|
| `KICAD_SEARCH_PATHS` | 扫描 KiCad 项目的目录，多个目录用逗号分隔 | `/home/user/pcb` |
| `KICAD_APP_PATH` | KiCad 共享数据目录路径 | `/usr/share/kicad` |
| `KICAD_VERSION` | KiCad 主版本号.次版本号 | `10.0` |
| `KICAD_CONFIG_DIR` | KiCad 用户配置目录 | `~/.config/kicad/10.0` |
| `KICAD_3RD_PARTY` | KiCad 第三方插件目录 | `~/.local/share/kicad/10.0/3rdparty` |
| `MCP_TRANSPORT` | MCP 传输协议 | `streamable-http` |

Linux 典型 `.env` 示例：

```dotenv
KICAD_SEARCH_PATHS=/home/user/pcb
KICAD_APP_PATH=/usr/share/kicad
KICAD_VERSION=10.0
KICAD_CONFIG_DIR=~/.config/kicad/10.0
KICAD_3RD_PARTY=~/.local/share/kicad/10.0/3rdparty
MCP_TRANSPORT=streamable-http
```

### 3. 构建并安装插件

使用 `make` 构建插件 zip 包，然后解压到 KiCad 插件目录：

```bash
# 在 kicad-mcp 仓库根目录执行：
make dist-plugin          # 生成 dist/kicad_ai_assistant.zip

KICAD_PLUGIN_DIR=~/.local/share/kicad/10.0/scripting/plugins
mkdir -p "$KICAD_PLUGIN_DIR"
unzip dist/kicad_ai_assistant.zip -d "$KICAD_PLUGIN_DIR"
```

### 4. 创建插件虚拟环境

在已安装的插件目录中运行 `setup_plugin.sh`，创建 `.venv` 并将 `kicad_mcp` 以可编辑模式安装。`uv` 会自动安装所需的 Python 版本。

```bash
cd ~/.local/share/kicad/10.0/scripting/plugins/kicad_ai_assistant
./setup_plugin.sh /path/to/kicad-mcp
```

### 5. 在 KiCad 中加载插件

1. 打开 KiCad 并加载你的项目。
2. 打开**原理图编辑器**或 **PCB 编辑器**。
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

## 功能概览

- **原理图编辑** — 添加/删除符号、设置属性、绘制和删除导线、自动连接引脚
- **PCB 封装库** — 按名称、描述或标签搜索系统封装库索引；为原理图符号设置封装
- **PCB 同步** — 通过 KiCad IPC 接口触发"从原理图更新 PCB"操作
- **PCB 摆放** — 查询、移动、旋转、翻转、对齐、等间距分布封装；定义或清除板框
- **上下文管理** — 当 LLM 上下文接近上限时自动压缩历史消息
- **会话管理** — 保存、恢复、重置当前会话；保存快照以便回退修改
- **DRC** — 运行设计规则检查并追踪违规历史

## 工具列表

### 原理图工具

| 工具 | 说明 |
|------|------|
| `list_symbol_libraries` | 列出所有可用符号库 |
| `search_symbols` | 按名称、描述或关键字搜索符号 |
| `get_symbol` | 获取符号的详细信息 |
| `get_symbol_pins` | 获取符号的引脚定义 |
| `sync_symbol_index` | 构建或刷新符号库索引 |
| `get_symbol_sync_status` | 查询符号索引构建进度 |
| `get_symbol_index_stats` | 获取符号索引统计信息 |
| `get_symbol_index_libraries` | 列出符号索引中包含的库 |
| `add_symbol_to_schematic` | 在原理图上放置符号 |
| `remove_symbol_from_schematic` | 按位号删除已放置的符号 |
| `move_component` | 将元件移动到新坐标 |
| `set_component_property` | 设置已放置符号的属性字段 |
| `get_component_properties` | 读取已放置符号的所有属性字段 |
| `add_wire_to_schematic` | 在两点之间绘制导线段 |
| `connect_pins_with_wire` | 自动在两个引脚之间布线 |
| `delete_wire_from_schematic` | 删除导线段 |
| `save_snapshot` | 保存当前原理图快照以便回退 |
| `list_projects` | 列出搜索路径中的 KiCad 项目 |
| `get_project_info` | 获取 KiCad 项目信息 |
| `open_project` | 打开 KiCad 项目 |
| `extract_schematic_netlist` | 从原理图提取网表 |
| `extract_project_netlist` | 提取整个项目的网表 |
| `find_component_connections` | 查找连接到某元件的所有网络 |
| `analyze_schematic` | 分析原理图设计问题 |
| `identify_circuit_patterns` | 识别常见电路模式 |
| `recognize_circuit_patterns` | 扩展电路模式识别 |
| `run_drc` | 运行 KiCad CLI 设计规则检查 |
| `get_drc_history` | 获取历史 DRC 结果 |
| `generate_bom` | 生成物料清单 |
| `export_bom` | 导出物料清单到文件 |
| `generate_thumbnail` | 渲染 PCB 缩略图 |

### PCB 工具

| # | 工具 | 说明 |
|---|------|------|
| 1 | `sync_footprint_index` | 后台构建/增量更新封装库索引 |
| 2 | `get_footprint_sync_status` | 查询封装索引构建进度 |
| 3 | `list_footprint_libraries` | 列出所有可用封装库 |
| 4 | `search_footprints` | 按名称/描述/标签搜索封装 |
| 5 | `get_footprint_details` | 获取封装详情（焊盘、边界框等） |
| 6 | `get_board_info` | 获取 PCB 板基本信息 |
| 7 | `list_footprints` | 列出板上已放置的所有封装 |
| 8 | `get_footprint` | 获取单个已放置封装的详情 |
| 9 | `list_nets` | 列出板上所有网络 |
| 10 | `get_ratsnest` | 获取未布线飞线（ratsnest） |
| 11 | `get_board_outline` | 读取板框（Edge.Cuts）图形元素 |
| 12 | `clear_board_outline` | 清除板框 |
| 13 | `add_board_outline_segment` | 向板框添加直线段 |
| 14 | `add_board_outline_arc` | 向板框添加圆弧 |
| 15 | `set_board_outline_rect` | 一步设置矩形板框（支持圆角） |
| 16 | `get_footprint_bbox` | 获取单个封装的 courtyard 边界框 |
| 17 | `get_board_bounding_box` | 获取全板封装的联合边界框 |
| 18 | `align_footprints` | 将一组封装对齐到同一坐标轴 |
| 19 | `distribute_footprints` | 沿坐标轴等间距分布封装 |
| 20 | `move_footprints_by_delta` | 将一组封装整体平移 (dx, dy) |
| 21 | `find_free_pcb_area` | 搜索板上不与已有封装重叠的空闲区域 |
| 22 | `set_footprint_position` | 移动/旋转单个封装 |
| 23 | `flip_footprint` | 将封装在顶层/底层之间翻转 |
| 24 | `set_footprint_property` | 设置封装的属性字段 |
| 25 | `update_pcb_from_schematic` | 通过 KiCad IPC 触发"从原理图更新 PCB"操作 |
| 26 | `reload_kicad` | 在 KiCad 编辑器中重新加载文件 |

## 项目结构

```
kicad-mcp/
├── main.py                  # MCP 服务器入口
├── pyproject.toml           # 包元数据和依赖
├── .env                     # 本地环境配置（不提交到版本库）
├── kicad_mcp/               # MCP 服务器包
│   ├── server.py            # 服务器初始化和工具注册
│   ├── config.py            # 配置和 KiCad 路径检测
│   ├── tools/               # 所有 MCP 工具实现
│   ├── resources/           # MCP 资源处理器
│   └── prompts/             # MCP 提示词模板
├── kicad_plugin/            # KiCad 动作插件
│   ├── __init__.py          # 插件入口（KiCadAIPlugin）
│   ├── server_manager.py    # 启动/停止 kicad-mcp 子进程
│   ├── llm_client.py        # LLM 代理工具调用循环（OpenAI / Anthropic）
│   ├── context_bridge.py    # 从 KiCad 收集当前项目路径
│   ├── settings.py          # 加载/保存插件配置
│   ├── setup_plugin.sh      # 创建插件 .venv 的辅助脚本
│   └── ui/                  # wxPython 聊天面板和设置对话框
├── docs/                    # 功能文档
└── tests/                   # 单元测试
```

## 常见问题

**插件未出现在 KiCad 中：**
- 确认插件目录名称为 `kicad_ai_assistant`（注意不要写错）。
- 安装后执行 **Tools → External Plugins → Refresh Plugins**。
- 检查 `setup_plugin.sh` 是否成功完成，并确认插件目录下存在 `.venv/bin/python`。

**MCP 服务器启动失败：**
- 检查 `.env` 中的 `KICAD_APP_PATH` 和 `KICAD_VERSION` 是否指向实际的 KiCad 安装路径。
- 查看 `~/.config/kicad/` 目录下的插件日志，排查 Python 报错信息。

**原理图编辑器编辑后未刷新：**
- 这是当前 KiCad IPC 接口的已知限制。请使用 **File → Reload** 或按 **Ctrl+Z / Ctrl+Y** 触发原理图编辑器刷新。

**LLM API 报错：**
- 在 **Options → Settings** 中确认 API Key 填写正确。
- 检查 `llm_model` 是否为所选提供商支持的有效模型名称。

## 贡献指南

1. Fork 本仓库
2. 创建功能分支
3. 提交包含测试的修改
4. 发起 Pull Request

## 许可证

本项目基于 MIT 许可证开源。
