# 002 - 新端口自动选中规则

## Parent PRD

`docs/requirements/REQ-0002-serial-port-monitor.md`（功能需求 3、4；实施决策 3）

## What to build

在 `on_added` 事件上应用已确认的规则 A：当前选中为空、或当前选中项已不在列表中（已失效）→ 自动选中新端口（多个新端口同时出现时选枚举顺序的第一个）；当前选中有效（包括启动时程序默认选中的端口）→ 只加入列表，绝不改变选择。监测绝不自动打开任何端口。

## Acceptance criteria

- [ ] 列表为空或当前选中已失效时，新端口被自动选中
- [ ] 当前选中有效（含启动默认选中）时，新端口只加入列表、选择不变
- [ ] 多个新端口同时出现时，选中枚举顺序的第一个
- [ ] 任何情况下监测都不会自动打开端口
- [ ] 窗口级测试覆盖上述三种场景

## Blocked by

- Blocked by `issues/001-port-list-polling-diff-update.md`

## User stories addressed

- User story 3
- User story 4
- User story 5
- User story 10
