# 011 - 接收解析模式选择（协议分帧 / 原始字节）

## Parent PRD

`docs/requirements/REQ-0004-receive-parse-mode.md`（第 3、4、5、6 节；验收标准 1-15）

## What to build

在既有 REQ-0003 接收链路上增加"接收解析"模式选择，提供一条"收到即显示"的原始字节路径：

- 新增 `QComboBox`（对象名 `parse_mode_combo`），选项 `按 \r\n 分帧`（默认）与 `原始字节`，不持久化选择。
- 分帧模式完全保持 REQ-0003 行为不变。
- 原始字节模式：读取块到达即进入原始字节历史并按当前文本/HEX 与编码渲染；不生成 `ReceivedEvent`、不写 `RX` 日志、不做超长帧判定；时间戳控件在该模式下禁用。
- 原始字节模式的文本渲染必须保持跨读取块的多字节字符待定语义（复用 `IncrementalTextDecoder`，全量重绘时不 flush）。
- `SerialController` 新增 `raw_queue`（只放 `bytes`）与 `set_raw_mode(enabled: bool)`；模式标记在控制器上持久化，`open()` 创建的新会话沿用；模式真正变化时在会话锁内重置分帧器未完成尾部，避免跨模式拼接。
- `reset_receive_session()` 同时替换并清空 `received_queue`、`raw_queue`、`diagnostic_queue` 并重置分帧器。
- 清空同时清除事件历史、原始字节历史与两种队列的待处理数据。
- 模式切换不清空两种历史，切换后按新模式渲染。
- 真实 com0com 端到端验证脚本落在 `tests/` 下，默认跳过，显式启用时执行。

## Acceptance criteria

- [ ] `parse_mode_combo` 存在，两个选项文本与顺序正确，启动默认"按 `\r\n` 分帧"
- [ ] 原始字节模式下不含 `\r`/`\n` 的字节到达后立即显示
- [ ] 原始字节模式下单独 `\r`、单独 `\n`、`\r\n` 都不分帧、不分行
- [ ] 原始字节模式下文本/HEX、UTF-8/GBK 切换正确重绘；跨块多字节字符不出现替换字符
- [ ] 原始字节模式不产生事件、不写 `RX` 日志
- [ ] 原始字节模式下时间戳控件禁用且无时间戳前缀
- [ ] 分帧模式下 REQ-0003 全部行为不变（既有测试保持通过）
- [ ] 切换模式重置分帧器尾部；切换前尾部不与切换后数据拼成事件
- [ ] 切换模式保留两种历史；两种通道互不污染（原始字节不会在切换后变成事件）
- [ ] `reset_receive_session()` 清空三个队列；清空后新数据按当前模式正常入链路
- [ ] `received_queue` 只提供 `ReceivedEvent`；`raw_queue` 只提供 `bytes`
- [ ] 发送行为不变：不自动追加 `0D 0A`，发送数据不进入任何接收通道
- [ ] 真实 com0com 端到端脚本在 `tests/` 下，默认跳过，显式启用可跑通两种模式
- [ ] 自动化测试在 Qt offscreen 下全绿，且不写入真实 `%LOCALAPPDATA%`

## Blocked by

None - 基于已完成的 REQ-0003 交付分支

## Requirements addressed

- §3 接收解析模式（控件、原始字节行为、模式切换、队列与接口契约）
- §4 清空行为
- §5 日志边界
- §6 验收标准 1-15
