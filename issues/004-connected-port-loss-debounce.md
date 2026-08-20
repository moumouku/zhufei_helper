# 004 - 已连接端口拔出去抖检测与提示

## Parent PRD

`docs/requirements/REQ-0002-serial-port-monitor.md`（功能需求 7、8；实施决策 1、4；测试决策 2）

## What to build

为 `PortMonitor` 增加连接端口跟踪（`set_connected` / `clear_connected`）：已连接的端口连续 2 次 `tick()` 缺失（约 2 秒）才触发 `on_lost`；判定期间端口重新出现则清零计数、恢复正常。MainWindow 收到 `on_lost` 后自动关闭连接、弹一次 `QMessageBox.warning`（「串口已拔出，连接已关闭」）、端口随差量更新从列表移除，UI 回到未打开状态。读取线程报错路径保持现状（立即关闭并提示，不受去抖影响）。

## Acceptance criteria

- [ ] 打开端口后拔掉：约 2 秒内调用 `controller.close()`、恰好弹一次 warning、端口从列表移除、UI 回到未打开状态
- [ ] 端口缺失 1 次后重新出现：不关闭、不弹窗、计数清零
- [ ] 读取线程报错仍立即关闭并提示（回归测试通过）
- [ ] `PortMonitor` 单元测试通过：连续 2 次缺失去抖、中途恢复、`set_connected`/`clear_connected` 跟踪

## Blocked by

- Blocked by `issues/001-port-list-polling-diff-update.md`

## User stories addressed

- User story 6
- User story 7
- User story 13
