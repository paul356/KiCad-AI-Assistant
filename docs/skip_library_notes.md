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

## 2. `ElementTemplate` 缺少 `hierarchical_label` 条目

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
- kicad-mcp 中的实现：`kcaa/tools/component_edit_tools.py`，函数 `add_label_to_schematic`、`list_labels_in_schematic`、`delete_label_from_schematic`
- KiCad `.kicad_sch` 格式中 `hierarchical_label` 实例：`(hierarchical_label "NAME" (shape input) (at x y angle) (effects ...) (uuid ...))`
