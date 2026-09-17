# 005 - 严格分帧与 ReceivedEvent

## Parent PRD

`docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md`（第 4、5、5.1、14.6、15.2、16.2 节；验收标准 1-8）

## What to build

实现无 Qt 依赖的接收分帧模块，放在 `paimon_assistant/receive_framer.py`。模块提供 `ReceivedEvent` 数据结构和按读取块增量处理的分帧器，公开接口固定为：

```text
ReceiveFramer(clock_ms, max_payload_bytes=1_048_576, on_overflow=None)
feed(data: bytes) -> list[ReceivedEvent]
reset() -> None
```

- `ReceivedEvent` 是不可变记录；`payload` 和 `raw_frame` 在构造时复制为稳定的 `bytes` 快照。
- 以连续字节 `0D 0A` 作为唯一结束边界；单独的 `0D` 或 `0A` 按载荷保留，支持边界跨读取块和一次读取多帧。
- `feed()` 按输入顺序返回本次识别出的完整事件；没有完整帧时返回空列表。每识别到结束边界，使用注入的毫秒时间源在同一处理点生成一个事件。
- `clock_ms` 是无参数可调用的毫秒时间源；每个事件只在识别到其结束边界时调用一次。
- `reset()` 丢弃未完成尾部和超长帧丢弃状态，不生成事件，供连接关闭和“清空”使用。
- 在未遇到 `0D 0A` 前，载荷长度恰好为 `1 MiB` 仍然有效；收到其后的第一个载荷字节时才进入溢出丢弃状态。丢弃到下一个完整结束符后恢复正常分帧。
- `on_overflow` 只在一次连续溢出开始时调用一次，回调不得直接操作 Qt；同一段溢出重置后可再次提示。
- 事件中的字节必须是稳定快照，后续 `feed()`、显示模式、编码或界面操作不能改变已生成事件。

## Acceptance criteria

- [ ] `A\r` 与后续读取块中的 `\n` 合并为一个事件，时间戳在识别 `\r\n` 时取得
- [ ] 一次输入 `A\r\nB\r\n` 按顺序生成两个独立事件
- [ ] 单独 `\r`、单独 `\n`、`\r\r\n` 和其他非 `\r\n` 换行形式符合严格边界规则
- [ ] `\r\n` 生成空 `payload`，且 `raw_frame` 为 `0D 0A`
- [ ] 未完成尾部不会生成事件；重置后不会与后续数据拼接
- [ ] 每个事件的 `received_at_ms`、`payload`、`raw_frame` 内容正确，多帧事件顺序稳定
- [ ] 载荷恰好为 `1 MiB` 时仍可在后续 `\r\n` 到达后生成事件，收到第一个额外载荷字节才判定超长
- [ ] 超过 `1 MiB` 的未完成帧不生成事件，持续丢弃到下一个 `\r\n`，之后能恢复解析
- [ ] 单次溢出只通过 `on_overflow` 产生一次诊断，连接/清空重置后可重新诊断
- [ ] 分帧器、事件结构和测试不依赖 Qt、真实串口或系统时钟
- [ ] 新增分帧单元测试覆盖严格 `0D 0A`、跨块、多帧、空帧、`\r\r\n`、尾部清理、`1 MiB` 边界、超长恢复、一次性溢出诊断和时间戳

## Blocked by

None - can start immediately

## Requirements addressed

- §4 接收数据协议
- §5 接收事件协议
- §14.6 自动化测试约束
- §15.2 可注入毫秒时间源
- §16.2 分帧器测试决策
