# 003 - 端口移除策略

## Parent PRD

`docs/requirements/REQ-0002-serial-port-monitor.md`（功能需求 5、6；实施决策 3）

## What to build

`on_removed` 事件的 UI 策略：未选中的端口被拔出时静默从列表移除、不弹窗；已选中但未连接的端口被拔出时从列表移除，且不自动改选剩余端口、选中项留空（之后如有新端口插入，由切片 002 的规则 A 接管选中）。

## Acceptance criteria

- [ ] 拔掉未选中端口：从列表移除，无弹窗
- [ ] 拔掉已选中（未连接）端口：从列表移除，选中留空，不自动补选
- [ ] 之后插入新端口时按规则 A 选中（与切片 002 的交互）
- [ ] 窗口级测试覆盖两种移除场景

## Blocked by

- Blocked by `issues/001-port-list-polling-diff-update.md`

## User stories addressed

- User story 8
- User story 9
