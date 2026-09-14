# 布线算法重构方案

## 状态：已完成

## 改动内容

### 思路变化

**旧方案**：
1. 找 `near_A`（pad 附近最近自由格点）→ 复杂出口候选 8 方向选择 → 插入路径
2. `_connect_pad`、`_exit_candidates`、`_block_pad_area`、`_find_closest_free` 等大量辅助函数

**新方案**：
1. A\* 直接以 pad 中心为起终点
2. pad 自身铜皮不作为障碍物（同 net 过滤），路径从 pad 内部穿出
3. 后处理结束后用精确 pad 中心坐标替换路径端点
4. 重新 snap + validate 保证 0/45/90° 和 clearance

### 删除的函数
- `_exit_candidates()` — 不再需要固定候选点
- `_block_pad_area()` — 不再需要 block 格点
- `_find_closest_free()` — 不再需要 near_A
- `_connect_pad()` — 不再需要出口候选
- `_pad_exit_points()` — 死代码
- `_filter_exits()` — 死代码

### 新增的函数
- `_align_path_endpoints()` — 用精确 pad 中心替换路径端点，再 snap/validate 收尾

### 简化的 `auto_route_pair`
- 去掉了 `_temp_grid` 构建
- 去掉了 `near_A`/`near_B` 查找
- 去掉了 `_connect_pad` + exit 插入
- A\* 直接 `pad_a_xy` → `pad_b_xy`
- 后处理后调用 `_align_path_endpoints` 收尾

### 下一步可优化
- 网格划分时对齐 pad 中心坐标，省去端点替换步骤
