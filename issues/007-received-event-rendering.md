# 007 - 接收事件渲染与时间戳显示

## Parent PRD

`docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md`（第 5、5.1、6、7.3 节；实施决策 4；测试决策 3；验收标准 9、10）

## What to build

实现无 Qt 依赖的接收事件渲染逻辑，并将 `MainWindow` 的接收显示从裸字节累积改为完整事件历史：

- 文本模式只渲染事件 `payload`，使用当前 UTF-8 或 GBK 解码；HEX 模式渲染完整 `raw_frame`，每字节为大写两位十六进制并以空格分隔。
- 每个完整事件在显示结果中形成自己的事件分隔换行；文本 `payload` 内部已有的控制字节按当前解码结果保留，渲染器在事件记录后追加事件分隔换行。未完成帧和超长帧不会进入渲染历史。
- 按 `received_at_ms` 转换本地时间，格式为 `HH:mm:ss.SSS`；时间戳开启时使用 `[HH:mm:ss.SSS] ` 前缀，关闭时只移除前缀，不改变事件数据。
- 增加“显示时间戳”二值开关，控件为 `QCheckBox`，对象名为 `timestamp_checkbox`；启动默认开启且不持久化。切换开关、接收模式或编码时，基于内存中的完整事件重新渲染全部历史。
- 内存保存事件的原始字节和时间戳，不保存依赖当前模式/编码生成的显示字符串；渲染不得修改事件或原始字节。
- `MainWindow` 提供对象名为 `receive_error_label` 的红色 `QLabel` 显示超长帧的一次性诊断，固定提示文本为“接收帧超过 1 MiB，已丢弃”；不弹 `QMessageBox`，不影响正常接收显示。
- 保留已有接收区自动滚动和模式切换体验，渲染器本身不引入 Qt 依赖，方便用 fake 时间和纯单元测试验证。

## Acceptance criteria

- [ ] 文本模式显示 `payload`，不显示末尾 `\r\n`；HEX 模式显示完整 `raw_frame`，包含 `0D 0A`
- [ ] HEX 输出始终为大写、两位一组、空格分隔
- [ ] 每个事件都有独立时间戳前缀和事件分隔换行，时间格式严格为 `HH:mm:ss.SSS`
- [ ] 时间戳开关启动默认开启；关闭只改变显示前缀，不改变事件历史、事件字节或日志输入
- [ ] 切换文本/HEX、UTF-8/GBK 或时间戳开关后，全部已完成事件按新设置正确重绘
- [ ] 原始字节、事件顺序和时间戳在任意显示切换后保持不变
- [ ] 未完成帧和超长帧不显示
- [ ] 接收区追加事件及历史重绘后保持原有末尾可见行为
- [ ] 超长帧诊断进入红色非模态 `receive_error_label`，同一段溢出只显示一次，重置后可再次显示
- [ ] 事件渲染单元测试覆盖文本、HEX、UTF-8/GBK、时间戳开关、重绘、事件分隔换行和字节不可变性
- [ ] MainWindow offscreen 测试覆盖事件队列消费和显示结果

## Blocked by

- Blocked by `issues/006-serial-session-event-queue.md`

## Requirements addressed

- §5 接收事件协议的显示数据边界
- §6 接收区显示
- §14.5 fake 时间源与 Qt offscreen 测试
- §15.4 无 Qt 依赖的事件渲染器
- §16.3 事件渲染测试决策
