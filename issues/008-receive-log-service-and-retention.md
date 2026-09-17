# 008 - 接收日志服务与保留清理

## Parent PRD

`docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md`（第 7、7.3、7.4、8、14、15.3、17 节；实施决策 3、4；测试决策 4；验收标准 11-15）

## What to build

实现无 Qt 依赖的接收日志服务，放在 `paimon_assistant/receive_log.py`，并通过依赖注入支持临时目录、fake 本地时间和可控文件操作测试：

- 服务公开 `ensure_directory() -> None` 和 `write_event(event: ReceivedEvent) -> bool`；日志目录、事件时间到本地日期的转换函数和文件操作均可注入测试替身。服务定义明确的 `ReceiveLogError` 供调用方区分日志故障。
- 默认日志目录为 `%LOCALAPPDATA%\\PaimonAssistant\\logs\\`，首次写日志或显式调用 `ensure_directory()` 时自动创建。
- 按事件完成时的本地日期选择严格命名为 `YYYY-MM-DD.txt` 的 UTF-8 文件；每个 `ReceivedEvent` 写一行 `[HH:mm:ss.SSS] RX <RAW_FRAME_HEX>`，固定使用完整 `raw_frame` 的大写 HEX，写入后及时刷新。
- 日志日期从事件时间戳转换得到，跨午夜完成的事件写入次日文件；记录行不重复写年月日。
- 启动时执行一次清理；运行跨过本地日期后，在新日期首次写日志前再次清理。只删除日志目录中名称严格匹配日期格式的普通文件，保留当天及此前 30 个日历日，按文件名日期而非修改时间判断。
- 首次目录创建、文件打开、写入或刷新失败时，服务立即记录原始异常、标记本次运行熔断并抛出 `ReceiveLogError`；熔断后的后续 `write_event()` 不访问文件系统、不缓存事件、不重试，直接返回 `False`。
- 日志错误使用 Python 标准库 `logging` 写入应用系统日志；本需求不要求 Windows Event Log，除非外部交付流程另行指定。
- 单个过期文件删除失败只记录错误并继续处理，既不影响其他文件清理，也不影响串口接收。
- 服务不接受发送数据作为日志输入，不修改 `ReceivedEvent` 和原始帧字节；服务不依赖 Qt、真实用户目录或真实系统时钟。

## Acceptance criteria

- [ ] 默认路径正确，缺失目录在首次写日志或显式打开目录前自动创建
- [ ] 每个完整接收事件立即追加到事件日期对应的 `YYYY-MM-DD.txt`，UTF-8 编码且写入后刷新
- [ ] 日志行格式严格为 `[HH:mm:ss.SSS] RX <RAW_FRAME_HEX>`，完整 HEX 含 `0D 0A`，空帧记录为 `0D 0A`
- [ ] 日志记录按事件形成顺序写入，日期只出现在文件名中
- [ ] 跨本地日期的事件写入新日期文件，并在新日期首次写入前执行一次保留清理
- [ ] 启动清理和跨日清理删除早于当前日期前 30 天的目标文件，保留边界日期文件
- [ ] 日期格式不匹配的文件、子目录和其他文件不会被删除，日期判断不依赖 mtime
- [ ] 单个删除失败不会中止串口相关功能或后续目标文件处理
- [ ] 目录/文件写入或刷新首次失败后抛出明确的 `ReceiveLogError` 并立即熔断；后续调用不访问文件系统，无内存失败缓存、无重试、无待重试队列
- [ ] 故障由 Python 标准库 `logging` 记录，并向调用方提供明确错误信息
- [ ] 日志服务单元测试覆盖路径、格式、刷新、跨日、30 日清理、非目标保护、删除失败、`ReceiveLogError` 和写入失败熔断
- [ ] 日志服务不依赖 Qt、真实用户目录或真实系统时钟

## Blocked by

- Blocked by `issues/005-receive-framing-and-events.md`

## Requirements addressed

- §7 接收日志
- §8 日志保留清理
- §9.2 日志目录创建能力
- §14.5 fake 本地时间、临时目录和可控文件操作
- §15.3 日志写入与 reader 解耦、首次失败熔断
- §15.4 无 Qt 依赖与依赖注入
- §16.4 日志服务测试决策
