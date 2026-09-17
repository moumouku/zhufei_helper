# 006 - 串口会话接收事件队列

## Parent PRD

`docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md`（第 1、4.2、5、5.1、10、10.1、11、12 节；实施决策 1、2、5；验收标准 1-8、17、18）

## What to build

改造 `paimon_assistant/serial_controller.py` 的 reader 链路，使串口线程向界面发布完整 `ReceivedEvent`，不再把裸读取块直接交给主窗口重新分帧。允许修改 `SerialController` 的内部接收实现，但必须保持 REQ-0002 的端口监测、连接关闭和用户可观察行为不变：

- 每次成功 `open()` 创建独立的分帧器、接收事件队列和 reader 会话；reader 对每个读取块调用分帧器，并按顺序把生成的事件放入 `received_queue`。
- `received_queue` 只允许放入 `ReceivedEvent`，不再放入裸 `bytes`；分帧诊断放入独立的线程安全 `diagnostic_queue`，运行时错误仍进入 `error_queue`。
- 连接关闭、reader 读异常、写失败或重新打开新连接时，旧会话的未完成尾部、溢出状态和排队事件不得泄漏到新会话；旧 reader 即使延迟返回也不能向新会话队列发布数据。
- 提供 `reset_receive_session()`。该操作在会话锁内递增会话代次、重置当前分帧器、替换 `received_queue` 和 `diagnostic_queue`，并丢弃旧队列对象中的待处理项目；reader 只在同一锁保护下、确认代次仍有效后发布分帧结果。
- 清空的会话代次切换点是“清空前数据”和“清空后数据”的唯一线性化点：重置前的尾部、溢出状态和旧事件全部丢弃，重置后到达的数据使用新分帧器和新队列正常入队。
- 保留现有错误队列和 reader 生命周期语义；reader 线程只负责串口读取、分帧和事件入队，不执行文件 I/O，不因日志服务状态改变。
- 既有接收测试应迁移到 `ReceivedEvent` 契约，不增加裸字节双轨兼容路径。`write()` 继续转发调用方提供的原始字节，不自动追加 `0D 0A`，发送数据不进入接收事件队列。

## Acceptance criteria

- [ ] fake serial 读取 `A\r`、再读取 `\n` 时，`received_queue` 得到一个正确的 `ReceivedEvent`
- [ ] 一次读取包含多帧时，队列按帧顺序提供独立事件，而不是裸字节块
- [ ] 空帧和跨读取块帧通过 controller 后仍保留正确的 `payload`、`raw_frame` 和时间戳
- [ ] 关闭连接或 reader 读异常后，未完成尾部和溢出状态被丢弃
- [ ] 关闭后立即重新打开新连接时，旧 reader 延迟返回的数据不会进入新连接队列
- [ ] `received_queue` 只提供 `ReceivedEvent`，分帧诊断进入独立 `diagnostic_queue`
- [ ] 会话重置能按会话锁和代次原子地丢弃点击时的旧事件、未完成解析状态和溢出状态；重置后到达的数据可正常生成事件
- [ ] 旧 reader 延迟返回时不能向新会话队列发布事件
- [ ] reader 线程不执行日志或其他文件操作，日志不可用不会阻塞串口读取
- [ ] 发送原始字节不追加 `\r\n`，不生成 `ReceivedEvent`，不进入 `RX` 接收链路
- [ ] serial controller 单元/回归测试覆盖独立会话队列、关闭/异常隔离、事件顺序和发送行为
- [ ] 既有打开、关闭、读写错误和 reader 生命周期测试保持通过；原有裸字节接收测试已迁移为事件契约

## Blocked by

- Blocked by `issues/005-receive-framing-and-events.md`

## Requirements addressed

- §1 端到端接收链路中的“接收 -> 分帧 -> 打包事件”
- §4.2 跨读取块、多帧、尾部清理和空帧
- §5 接收事件协议
- §10 清空时的解析器和事件队列边界
- §11 发送行为
- §15.1 会话级分帧器和事件队列
- §16.6 独立会话队列测试决策
