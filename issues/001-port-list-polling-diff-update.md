# 001 - 端口列表轮询与差量更新

## Parent PRD

`docs/requirements/REQ-0002-serial-port-monitor.md`（功能需求 1、2、9；实施决策 1、3；测试决策 2）

## What to build

实现端口列表监测主干：新增纯 Python 的 `PortMonitor` 模块（无 Qt 依赖），`tick()` 轮询注入的 port lister 并与上次快照做差量，触发 `on_added` / `on_removed` 事件；MainWindow 用 1000ms `QTimer` 驱动 `tick()`；端口下拉框按差量更新（只增删变化的项，不清空重建）；手动「刷新」按钮与轮询共用同一差量更新路径；连接打开期间列表数据仍持续更新（下拉框保持禁用）。

## Acceptance criteria

- [ ] 启动枚举行为不变（既有测试回归通过）：端口列出、默认选中第一个
- [ ] 新插入的端口不点「刷新」也在约 1 秒内出现在列表中
- [ ] 被拔掉的端口约 1 秒内从列表消失
- [ ] 连接打开期间列表数据仍更新，端口下拉框保持禁用
- [ ] 手动「刷新」按钮仍可重新枚举（既有测试回归通过），且走同一差量路径
- [ ] `PortMonitor` 单元测试通过：差量增/删事件、相同快照不产生事件、事件顺序
- [ ] 完整测试套件通过（新增 + 回归）

## Blocked by

None - can start immediately

## User stories addressed

- User story 1
- User story 2
- User story 11
- User story 12
