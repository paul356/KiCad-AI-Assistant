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

而 kcaa 需要的 wire-exit 方向（元件体 → 导线出口）使用**文件 CCW 约定**
（屏幕 +Y 朝下，0=right, 90=up, 180=left, 270=down）。

### 变换公式

kcaa 在 `skip_helpers._pin_world_from_lib` 中取 stub 反向（不依赖 skip
的角度输出）：

```python
angle_exit = (angle_lib + 180) % 360
```

验证：
- J3 右侧引脚（sym 旋转 0°）：lib=180° → `(180+180)%360 = 0°`（→ 右）✓
- R1 pin1（sym 旋转 180°）：lib=90° → `(90+180)%360 = 270°`（↓ 下）✓

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

## 6. `AtValue.rotate90degrees` 旋转方向与 KiCad 相反

### 问题描述

`skip/at_location.py` 的 `AtValue.rotate90degrees()` 每步执行：

```python
new_x = self.y
new_y = -1 * self.x    # (x, y) → (y, −x)
```

该矩阵在库坐标系（+Y 向上、数学 CCW）中是 **顺时针** 90°；而 KiCad 的符号/引脚旋转是 **逆时针**（CCW，`(x,y) → (−y, x)`）。对 **90°/270°** 放置的符号，引脚世界坐标系统性错误；**0°/180°** 时两种方向等价（两遍 90° 结果相同），因此只覆盖 0°/180° 的验证与测试不会暴露此问题。

### 影响范围

- `skip/eeschema/schematic/symbol.py` 的 `SymbolPin.location`（主路径：`while ... rotate90degrees()` 循环后 `par.x + rel.x, par.y - rel.y`）
- `kcaa/utils/skip_helpers.py` fallback 的同一循环（单引脚电源符号路径）

两处同源，误差均为 `(2·y_lib, 2·x_lib)`。**角度不受影响**：`rotation` 字段只是数值累加（每步 `+90`），两种方向的步数相同，因此 `(540−angle)%360` 的输出仍然正确；错误的是位置。

### 实测证据

ODrive v3 工程 `two_ax_PCB.kicad_sch`，U2A（`unit 1` @ (101.6, 93.8022, 90°)）pin52 = PC11：

- 库定义：`(-78.74, 5.08, 270°)`
- KiCad 真实世界坐标（磁盘导线精确匹配）：**(96.52, 172.54)**
- skip 输出：**(106.68, 15.0622)**，偏差 `(+10.16, −157.48) = (2·5.08, 2·(−78.74))`

验证方法：对该图全部 64 个引脚（unit A 55 + unit B 9），用 4 种候选 90° 变换计算世界坐标并与磁盘全部导线端点集合比对，只有 KiCad 正确矩阵（库 CCW90 再 y-flip，净 `(−y, −x)`）命中 64/64，其余候选 0~1 命中。

### 修复方向

- **不要用 `rotate90degrees()` 计算位置**。kcaa 已有正确的参考实现：`kcaa/utils/symbol_geometry.py::_rotate_lib_point`（库 y-up CCW 四方向分支表），`lib_bbox_to_world` 的完整四步（mirror → CCW 旋转 → 平移 → y-flip）与之配套。
- `sym_pin_world_coords` 两条路径统一自算位置（经 `_rotate_lib_point`），角度逻辑（`(540−angle)%360`）保持不变。
- 建议向 skip 上游提 PR：修正 `rotate90degrees` 为 CCW，或在 `SymbolPin.location` 内改用正确矩阵。

---

## 参考

- skip 仓库：https://github.com/psychogenic/skip
- kcaa 坐标系约定总览：`docs/coordinate-systems.md`
- kicad-mcp 中的实现：
  - `kcaa/tools/symbol_edit_tools.py` — `add_label_to_schematic`、`list_labels_in_schematic`、`delete_label_from_schematic`
  - `kcaa/utils/skip_helpers.py` — `sym_pin_world_coords`（单引脚 fallback + 坐标系变换）
  - `kcaa/utils/symbol_index_reader.py` — `table.lib` 单元素兼容处理
  - `kcaa/utils/skip_compat.py` — UTF-8 编码兼容（Windows）
- KiCad `.kicad_sch` 格式中 `hierarchical_label` 实例：`(hierarchical_label "NAME" (shape input) (at x y angle) (effects ...) (uuid ...))`
