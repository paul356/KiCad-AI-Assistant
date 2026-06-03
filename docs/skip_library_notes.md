# skip 库使用笔记与已知限制

本文档记录在 kicad-mcp 开发过程中发现的 [skip](https://github.com/psychogenic/skip) 库的限制和变通方案，供未来向 skip 提交 PR 时参考。

---

## 1. `hierarchical_label` 缺少专用集合类

### 问题描述

skip 为 `label` 和 `global_label` 注册了专用集合类（`LabelCollection`、`GlobalLabelCollection`），这两个属性在 schematic 对象上**始终存在**，支持 `.new()` 方法创建新元素。

但 `hierarchical_label` **未注册**专用集合类，导致以下行为不一致：

| 情况 | `sch.label` / `sch.global_label` | `sch.hierarchical_label` |
|------|----------------------------------|--------------------------|
| 文件中 0 个 | 返回空集合 ✅ | `AttributeError` ❌ |
| 文件中 1 个 | 返回集合（可迭代）✅ | 返回单个 `ParsedValue`（不可迭代）❌ |
| 文件中 2+ 个 | 返回集合（可迭代）✅ | 返回可迭代结构 ✅ |

### 受影响的代码

- `skip/eeschema/schematic/schematic.py` 中的 `dedicated_collections_for` 字典
- `skip/eeschema/label.py` 中缺少 `HierarchicalLabelWrapper` 和 `HierarchicalLabelCollection` 类
- `skip/element_template.py` 中缺少 `hierarchical_label` 模板

### 变通方案（kicad-mcp 中的实现）

**创建**：通过 `sch.new_from_list()` 手动传入 sexp 列表：

```python
from sexpdata import Symbol
import uuid

hier_tmpl = [
    Symbol('hierarchical_label'), text,
    [Symbol('shape'), Symbol(shape)],
    [Symbol('at'), x, y, angle],
    [Symbol('effects'), [Symbol('font'), [Symbol('size'), 1.27, 1.27]],
     [Symbol('justify'), Symbol('left')]],
    [Symbol('uuid'), Symbol(str(uuid.uuid4()))]
]
lbl = sch.new_from_list(hier_tmpl)
```

**迭代**：使用如下辅助函数安全迭代：

```python
def _iter_schematic_labels(sch, attr_name):
    try:
        coll = getattr(sch, attr_name)
    except AttributeError:
        return []
    try:
        return list(coll)
    except TypeError:
        return [coll]  # 只有 1 个时 skip 返回单个 ParsedValue
```

### 建议的 PR 修复

1. 在 `label.py` 中添加：
   ```python
   class HierarchicalLabelWrapper(BaseLabelWrapper): ...
   class HierarchicalLabelCollection(BaseLabelCollection):
       def _new_instance(self):
           return HierarchicalLabelWrapper(
               self.parent.new_from_list(ElementTemplate['hierarchical_label']))
   ```
2. 在 `element_template.py` 中添加 `hierarchical_label` 模板
3. 在 `schematic.py` 的 `dedicated_collections_for` 和 `dedicated_wrapper_type_for` 中注册

---

## 2. 单元素时返回裸 `ParsedValue` 而非集合（通用问题）

### 问题描述

skip 的多个集合属性在只有 **1 个元素**时返回单个 `ParsedValue`，而有 2+ 个元素时返回可迭代集合。这不一致，需要调用方自行判断类型。

已在以下场景发现此问题：

- **`sch.hierarchical_label`**（见第 1 条）
- **`table.lib`**（sym-lib-table 文件中只有一个库条目时）

### 变通方案

```python
import skip.sexp.parser as _sp
raw = table.lib
libs = [raw] if isinstance(raw, _sp.ParsedValue) else raw
```

### 建议的 PR 修复

所有集合属性应统一返回列表或专用集合类型，而不是在单元素时"退化"为裸 `ParsedValue`。

---

## 3. 单引脚 symbol 的 `sym.pin` 迭代失败

### 问题描述

对于单引脚 symbol（如电源符号 VCC/GND、PWR_FLAG、TestPoint 等），`sym.pin` 迭代时 skip 不返回 `SymbolPin` 对象，而是退化为原始 `ParsedValue` 的子节点（引脚编号字符串和 UUID），导致 `pin.number` / `pin.location` 抛 `AttributeError`。

根本原因与第 2 条相同：单元素时 skip 不创建集合包装。

### 变通方案（`skip_helpers.py`）

先尝试正常路径，若无结果则 fallback 到手动读取 `lib_symbol` 中的引脚定义并自行做旋转/镜像变换：

```python
results = []
for pin in sym.pin:
    try:
        results.append(PinWorldCoords(str(pin.number), ...))
    except AttributeError:
        continue

if not results:
    # fallback: 从 sym.lib_symbol.pin 手动计算世界坐标
    ...
```

### 建议的 PR 修复

`sym.pin` 应始终返回 `SymbolPin` 对象的集合，无论引脚数量多少。

---

## 4. `SymbolPin.location` 使用库坐标系（非原理图坐标系）

### 问题描述

`SymbolPin.location.rotation` 返回的角度使用**库编辑器坐标系**：

- +Y 朝上（库坐标）
- 角度为逆时针（CCW）
- 角度方向是从导线出口指向元件体（即 stub 方向，非导线出方向）

而原理图坐标系要求：

- +Y 朝下（屏幕坐标）
- 角度为顺时针（CW）
- 角度方向为导线出口方向（即元件体指向导线）

### 变换公式

```python
angle_schematic = (540 - angle_lib) % 360
```

验证：
- J3 右侧引脚（sym 旋转 0°）：lib=180° → `(540-180)%360 = 0°`（→ 右）✓
- R1 pin1（sym 旋转 180°）：lib=90° → `(540-90)%360 = 90°`（↓ 下）✓

### 建议的 PR 修复

`SymbolPin.location` 应提供原理图坐标系下的角度，或提供一个 `schematic_rotation` 属性。

---

## 5. `ElementTemplate` 缺少 `hierarchical_label` 条目

### 问题描述

`skip/element_template.py` 中的 `ElementTemplate` 字典只有：
- `wire`
- `global_label`
- `label`
- `text`
- `junction`

缺少 `hierarchical_label`，导致无法通过标准方式创建。

### 建议的 PR 修复

在 `ElementTemplate` 中添加：

```python
'hierarchical_label': [
    Symbol('hierarchical_label'), 'HLABEL',
    [Symbol('shape'), Symbol('input')],
    [Symbol('at'), 0, 0, 0],
    [Symbol('effects'), [Symbol('font'), [Symbol('size'), 1.27, 1.27]],
     [Symbol('justify'), Symbol('left')]],
    [Symbol('uuid'), Symbol('HIERUUID')]
],
```

---

## 参考

- skip 仓库：https://github.com/psychogenic/skip
- kicad-mcp 中的实现：
  - `kcaa/tools/component_edit_tools.py` — `add_label_to_schematic`、`list_labels_in_schematic`、`delete_label_from_schematic`
  - `kcaa/utils/skip_helpers.py` — `sym_pin_world_coords`（单引脚 fallback + 坐标系变换）
  - `kcaa/utils/symbol_index_reader.py` — `table.lib` 单元素兼容处理
  - `kcaa/utils/skip_compat.py` — UTF-8 编码兼容（Windows）
- KiCad `.kicad_sch` 格式中 `hierarchical_label` 实例：`(hierarchical_label "NAME" (shape input) (at x y angle) (effects ...) (uuid ...))`
