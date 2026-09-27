# 020 - 故障隔离、全量回归与可发布交付

## Type

AFK

## Parent PRD

`docs/requirements/REQ-0005-oscilloscope.md`（§2.2-2.3、§10、§12、§13 全部、§14）

## What to build

完成 REQ-0005 的跨功能验收和交付收口。建立覆盖协议、接收所有权、双时间、恢复边界、三分钟存储、绘图交互、清空及日志故障的自动化验收矩阵；验证所有连接和资源异常都恢复一致的串口、消费者与页面锁定状态。保持 REQ-0001～REQ-0004 全量行为，并在 Python 3.10、最低支持 PySide6 6.6、Qt offscreen 和 PyInstaller 路径验证 QtCharts 可用。

功能通过后，将源码版本更新为 `0.6.0`，并同步 README、用户手册和 CHANGELOG。由于此 issue 不发布 EXE，文档必须明确区分“源码开发版本 v0.6.0”和“最新已发布 EXE v0.5.0”，CHANGELOG 使用未发布口径；不得替换仓库根目录 EXE、SHA 或 v0.5.0 下载链接。本 issue 也不创建 GitHub Release，不执行发布 commit、tag 或 push。

## Acceptance criteria

- [x] RX 日志测试分别证明合法帧和非法完整帧写入，未完成帧和超长帧不写入，且时间/HEX 格式保持 REQ-0003 契约
- [x] 日志首次失败熔断后，串口读取、分帧、协议解析、原始采样保留、图表更新和数据显示继续；固定日志错误提示只按既有规则显示
- [x] 内存或绘图资源失败停止波形接收、关闭串口、保留已有数据、解除切页锁并显示非模态错误
- [x] 串口打开、读取、写入、关闭及热拔插失败均不会留下假连接、假消费者或永久禁用的页面切换；底层关闭失败保持既有 best-effort 非模态语义，不新增关闭失败弹窗
- [x] 自动化验收覆盖 REQ-0005 §13.1～§13.4，并使用注入时钟、fake serial、临时日志目录和 Qt offscreen，避免精确时序依赖真实 sleep
- [ ] REQ-0001～REQ-0004 全量既有测试继续通过，尤其包括数据页原始字节/分帧、日志熔断、清空线性化、热插拔与发送字节契约
- [x] 在 Python 3.10 + PySide6 6.6.x 最低支持环境验证 QtCharts 导入、核心图表测试和源码冒烟；同时在 Python 3.11 开发环境通过全量测试，不新增 PySide6 之外的图表运行时依赖
- [ ] PyInstaller 干净构建包含 QtCharts，源码和打包程序的 `--smoke-test` 均通过
- [x] 必要的 Windows 原生 QtCharts 鼠标交互验收步骤明确记录；真实 com0com 验收保持显式启用且不成为普通自动测试前提
- [x] `README.md` 与 `docs/user-manual.md` 准确说明数据/波形页、接收互斥、整数协议、时间语义、三分钟窗口和图表操作
- [x] `CHANGELOG.md` 增加未发布的 v0.6.0 实现与实际验证结果，程序版本信息同步为 `0.6.0`；README 明确源码开发版本 v0.6.0 与最新已发布 EXE v0.5.0，未执行的测试或手工验收不得宣称通过
- [x] 不修改父 PRD 的已确认决定，不替换仓库根 `PaimonAssistant.exe` 或其 SHA，不修改 v0.5.0 发布链接，不创建 Release、不执行发布 commit/tag/push

## 交付状态

本 issue 的功能与文档/版本实现已完成；交付验证按以下口径收口，未完成项不得先写“通过”：

- 已验证（本工作树快照，最终滚动/悬停修复前）：全量回归 `557 passed, 6 skipped`（跳过项 6 条）；Python 3.10.19 + PySide6 6.6.3.1 下 QtCharts 导入、核心图表相关测试 65 项与源码冒烟测试通过；issue 018 原生交互在上一提交的 Windows 环境 20 项通过。
- 待最终验证：最终滚动/悬停修复合入后的全量回归；Windows 原生 QtCharts 交互复验；PyInstaller 干净构建及源码/打包程序 `--smoke-test`；完成后替换 README 与 CHANGELOG 中的“待发布验证回填”占位。
- 未执行：发布 commit/tag/push、GitHub Release、仓库根 EXE/SHA 与 v0.5.0 下载链接的任何变更。

## Blocked by

- Blocked by `issues/019-linearizable-new-acquisition.md`

## User stories addressed

按父 PRD §2.1 的范围项映射：

- US-1：在“数据”和“波形”页面间切换
- US-2：两页独立开始/停止接收并隔离历史
- US-3：严格解析 1～8 个整数通道
- US-4：查看帧、相对时间、波形和通道状态
- US-5：保留并交互检查最近 180 秒原始数据
- US-6：将完整示波器帧写入既有 RX 日志
