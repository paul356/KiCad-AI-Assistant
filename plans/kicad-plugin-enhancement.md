# kicad_plugin 优化建议

## 状态：图片上传已完成，PDF 解析待做

## 目标

为 kicad_plugin（KiCad AI Assistant 插件）增加两项能力，要求同时支持 **Linux / Windows / macOS**：

1. 支持上传/粘贴图片（用户给 LLM 看图，含从剪贴板粘贴截图）
2. 支持解析 PDF 文档

> 截图能力不做插件内实现：用户自行截图后 **Ctrl+V** 粘贴到聊天框即可，schematic / PCB 截图均走此路径。详见「暂不考虑」章节。

## 现有架构（相关部分）

```
KiCad GUI (wxPython)
  └─ kicad_plugin/
       ├─ ui/panel.py        ← 聊天面板（wx.Frame, TextCtrl 输入）
       ├─ llm_client.py      ← LLMClient，直连 OpenAI/Anthropic/Ollama，多模态消息在此构建
       ├─ server_manager.py  ← 启动 kcaa MCP server（独立 venv 子进程）
       └─ setup_plugin.sh/.bat/.ps1 ← 创建 venv 安装依赖
```

关键结论：
- 插件运行在 KiCad 嵌入式 wx 环境，`wx` 可用 → 截图/图片 UI 无需额外依赖。
- LLM 消息构建集中在 `llm_client.py`（`_build_anthropic_messages` / `_stream_openai` / `_stream_ollama` / `_call_*`），新增多模态字段需按 provider 分别处理。
- KiCad 嵌入式 Python 装第三方包受限；纯 Python / 有跨平台轮子的库（PyMuPDF、pdfplumber、pypdf）应装进 `setup_plugin*.sh/.bat/.ps1` 创建的 venv，插件侧以子进程调用。

---

## 1. 支持上传 / 粘贴图片

### 方案
- **UI（panel.py）**：输入框旁加"添加图片"按钮（`wx.FileDialog` 过滤 png/jpg），选中后 `wx.Image` 加载并显示缩略图；支持一次携带多张，随下一条消息一起发送。
- **剪贴板粘贴（Ctrl+V）**：输入框内 Ctrl+V 时，若系统剪贴板含位图（`wx.DF_BITMAP`）则粘贴为图片（存临时 PNG 进附件栏），否则退化为普通文本粘贴；另有"粘贴"按钮（`wx.ART_PASTE`）触发同一逻辑。
- **附件缩略图条**：已选图片显示 48×48 缩略图，点击移除；带数量标签与"✕ clear"一键清空。
- **消息构建（llm_client.py）**，按 provider 区分：
  - OpenAI：content 变为数组
    `[{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]`
  - Anthropic：
    `[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"<base64>"}}]`
  - Ollama：请求体加 `images: ["<base64>"]`，messages 使用数组格式。
- **发送前压缩**：最长边缩到 ~1024px，控制体积，避免超 token/超时；统一 `wx.Image.Scale` → `wx.Image.ConvertToBitmap` → 编码。
- **会话保存**：写 session 文件前剥离 `image_url` 块（避免 base64 撑爆文件），加载会话后图片上下文不保留。
- **模型要求**：需 vision 能力（gpt-4o / claude-3.x / llava 等），在设置中提示。

### 跨平台
- 全链路只用 Python 标准库（base64）+ wx → 天然支持三平台。

---

## 2. 支持解析 PDF 文档

### 方案 A（主路径）：文本抽取 + 页码标注
- `pypdf`（纯 Python）/ `pdfplumber`（表格友好）/ `PyMuPDF`（fitz，速度快）。
- **每段文字前标注页码**（如 `[P3]`），LLM 回答时能引用具体页。

### 方案 B（用户按需补充）：页面转图
- `PyMuPDF` 渲染用户指定页为 PNG，走第 1 节多模态格式发送 —— 适合 datasheet 原理图、图表密集页。
- **由用户判断**：看完文本摘要后，如信息不足（图表、原理图），用户主动要求补充指定页截图，而非系统自动转图。

### 推荐组合（简化版，2026-08-04 定稿）
- **默认流程**：全量抽文本（带页码）入上下文。
- **补充流程**：用户判断需要看图 → 用户要求渲染指定页（或当前打开的 PDF 页）→ 方案 B 转图发给 LLM。
- 不引入 LLM 选页/自动转图逻辑，保持实现简单。

### 运行位置
- 库安装进 `setup_plugin.sh/.bat/.ps1` 的 venv（`pymupdf`、`pdfplumber`）。
- 插件侧通过 `server_manager` 类似机制以子进程跑解析脚本，避免污染 KiCad 嵌入式 Python。

### 跨平台
- `pymupdf` / `pdfplumber` / `pypdf` 均有 Linux / macOS / Windows 支持（PyMuPDF 提供三平台 wheel）。

---

## 实施顺序建议

1. 图片上传/粘贴（第 1 节）—— 打通多模态消息格式，PDF 复用。✅ 已完成
2. PDF 解析（第 2 节）—— 主路径走方案 A（抽文本带页码）；补充走方案 B（用户指定页转图）。依赖 venv 与子进程机制。

## 风险 / 注意事项

- **token 与超时**：图片/多页 PDF 会显著增加输入体积，需限制尺寸与页数（如最多前 N 页）。
- **Vision 模型支持**：需在设置中提示所选模型必须支持图像输入。
- **剪贴板格式**：Ctrl+V 依赖系统剪贴板位图格式（wx.DF_BITMAP），X11/Wayland/Windows/macOS 需验证；剪贴板只有文本时退化为普通粘贴。
- **测试**：三平台 CI 需覆盖图片粘贴与 PDF 解析（现有 `.github` 有 CI 可扩展）。

---

## 最终方案与目标（2026-08-05 定稿）

### 目标
为 kicad_plugin（KiCad AI Assistant 插件）增加两项能力，支持 Linux / Windows / macOS：
1. 用户上传/粘贴图片给 LLM 看（含 Ctrl+V 粘贴系统截图）
2. 解析 PDF 文档

> 截图不单独做插件内实现：用户自行截图后 Ctrl+V 粘贴到聊天框。

### 最终方案

| 能力 | 方案 | 说明 |
|---|---|---|
| 图片上传 | **已实现**（2026-08-04） | 面板加"添加图片/粘贴"按钮，缩略图条，Ctrl+V 智能粘贴（剪贴板有 bitmap 时粘贴为图片，否则粘贴文本）；压缩 ≤1024px → base64；OpenAI/Anthropic/Ollama 三 provider 格式转换；会话保存剥离 base64 |
| 截图（schematic/PCB） | 不实现插件内抓取 | 用户系统截图 → Ctrl+V 粘贴 → 复用图片链路 |
| PDF 解析 | **主路径**：全量抽文本（带页码 `[P3]`）入上下文；**补充**：用户判断信息不足时要求渲染指定页转图 | 不做 LLM 选页/自动转图，保持简单 |

### 迭代路径（先落地，看效果再增强）
1. **PDF v1**：只做"抽文本带页码"（PyMuPDF，venv 子进程）。验证文本抽取对 datasheet 的可用性。
2. **PDF v2**（视 v1 效果）：用户指定页转图 → 复用图片注入链路。
3. **PDF v3**（可选）：若文本乱/图表多，再考虑 qwen3.8-max 原生 PDF 理解（方案 C）或 LLM 选页（见「暂不考虑」章节）。

### 已完成的代码改动（ideas 分支）
- `kicad_plugin/llm_client.py`：`run()` 支持 `images` 参数；OpenAI 数组格式 + Anthropic/Ollama 转换；`_maybe_compact` 兼容数组 content。
- `kicad_plugin/ui/panel.py`：附件 UI（添加/粘贴/缩略图/清空）；Ctrl+V 智能粘贴；压缩编码；会话保存剥离图片。
- 44 个 `test_llm_client` 单元测试通过；ruff lint/format 通过。

---

## 暂不考虑（2026-08-05）

以下能力经研究后决定暂不实现，保留研究结论供后续参考。

### N1. 插件内 schematic / PCB 截图

#### 方案 A：wx 屏幕抓取（仅限 PCB editor 实时取景）
- 插件在 KiCad GUI 内运行，用 `wx.ScreenDC` + `wx.MemoryDC` + `wx.Bitmap` 抓取当前编辑器画布区域：
  ```python
  dc = wx.ScreenDC()
  bmp = wx.Bitmap(w, h)
  mem = wx.MemoryDC(bmp)
  mem.Blit(0, 0, w, h, dc, x, y)
  mem.SelectObject(wx.NullBitmap)
  bmp.ConvertToImage().SaveFile(path, wx.BITMAP_TYPE_PNG)
  ```
- 原理：`wx.ScreenDC` 走 OS 层 API 抓屏幕像素（X11 `XGetImage` / Windows `BitBlt` / macOS `CGWindowListCreateImage`），**抓取与窗口属于哪个进程无关**。

#### ⚠️ 难点（2026-08-04 研究结论）
- **插件只运行在 PCB editor 进程**：`kicad_plugin/__init__.py` 中 `_ActionPluginBase = pcbnew.ActionPlugin`。
- **KiCad 的 schematic editor（eeschema）是独立进程**：`wx.GetTopLevelWindows()` 只能枚举**本进程**窗口 → **Python 端从物理上拿不到 schematic editor 的 wx 对象**（不是难，是跨进程不可达）。
- 要抓 schematic 窗口必须跨进程查窗口坐标：Windows `EnumWindows` / X11 `xwininfo` / macOS `CGWindowListCopyWindowInfo`，三平台实现各异、维护成本高。
- **结论：方案 A 只对 PCB editor 可行（同进程拿 `GetScreenPosition()`）；schematic 实时截图改走方案 B。**
- 另：Linux **Wayland** 下 `wx.ScreenDC` 抓屏受限（X11 正常），需验证。

#### 方案 B：kicad-cli 导出
- 不走 KiCad GUI，用独立进程 `kicad-cli` 导出文件 → **无跨进程问题、无 GUI 依赖**。
- PCB：`kicad-cli pcb export svg --output out.svg board.kicad_pcb`（KiCad 7+）。
- Schematic：`kicad-cli sch export svg --output out.svg sheet.kicad_sch`（KiCad 7+，`--recursive` 可导出层级子图）。

#### ✅ 可行性（2026-08-04 研究结论：链路大半已存在）
- **PCB 链路已实现**：`kcaa/tools/export_tools.py` 的 `generate_thumbnail_with_cli` 已用 `kicad-cli pcb export svg` 生成 PCB 图并返回给 LLM（fastmcp `Image`）。
- **kicad-cli 查找已封装**：`kcaa/utils/kicad_cli.py`（三平台路径检测 + 缓存 + `KICAD_CLI_PATH` 环境变量）；安全子进程封装在 `kcaa/utils/secure_subprocess.py`。
- **schematic 子命令已验证**：`kcaa/tools/bom_tools.py` 已用 `kicad-cli sch export bom` → sch 链路可用。
- 测试夹具齐全：`tests/**/fixtures/` 下有 `.kicad_pcb` / `.kicad_sch`，可在有 KiCad 的环境写集成测试。

#### ⚠️ 难点
- **kicad-cli 不直接输出 PNG**（`pcb/sch export` 只支持 svg/pdf/dxf 等）。**SVG 不是 vision 模型原生支持的格式**（OpenAI/Anthropic/Ollama 只收 png/jpeg/webp/gif）→ 必须转换：Pillow（纯 wheel）或 cairosvg（native cairo）。
- **现有 `generate_thumbnail_with_cli` 直接返回 `format="svg"`，LLM 侧可能拒收** → 需补 SVG→PNG 转换步骤。
- 实时性差：导出的是整板/整图，不是当前视图（"当前视图"仍要靠方案 A，仅 PCB）。
- 本机（2026-08-04）无 kicad-cli，尚未实测运行；需在有 KiCad 环境验证 `sch export svg` 与转换链路。

#### 暂不考虑原因
- 方案 A 跨进程不可行（schematic）；方案 B 需额外维护 SVG→PNG 转换，性价比低。
- **替代路径**：用户用系统截图工具截取 schematic / PCB 画面 → 在插件输入框 **Ctrl+V** 粘贴 → 走第 1 节图片注入链路发给 LLM。该功能已在第 1 节的图片粘贴实现中覆盖。

### N2. PDF 方案 C：百炼 + qwen3.8-max 原生 PDF 理解

- **qwen3.8-max 原生支持 PDF**（2026-08-04 官方文档确认，`https://help.aliyun.com/zh/model-studio/pdf-understanding`），走 OpenAI 兼容接口，content 数组加 `type: "file"` 块：
  ```json
  {"type": "file", "file": {"file_url": "https://.../doc.pdf"}}            // URL，二选一
  // 或 Base64：
  {"type": "file", "file": {"file_data": "data:application/pdf;base64,xxx", "filename": "report.pdf"}}  // file_data 时 filename 必填
  ```
- **限制**：单文件 ≤150MB、≤500 页；首包超时最长 300s（建议流式）；**仅华北 2（北京）地域**；不支持 Responses API。
- **计费**（两段流水线）：
  1. 文档解析费：0.02 元/页（平台先做版面分析/OCR，拆成文字 + 图片）；
  2. 模型调用费：解析出的**文字按文本 token、图片按图片 token** 计入输入，按模型标准输入价计费。
- **优点**：不用在插件里装 PyMuPDF/pdfplumber 做预处理；文字走文本 token（便宜）、仅图表走图片 token。
- **缺点**：非通用能力（仅百炼华北2 + qwen3.8-max）；base_url 需带 WorkspaceId（`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`）。

#### 暂不考虑原因
- 非通用能力，仅百炼华北2 + qwen3.8-max；先走通用方案 A+B，视效果再考虑。

### N3. PDF 图文拆分的对应关系问题

- 方案 C / 方案 A+B 把文字与图片拆开，**页面内图文相对位置丢失**（模型不知道图片A和文字B在页面上谁在上谁在下）。
- **保留的**：页序、图注/标题文字（caption 是文字，跟着文字流走）。
- **影响分场景**：文字为主（报告/论文）影响小；图文强耦合（datasheet 原理图+引脚表+时序图）影响中等 —— 但图片内部信息（引脚名、数值）模型可自读，不依赖坐标对应，且图注可交叉验证。
- **强版面场景**：用方案 B 整页转图保真，代价是全页按图片 token 计费（更贵）。

#### 暂不考虑原因
- 先落地方案 A（抽文本带页码），看实际效果再决定是否需要整页转图保真。

### N4. PDF 成本对比与图片 token 算法

| 方案 | 解析成本 | 输入 token 成本 | 通用性 |
|---|---|---|---|
| 百炼原生 PDF（方案 C） | 0.02 元/页 | 文字按文本、图表按图片 token | 仅华北2 + qwen3.8-max |
| 本地转图（方案 B） | 0（本地渲染） | 全页按图片 token（贵） | 任何 vision 模型 |
| 本地抽文本（方案 A） | 0 | 仅文本 token（最便宜，但丢图表） | 任何模型 |
- 图片 token 算法（百炼视觉模型）：**每张 ≈ ⌈h×w/1024⌉ + 2**（1024×1024 ≈ 1026 token，线性增长）。
- 图文混合文档用方案 C 更划算；纯图 PDF 方案 B/C 成本接近。
