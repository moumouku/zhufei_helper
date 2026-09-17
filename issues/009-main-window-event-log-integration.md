# 009 - 主窗口接收事件与日志集成

## Parent PRD

`docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md`（第 1、5.1、6、7、7.3、7.4、9、10.1、11、15.3、17 节；实施决策 3-4；测试决策 1、5；验收标准 9-16、18）

## What to build

在 `paimon_assistant/main_window.py` 中协调 controller、事件渲染器和日志服务，形成完整的“事件队列 -> 显示 -> 日志”链路：

- 主线程批量消费 `ReceivedEvent`，按事件顺序加入内存历史并渲染；显示渲染使用当前模式、编码和时间戳开关。每个事件在主线程中最多调用日志服务一次，正常情况下同步写入并刷新。
- 处理顺序必须保证日志故障不会丢失显示：先将事件纳入内存历史并完成显示更新，再捕获 `ReceiveLogError` 或失败返回；首次故障显示指定红色提示：`日志写入失败，请检查磁盘空间或权限`。reader、分帧、事件生成和队列消费不因日志状态停止。
- 日志服务熔断后，主窗口不缓存失败事件、不安排重试，也不反复触发文件操作；后续事件仍继续进入历史和显示。
- 允许测试注入日志服务和目录打开器；不依赖真实 `%LOCALAPPDATA%`、Windows 文件资源管理器或磁盘故障才能测试主窗口行为。
- 主界面提供“显示时间戳” `QCheckBox`（对象名 `timestamp_checkbox`）、“日志目录” `QPushButton`（对象名 `log_dir_button`）和日志错误红色 `QLabel`（对象名 `log_error_label`）。日志失败提示持续显示到本次运行结束或后续明确状态替换，不使用模态弹窗。
- 点击“日志目录”时先调用日志服务 `ensure_directory()`，再使用注入的 Windows 目录打开器打开目录本身；创建或打开失败时在 `log_error_label` 显示包含失败原因的明确错误，但不影响接收、显示或串口操作。
- 超长帧诊断由独立诊断队列消费并显示到红色非模态 `QLabel`（对象名 `receive_error_label`），固定提示文本为“接收帧超过 1 MiB，已丢弃”；主窗口不从裸字节自行判断帧边界。
- 保留已有串口错误队列、端口热插拔、接收区自动滚动和发送区行为。发送数据只调用 controller 写原始字节，不进入 `ReceivedEvent` 和 `RX` 日志；REQ-0002 的控制器内部接收改造不改变其外部热插拔行为。
- 连接关闭、读异常或重新打开后继续遵守会话隔离。

## Acceptance criteria

- [ ] 主窗口消费的是 `ReceivedEvent`，事件按队列顺序显示并按同一顺序进入日志服务
- [ ] 时间戳显示开关、文本/HEX模式和编码切换不会改变事件历史或日志内容
- [ ] 日志写入在正常情况下及时完成；事件先进入历史和显示，显示区仍能继续处理后续事件
- [ ] 日志服务失败后出现指定红色 `log_error_label` 提示，并且串口接收、事件生成、队列消费、接收显示和其他串口操作继续工作
- [ ] 日志失败后没有失败事件内存缓存或重试行为，后续写入不再反复触发文件操作
- [ ] 超长帧诊断通过独立队列到达红色非模态 `receive_error_label`，不阻断正常显示
- [ ] “日志目录”入口创建并打开日志目录，而不是打开单个日期文件
- [ ] 日志目录打开失败有明确 UI 错误，且不关闭连接、不丢接收数据
- [ ] 主窗口 offscreen 测试覆盖事件消费、日志失败隔离、日志目录成功/失败和发送行为回归
- [ ] 既有端口监测、连接错误、模式切换、自动滚动和串口收发测试保持通过；接收测试已迁移到 `ReceivedEvent` 契约

## Blocked by

- Blocked by `issues/006-serial-session-event-queue.md`
- Blocked by `issues/007-received-event-rendering.md`
- Blocked by `issues/008-receive-log-service-and-retention.md`

## Requirements addressed

- §1 完整接收链路
- §6 接收区显示与 UI 开关
- §7.3 日志写入时机与故障隔离
- §9 日志目录入口
- §11 发送行为回归
- §15.3 主线程日志写入及熔断
- §16.1、§16.5 外部行为和主窗口测试决策
