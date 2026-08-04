# kicad_plugin 优化建议

## 状态：规划中

## 目标

为 kicad_plugin（KiCad AI Assistant 插件）增加三项能力，要求同时支持 **Linux / Windows / macOS**：

1. 支持上传图片（用户给 LLM 看图）
2. 支持获取 schematic 或 PCB 的截图（LLM 主动"看"板子）
3. 支持解析 PDF 文档

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

## 1. 支持上传图片

### 方案
- **UI（panel.py）**：输入框旁加"添加图片"按钮（`wx.FileDialog` 过滤 png/jpg），选中后 `wx.Image` 加载并显示缩略图；支持一次携带多张，随下一条消息一起发送。
- **消息构建（llm_client.py）**，按 provider 区分：
  - OpenAI：content 变为数组
    `[{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]`
  - Anthropic：
    `[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"<base64>"}}]`
  - Ollama：请求体加 `images: ["<base64>"]`，messages 使用数组格式。
- **发送前压缩**：最长边缩到 ~1024px，控制体积，避免超 token/超时；统一 `wx.Image.Scale` → `wx.Image.ConvertToBitmap` → 编码。
- **模型要求**：需 vision 能力（gpt-4o / claude-3.x / llava 等），在设置中提示。

### 跨平台
- 全链路只用 Python 标准库（base64）+ wx → 天然支持三平台。

---

## 2. 支持获取 schematic / PCB 截图

### 方案 A：wx 屏幕抓取（推荐，主路径）
- 插件在 KiCad GUI 内运行，用 `wx.ScreenDC` + `wx.MemoryDC` + `wx.Bitmap` 抓取当前编辑器画布区域：
  ```python
  dc = wx.ScreenDC()
  bmp = wx.Bitmap(w, h)
  mem = wx.MemoryDC(bmp)
  mem.Blit(0, 0, w, h, dc, x, y)
  mem.SelectObject(wx.NullBitmap)
  bmp.ConvertToImage().SaveFile(path, wx.BITMAP_TYPE_PNG)
  ```
- 画布窗口句柄：schematic 编辑器 / PCB 编辑器各取对应 wxWindow 的 `GetScreenPosition()+GetSize()`。
- OpenGL GAL 画布同样可抓到（按屏幕坐标抓取）。
- 截图保存为临时 PNG → base64 → 按第 1 节多模态格式注入消息，供 LLM 分析。

### 方案 B：KiCad plot 导出（备选/整板渲染）
- PCB：`pcbnew.PLOT_CONTROLLER` plot 到 SVG/PDF；schematic：eeschema plot 导出。
- 需要 PNG 时用 Pillow/cairosvg 转换（cairosvg 依赖 native cairo，三平台均有轮子；Pillow 为纯 wheel）。
- 适合不依赖 GUI 的场景或需要整板/整图导出。

### 建议
- 方案 A 为主（实时取景、所见即所得），方案 B 提供"整板导出"工具；两者输出统一为 base64 图片。

---

## 3. 支持解析 PDF 文档

### 方案
- **A. 文本抽取**：`pypdf`（纯 Python）/ `pdfplumber`（表格友好）/ `PyMuPDF`（fitz，速度快）。
- **B. 页面转图**：`PyMuPDF` 渲染每页为 PNG，按第 1 节多模态格式发送 —— 适合 datasheet 原理图、图表密集文档（纯文本抽取易乱）。
- **推荐组合**：先抽文本入上下文；当页数少、图/表多或文本乱时自动转图让多模态模型读。

### 运行位置
- 库安装进 `setup_plugin.sh/.bat/.ps1` 的 venv（`pymupdf`、`pdfplumber`）。
- 插件侧通过 `server_manager` 类似机制以子进程跑解析脚本，避免污染 KiCad 嵌入式 Python。

### 跨平台
- `pymupdf` / `pdfplumber` / `pypdf` 均有 Linux / macOS / Windows 支持（PyMuPDF 提供三平台 wheel）。

---

## 实施顺序建议

1. 图片上传（第 1 节）—— 打通多模态消息格式，第 2、3 节复用。
2. 截图（第 2 节 A 方案）—— 复用图片注入链路。
3. PDF 解析（第 3 节）—— 依赖 venv 与子进程机制。

## 风险 / 注意事项

- **token 与超时**：图片/多页 PDF 会显著增加输入体积，需限制尺寸与页数（如最多前 N 页）。
- **Vision 模型支持**：需在设置中提示所选模型必须支持图像输入。
- **Wayland 抓屏限制**：Linux Wayland 下 `wx.ScreenDC` 可能受限，需验证；备选使用 KiCad plot 导出或 `grim` 等工具。
- **KiCad 版本差异**：schematic plot / 新版 API 各版本接口不同，做兼容层。
- **测试**：三平台 CI 需覆盖截图与 PDF 解析（现有 `.github` 有 CI 可扩展）。
