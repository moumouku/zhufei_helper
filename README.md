# 派蒙助手

派蒙助手是一个面向 Windows 的极简串口调试上位机。它通过串口接收设备数据并实时显示原始内容，同时支持发送文本或 HEX 数据。

当前版本：`v0.5.0`。

完整操作步骤见 [docs/user-manual.md](docs/user-manual.md)。
按功能独立管理的需求文档见 [docs/requirements/index.md](docs/requirements/index.md)。
界面设计方案与实施记录见 [docs/ui-design/ui-design.md](docs/ui-design/ui-design.md)。
版本变更及实现清单见 [CHANGELOG.md](CHANGELOG.md)。
使用 AI 更新并安全发布新版本见 [docs/ai-update-guide.md](docs/ai-update-guide.md)。

## 下载

仓库根目录直接提供单文件 Windows 程序，**无需安装 Python 或任何依赖**：

```text
PaimonAssistant.exe
```

- 仓库内直接获取（点开文件页面右上角的 Download raw file）：<https://github.com/moumouku/zhufei_helper/blob/main/PaimonAssistant.exe>
- `v0.5.0` 发布页：<https://github.com/moumouku/zhufei_helper/releases/tag/v0.5.0>

当前文件 SHA256：

```text
20a7c6b82306b6aeb3ebec70be40e20f4f77d3f1797d9e5bf8be3ed3177e5e07
```

下载后可用下面的命令校验：

```powershell
Get-FileHash .\PaimonAssistant.exe -Algorithm SHA256
```

也可以使用源码运行或自行打包，见下方“安装依赖”和“打包”两节。

## 功能

- 深色「派蒙·石墨」界面：按“连接、接收、发送”分区，接收数据是视觉中心；界面中文使用 Microsoft YaHei UI，接收显示区与发送输入框使用 Consolas 等宽字体
- 默认窗口 1080 x 680、最小 760 x 480；窗口底部状态栏显示连接状态、端口与串口参数、解析模式和日志状态
- 串口参数使用可折叠行，按钮实时显示 `8N1`、`7E1.5` 等摘要；展开/收起不重置参数
- “跟随最新”默认开启：上翻历史时自动暂停跟随并继续接收与写日志，点击“回到最新”恢复；两种解析模式分别记住各自阅读位置
- 同一时刻只有一个主操作：未连接突出“打开”，已连接突出“发送”
- 自动枚举可用 COM 口
- 每约 1 秒自动监测串口热插拔，按差量更新端口列表
- 新端口按规则自动选中但绝不自动打开；连接端口连续两次轮询缺失后自动关闭并提示
- 波特率预置 9600、19200、38400、57600、115200、230400、460800、921600，也支持手动输入
- 数据位、校验位、停止位可配置，默认 8N1
- 接收区支持文本和 HEX 两种显示模式
- 文本编码支持 UTF-8 和 GBK
- 接收解析可选“按 `\r\n` 分帧”（默认）或“原始字节”：原始字节模式下数据到达即显示，不做分帧、不写 `RX` 日志，适合调试未知协议
- 分帧模式下以连续 `\r\n`（`0D 0A`）作为唯一帧结束边界；单独的 `\r`、单独的 `\n` 和 `\r\r\n` 按严格边界规则处理
- 每个完整数据帧生成一个接收事件，并在识别到结束边界时记录本地时间戳（`HH:mm:ss.SSS`）
- 提供“时间戳”显示开关，默认开启；关闭只影响显示，不影响接收与日志
- 无论时间戳开关状态如何，完整接收事件都按本地日期写入 `%LOCALAPPDATA%\PaimonAssistant\logs\YYYY-MM-DD.txt`
- 日志记录格式为 `[HH:mm:ss.SSS] RX <原始帧 HEX>`，固定使用完整原始帧，不受界面模式或编码影响
- 提供“日志目录”入口，并用资源管理器打开日志目录本身
- 超过 30 天的日期日志在启动时和跨日首次写日志前自动清理，不误删其他文件
- 日志写入失败时显示红色提示并写入系统日志，串口接收、事件生成和界面显示继续工作
- 单个未完成帧超过 1 MiB 时丢弃并给出一次性红色诊断提示，识别到下一个 `\r\n` 后恢复分帧
- 发送区支持文本和 HEX 两种模式
- HEX 输入支持空格、逗号混合分隔，非法输入会提示
- 接收线程与界面线程分离，队列批量刷新界面
- 串口打开、读写、断开异常会提示并恢复到可重新打开的状态

## 环境

- Windows 10 / 11
- Python 3.10 或 3.11
- PySide6 6.6+
- pyserial 3.5+
- PyInstaller 6.x（仅打包时需要）

## 安装依赖

在项目根目录执行：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 运行

```powershell
.venv\Scripts\python.exe main.py
```

无可用串口时软件仍可启动；打开串口前需要先选择有效的 COM 口。

## 测试

测试使用 Qt offscreen 平台，不需要显示器或真实串口：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:QT_QPA_PLATFORM
```

测试重点包括配置校验、UTF-8/GBK 解码、HEX 解析、串口 reader 生命周期、会话隔离、严格 `\r\n` 分帧与超长帧处理、接收事件渲染、接收解析模式切换、日志格式与 30 日保留清理、日志故障隔离、清空边界、主窗口收发、界面主题与字体、布局与折叠行、状态栏事实、历史阅读跟随、热插拔差量与去抖策略和入口冒烟。自动化测试当前结果为 `390 passed, 6 skipped`（跳过项为需要创建符号链接特权的用例和默认跳过的真实串口验收）。测试通过会话级环境隔离保证不会写入真实的 `%LOCALAPPDATA%\PaimonAssistant\logs\`。

真实 com0com 端到端验收默认跳过，可显式执行：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:PAIMON_COM0COM_E2E = "1"
.venv\Scripts\python.exe -m pytest tests/test_manual_com0com_e2e.py -q
```

端口可用 `PAIMON_E2E_WINDOW_PORT` / `PAIMON_E2E_PEER_PORT` 覆盖（默认 `COM17` / `COM19`）。

## 打包

使用 PyInstaller 生成单文件窗口程序：

```powershell
.venv\Scripts\pyinstaller.exe --clean --noconfirm PaimonAssistant.spec
```

产物位于 `dist\PaimonAssistant.exe`。可以用下面的命令做无界面冒烟检查：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
dist\PaimonAssistant.exe --smoke-test
Remove-Item Env:QT_QPA_PLATFORM
```

## com0com 联调

当前开发环境的 com0com 端口为 `COM17 <-> COM19`，默认手工验收使用该虚拟串口对。端口号由 Windows/com0com 分配，其他环境可能不同；`COM3 <-> COM4` 仅为下载 com0com 前的示例。驱动安装和创建端口通常需要管理员权限。

1. 确认 com0com 已提供 `COM17` 与 `COM19` 配对口；若在其他环境中端口号不同，以系统实际枚举结果为准。
2. 启动派蒙助手，选择 `COM17`，配置为 `115200 / 8 / N / 1`，点击打开。
3. 在另一端使用串口工具连接 `COM19`。也可以使用 pyserial 自带终端：

   ```powershell
   .venv\Scripts\python.exe -m serial.tools.miniterm COM19 115200
   ```

4. 从 COM19 发送 UTF-8 或 GBK 文本，检查派蒙助手的文本显示和 HEX 显示。
5. 在派蒙助手发送文本和 HEX，检查 COM19 收到的原始字节。
6. 在派蒙助手保持 COM17 连接时，使 COM17 短暂消失后在一次轮询内恢复，确认不会关闭；连续两次轮询仍不可见时，确认约 2 秒内自动关闭并弹出“串口已拔出，连接已关闭”。
7. 拔除未选中的 COM19，确认它静默从列表移除；拔除未连接但已选中的端口，确认选择留空且不会自动打开其他端口。
8. 关闭另一端、让另一个实例占用 COM17，确认原有错误提示出现且窗口可以再次打开。

当前环境已检测到 `COM17` 和 `COM19`；若在其他 Windows 环境中联调，需要使用该环境实际提供的端口号。

## 已知边界

- 接收端只按 `\r\n` 识别数据帧边界，不解析载荷内部的业务字段，不识别帧头、长度、命令字、校验和、JSON、Modbus 等协议字段。
- “按 `\r\n` 分帧”模式下，没有结束符的数据不会显示也不会写日志；调试未知协议时请切到“原始字节”模式。
- 接收历史保存在内存中，长时间高流量运行会持续增加内存占用；清空按钮可主动释放历史内容。
- 本期不持久化串口配置。
- com0com 属于外部测试环境，不是应用运行时依赖。
