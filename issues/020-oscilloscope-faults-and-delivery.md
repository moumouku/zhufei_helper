# 020 - 故障隔离、全量回归与可发布交付

## Type

AFK

## Parent PRD

`docs/requirements/REQ-0005-oscilloscope.md`（§2.2-2.3、§10、§12、§13 全部、§14）

## What to build

完成 REQ-0005 的跨功能验收和交付收口。建立覆盖协议、接收所有权、双时间、恢复边界、三分钟存储、绘图交互、清空及日志故障的自动化验收矩阵；验证所有连接和资源异常都恢复一致的串口、消费者与页面锁定状态。保持 REQ-0001～REQ-0004 全量行为，并在 Python 3.10、最低支持 PySide6 6.6、Qt offscreen 和 PyInstaller 路径验证 QtCharts 可用。

功能通过后，将源码版本更新为 `0.6.0`，并同步 README、用户手册和 CHANGELOG。原实施阶段只验证本地 EXE；验收后用户进一步授权同步工作区、清理临时文件，并将可直接下载的 EXE 随集成分支推送。因此当前分支根目录纳入已验证的 v0.6.0 EXE，文档提供下载链接和 SHA256；最新正式 Release 与 `main` 仍为 v0.5.0，本次不创建 v0.6.0 tag 或 GitHub Release。

## Acceptance criteria

- [x] RX 日志测试分别证明合法帧和非法完整帧写入，未完成帧和超长帧不写入，且时间/HEX 格式保持 REQ-0003 契约
- [x] 日志首次失败熔断后，串口读取、分帧、协议解析、原始采样保留、图表更新和数据显示继续；固定日志错误提示只按既有规则显示
- [x] 内存或绘图资源失败停止波形接收、关闭串口、保留已有数据、解除切页锁并显示非模态错误
- [x] 串口打开、读取、写入、关闭及热拔插失败均不会留下假连接、假消费者或永久禁用的页面切换；底层关闭失败保持既有 best-effort 非模态语义，不新增关闭失败弹窗
- [x] 自动化验收覆盖 REQ-0005 §13.1～§13.4，并使用注入时钟、fake serial、临时日志目录和 Qt offscreen，避免精确时序依赖真实 sleep
- [x] REQ-0001～REQ-0004 全量既有测试继续通过，尤其包括数据页原始字节/分帧、日志熔断、清空线性化、热插拔与发送字节契约
- [x] 在 Python 3.10 + PySide6 6.6.x 最低支持环境验证 QtCharts 导入、核心图表测试和源码冒烟；同时在 Python 3.11 开发环境通过全量测试，不新增 PySide6 之外的图表运行时依赖
- [x] PyInstaller 干净构建包含 QtCharts，源码和打包程序的 `--smoke-test` 均通过
- [x] 必要的 Windows 原生 QtCharts 鼠标交互验收步骤明确记录；真实 com0com 验收保持显式启用且不成为普通自动测试前提
- [x] `README.md` 与 `docs/user-manual.md` 准确说明数据/波形页、接收互斥、十进制数值协议、时间语义、三分钟窗口和图表操作
- [x] `CHANGELOG.md` 记录 v0.6.0 开发构建的实现与实际验证结果，程序版本信息为 `0.6.0`；README 明确本分支源码/EXE 为 v0.6.0、最新正式 Release 为 v0.5.0，未执行的验收不宣称通过
- [x] 父需求按用户确认的小数协议和接收正文修订；已验证 EXE 按验收后的授权纳入根目录，SHA256 和直接下载链接同步；保留历史 v0.5.0 tag、Release 与下载链接

## 交付状态

本 issue 的功能、文档、源码版本与本地交付验证已完成：

- Python 3.11.15 + PySide6 6.11.1、Python 3.10.19 + PySide6 6.6.3.1 两套环境均通过全量回归 `620 passed, 6 skipped`；QtCharts 导入及源码冒烟通过。
- Windows 原生 QtCharts 交互专项（本轮小数协议修订后）`22 passed`；前次更广原生专项基线为 `73 passed`；前次 125% 缩放下检查 1080 × 680、760 × 480 的数据页与八通道波形页。
- PyInstaller 6.22.0 干净构建及打包程序冒烟通过，清单包含 QtCharts；本地产物 `dist/req0005/PaimonAssistant.exe`。
- 已完成独立只读审查、滚动锚点与悬停查询修正、队列分配失败原子性修正及图表主题修正；README、CHANGELOG 已回填最终结果。
- 未执行真实 com0com 联调（5 项默认跳过）、100% 缩放人工复验、高帧率长时间设备压力测试；另有 1 项符号链接权限跳过。
- 验收后按用户授权推送 `feature/req-0005-integration` 及根目录 v0.6.0 EXE；源码与验证产物已迁回主工作区，12 个 REQ-0005 临时 worktree 已清理，原交接改动已备份。本次未创建 v0.6.0 tag/Release，`main` 与历史 v0.5.0 发布保留。

## Blocked by

- Blocked by `issues/019-linearizable-new-acquisition.md`

## User stories addressed

按父 PRD §2.1 的范围项映射：

- US-1：在“数据”和“波形”页面间切换
- US-2：两页独立开始/停止接收并隔离历史
- US-3：严格解析 1～8 个十进制数值通道，兼容整数并支持小数
- US-4：查看帧、相对时间、波形和通道状态
- US-5：保留并交互检查最近 180 秒原始数据
- US-6：将完整示波器帧写入既有 RX 日志
