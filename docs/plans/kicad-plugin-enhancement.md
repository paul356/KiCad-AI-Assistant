# kicad_plugin Enhancement Plan

## Status: Image upload complete, PDF parsing done

## Goals

Add two capabilities to kicad_plugin (KiCad AI Assistant plugin), requiring support for **Linux / Windows / macOS**:

1. Support uploading/pasting images (user shows images to LLM, including pasting screenshots from clipboard)
2. Support parsing PDF documents

> Screenshot capture is not implemented inside the plugin: users take screenshots themselves and **Ctrl+V** paste into the chat box. Both schematic and PCB screenshots use this path. See "Not Considered" section for details.

## Existing Architecture (Relevant Parts)

```
KiCad GUI (wxPython)
  └─ kicad_plugin/
       ├─ ui/panel.py        ← Chat panel (wx.Frame, TextCtrl input)
       ├─ llm_client.py      ← LLMClient, direct connection to OpenAI/Anthropic/Ollama, multimodal messages built here
       ├─ server_manager.py  ← Starts kcaa MCP server (separate venv subprocess)
       └─ setup_plugin.sh/.bat/.ps1 ← Creates venv and installs dependencies
```

Key conclusions:
- The plugin runs in KiCad's embedded wx environment, `wx` is available → screenshot/image UI needs no additional dependencies.
- LLM message construction is centralized in `llm_client.py` (`_build_anthropic_messages` / `_stream_openai` / `_stream_ollama` / `_call_*`); adding multimodal fields requires per-provider handling.
- KiCad's embedded Python has limited third-party package installation; pure Python / cross-platform wheel libraries (PyMuPDF, pdfplumber, pypdf) should be installed in the venv created by `setup_plugin*.sh/.bat/.ps1`, and called via subprocess from the plugin side.

---

## 1. Support Uploading / Pasting Images

### Approach
- **UI (panel.py)**: Add an "Add Image" button next to the input box (`wx.FileDialog` filtering png/jpg), after selection load with `wx.Image` and display thumbnail; support multiple images at once, sent together with the next message.
- **Clipboard Paste (Ctrl+V)**: When Ctrl+V is pressed in the input box, if the system clipboard contains a bitmap (`wx.DF_BITMAP`), paste as image (save to temporary PNG and add to attachment bar), otherwise fall back to normal text paste; also a "Paste" button (`wx.ART_PASTE`) triggers the same logic.
- **Attachment Thumbnail Bar**: Selected images show as 48×48 thumbnails, click to remove; includes count label and "✕ clear" button to clear all.
- **Message Construction (llm_client.py)**, per provider:
  - OpenAI: content becomes an array
    `[{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]`
  - Anthropic:
    `[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"<base64>"}}]`
  - Ollama: request body adds `images: ["<base64>"]`, messages use array format.
- **Pre-send Compression**: Scale longest edge to ~1024px to control size, avoid token/timeout issues; unified `wx.Image.Scale` → `wx.Image.ConvertToBitmap` → encode.
- **Session Saving**: Strip `image_url` blocks before writing session files (to avoid base64 bloating files); image context is not preserved after loading a session.
- **Model Requirements**: Requires vision capability (gpt-4o / claude-3.x / llava, etc.), prompted in settings.

### Cross-Platform
- The entire chain uses only Python standard library (base64) + wx → naturally supports all three platforms.

---

## 2. Support Parsing PDF Documents

### Approach A (Main Path): Text Extraction + Page Number Annotation
- `pypdf` (pure Python) / `pdfplumber` (table-friendly) / `PyMuPDF` (fitz, fast).
- **Annotate page number before each text segment** (e.g., `[P3]`), so the LLM can reference specific pages when answering.

### Approach B (User-On-Demand): Page-to-Image Conversion
- `PyMuPDF` renders user-specified pages as PNG, sent via the multimodal format from Section 1 — suitable for datasheet schematics, chart-dense pages.
- **User decides**: After reading the text summary, if information is insufficient (charts, schematics), the user actively requests rendering specific pages, rather than the system auto-converting.

### Recommended Combination (Simplified, finalized 2026-08-04)
- **Default flow**: Extract all text (with page numbers) into context.
- **Supplementary flow**: User decides they need to see images → user requests rendering specific pages (or currently open PDF page) → Approach B converts to image and sends to LLM.
- No LLM page selection / auto-conversion logic, keeping implementation simple.

### Execution Location
- Libraries installed in the venv from `setup_plugin.sh/.bat/.ps1` (`pymupdf`, `pdfplumber`).
- Plugin side runs the extraction script as a subprocess via a mechanism similar to `server_manager`, to avoid polluting KiCad's embedded Python.

### Cross-Platform
- `pymupdf` / `pdfplumber` / `pypdf` all support Linux / macOS / Windows (PyMuPDF provides wheels for all three platforms).

---

## Suggested Implementation Order

1. Image upload/paste (Section 1) — Establish multimodal message format, reused by PDF. ✅ Complete
2. PDF parsing (Section 2) — Main path uses Approach A (extract text with page numbers); supplementary uses Approach B (user-specified page to image). Depends on venv and subprocess mechanism.

## Risks / Notes

- **Tokens and Timeout**: Images/multi-page PDFs significantly increase input size; need to limit size and page count (e.g., max first N pages).
- **Vision Model Support**: Need to prompt in settings that the selected model must support image input.
- **Clipboard Format**: Ctrl+V depends on system clipboard bitmap format (wx.DF_BITMAP); X11/Wayland/Windows/macOS need verification; when clipboard only has text, falls back to normal paste.
- **Testing**: Three-platform CI needs to cover image paste and PDF parsing (existing `.github` has CI that can be extended).

---

## Final Plan and Goals (Finalized 2026-08-05)

### Goals
Add two capabilities to kicad_plugin (KiCad AI Assistant plugin), supporting Linux / Windows / macOS:
1. Users upload/paste images for LLM to see (including Ctrl+V pasting system screenshots)
2. Parse PDF documents

> Screenshots are not implemented inside the plugin: users take screenshots themselves and Ctrl+V paste into the chat box.

### Final Plan

| Capability | Approach | Description |
|---|---|---|
| Image Upload | **Implemented** (2026-08-04) | Panel has "Add Image/Paste" button, thumbnail bar, Ctrl+V smart paste (pastes as image when clipboard has bitmap, otherwise pastes text); compress ≤1024px → base64; OpenAI/Anthropic/Ollama three-provider format conversion; session save strips base64 |
| Screenshot (schematic/PCB) | Not implemented in plugin | User takes system screenshot → Ctrl+V paste → reuses image pipeline |
| PDF Parsing | **Main path**: extract all text (with page numbers `[P3]`) into context; **Supplementary**: user requests rendering specific pages to image when text is insufficient | No LLM page selection / auto-conversion, keeping it simple |

### Iteration Path (Land first, enhance based on results)
1. **PDF v1**: Only "extract text with page numbers" (PyMuPDF, venv subprocess). Validate text extraction usability for datasheets.
2. **PDF v2** (depending on v1 results): User-specified page to image → reuse image injection pipeline.
3. **PDF v3** (optional): If text is messy/chart-heavy, consider qwen3.8-max native PDF understanding (Approach C) or LLM page selection (see "Not Considered" section).

### Completed Code Changes (ideas branch)
- `kicad_plugin/llm_client.py`: `run()` supports `images` parameter; OpenAI array format + Anthropic/Ollama conversion; `_maybe_compact` compatible with array content.
- `kicad_plugin/ui/panel.py`: Attachment UI (add/paste/thumbnail/clear); Ctrl+V smart paste; compression encoding; session save strips images.
- 44 `test_llm_client` unit tests pass; ruff lint/format pass.

---

## Not Considered (2026-08-05)

The following capabilities were researched but decided not to implement. Research conclusions are retained for future reference.

### N1. In-Plugin Schematic / PCB Screenshot

#### Approach A: wx Screen Capture (PCB editor only, real-time)
- The plugin runs inside the KiCad GUI, use `wx.ScreenDC` + `wx.MemoryDC` + `wx.Bitmap` to capture the current editor canvas area:
  ```python
  dc = wx.ScreenDC()
  bmp = wx.Bitmap(w, h)
  mem = wx.MemoryDC(bmp)
  mem.Blit(0, 0, w, h, dc, x, y)
  mem.SelectObject(wx.NullBitmap)
  bmp.ConvertToImage().SaveFile(path, wx.BITMAP_TYPE_PNG)
  ```
- Principle: `wx.ScreenDC` uses OS-level API to capture screen pixels (X11 `XGetImage` / Windows `BitBlt` / macOS `CGWindowListCreateImage`), **capture works regardless of which process the window belongs to**.

#### ⚠️ Challenges (2026-08-04 research conclusion)
- **Plugin only runs in PCB editor process**: `kicad_plugin/__init__.py` has `_ActionPluginBase = pcbnew.ActionPlugin`.
- **KiCad's schematic editor (eeschema) is a separate process**: `wx.GetTopLevelWindows()` can only enumerate **current process** windows → **Python side physically cannot access schematic editor's wx objects** (not difficult, just cross-process unreachable).
- To capture schematic windows, must query window coordinates across processes: Windows `EnumWindows` / X11 `xwininfo` / macOS `CGWindowListCopyWindowInfo`, implementations differ across platforms, high maintenance cost.
- **Conclusion: Approach A only works for PCB editor (same process, can get `GetScreenPosition()`); schematic real-time screenshot uses Approach B.**
- Also: Linux **Wayland** has limited `wx.ScreenDC` screen capture (X11 works fine), needs verification.

#### Approach B: kicad-cli Export
- Instead of going through KiCad GUI, use a separate process `kicad-cli` to export files → **no cross-process issue, no GUI dependency**.
- PCB: `kicad-cli pcb export svg --output out.svg board.kicad_pcb` (KiCad 7+).
- Schematic: `kicad-cli sch export svg --output out.svg sheet.kicad_sch` (KiCad 7+, `--recursive` can export hierarchical sub-sheets).

#### ✅ Feasibility (2026-08-04 research conclusion: most of the pipeline already exists)
- **PCB pipeline implemented**: `kcaa/tools/export_tools.py`'s `generate_thumbnail_with_cli` already uses `kicad-cli pcb export svg` to generate PCB image and return to LLM (fastmcp `Image`).
- **kicad-cli lookup encapsulated**: `kcaa/utils/kicad_cli.py` (three-platform path detection + caching + `KICAD_CLI_PATH` environment variable); secure subprocess wrapper in `kcaa/utils/secure_subprocess.py`.
- **Schematic subcommand verified**: `kcaa/tools/bom_tools.py` already uses `kicad-cli sch export bom` → sch pipeline works.
- Test fixtures available: `tests/**/fixtures/` has `.kicad_pcb` / `.kicad_sch`, can write integration tests in environments with KiCad.

#### ⚠️ Challenges
- **kicad-cli does not directly output PNG** (`pcb/sch export` only supports svg/pdf/dxf, etc.). **SVG is not a format natively supported by vision models** (OpenAI/Anthropic/Ollama only accept png/jpeg/webp/gif) → must convert: Pillow (pure wheel) or cairosvg (native cairo).
- **Existing `generate_thumbnail_with_cli` directly returns `format="svg"`, LLM side may reject** → need to add SVG→PNG conversion step.
- Poor real-time: exports the entire board/sheet, not the current view ("current view" still requires Approach A, PCB only).
- Local machine (2026-08-04) has no kicad-cli, not yet tested; need to verify `sch export svg` and conversion pipeline in an environment with KiCad.

#### Reason for Not Considering
- Approach A is cross-process infeasible (schematic); Approach B requires additional SVG→PNG conversion maintenance, low cost-effectiveness.
- **Alternative path**: User uses system screenshot tool to capture schematic / PCB screen → **Ctrl+V** paste in plugin input box → goes through Section 1 image injection pipeline to send to LLM. This functionality is already covered by the image paste implementation in Section 1.

### N2. PDF Approach C: Bailian + qwen3.8-max Native PDF Understanding

- **qwen3.8-max natively supports PDF** (2026-08-04 official documentation confirmed, `https://help.aliyun.com/zh/model-studio/pdf-understanding`), uses OpenAI-compatible interface, content array adds `type: "file"` block:
  ```json
  {"type": "file", "file": {"file_url": "https://.../doc.pdf"}}            // URL, either/or
  // Or Base64:
  {"type": "file", "file": {"file_data": "data:application/pdf;base64,xxx", "filename": "report.pdf"}}  // filename required when using file_data
  ```
- **Limitations**: Single file ≤150MB, ≤500 pages; first packet timeout up to 300s (streaming recommended); **only North China 2 (Beijing) region**; does not support Responses API.
- **Billing** (two-stage pipeline):
  1. Document parsing fee: ¥0.02/page (platform first does layout analysis/OCR, splits into text + images);
  2. Model call fee: parsed **text charged as text tokens, images as image tokens** for input, billed at model's standard input price.
- **Pros**: No need to install PyMuPDF/pdfplumber in the plugin for preprocessing; text goes through text tokens (cheaper), only charts go through image tokens.
- **Cons**: Not a universal capability (only Bailian North China 2 + qwen3.8-max); base_url needs WorkspaceId (`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`).

#### Reason for Not Considering
- Not a universal capability, only Bailian North China 2 + qwen3.8-max; first use universal Approach A+B, consider based on results.

### N3. PDF Image-Text Split Correspondence Issue

- Approach C / Approach A+B split text and images, **intra-page image-text relative position is lost** (the model doesn't know whether image A or text B is above/below on the page).
- **Preserved**: page order, caption/title text (captions are text, follow the text flow).
- **Impact by scenario**: Text-heavy (reports/papers) minimal impact; strong image-text coupling (datasheet schematics + pin tables + timing diagrams) moderate impact — but image internal information (pin names, values) can be read by the model itself, doesn't depend on coordinate correspondence, and captions can cross-validate.
- **Strong layout scenarios**: Use Approach B full-page-to-image for fidelity, at the cost of full-page image token billing (more expensive).

#### Reason for Not Considering
- First implement Approach A (extract text with page numbers), see actual results before deciding whether full-page-to-image fidelity is needed.

### N4. PDF Cost Comparison and Image Token Calculation

| Approach | Parsing Cost | Input Token Cost | Universality |
|---|---|---|---|
| Bailian native PDF (Approach C) | ¥0.02/page | Text as text tokens, charts as image tokens | Only North China 2 + qwen3.8-max |
| Local image conversion (Approach B) | 0 (local rendering) | Full page as image tokens (expensive) | Any vision model |
| Local text extraction (Approach A) | 0 | Text tokens only (cheapest, but loses charts) | Any model |
- Image token calculation (Bailian vision model): **per image ≈ ⌈h×w/1024⌉ + 2** (1024×1024 ≈ 1026 tokens, linear growth).
- Mixed image-text documents are more cost-effective with Approach C; pure-image PDFs have similar cost for Approach B/C.
