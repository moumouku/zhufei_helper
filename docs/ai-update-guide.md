# 派蒙助手 AI 更新与发布命令

派蒙助手项目已配置可复用的 pi 命令：

```text
/update-paimon <本次更新内容>
```

每次更新只需要写本次要实现的内容。发布约定固定为：**Branches 只保留 `main`，每个版本进入 Tags，`main` 与版本标签都包含能直接下载的 EXE**，不需要每次重复提醒。

## 使用示例

修复问题：

```text
/update-paimon 修复串口意外断开后无法重新连接的问题
```

增加功能：

```text
/update-paimon 增加接收时间戳开关，默认关闭，并记住用户上次的选择
```

可执行文件默认随 `main` 和版本标签交付。如果还需要 GitHub Release，可以另外明确要求：

```text
/update-paimon 增加接收内容保存功能，并把新版本 EXE 上传到对应的 GitHub Release
```

如果命令后没有内容，AI 只会询问本次要更新什么，不会自行猜测需求。

## 命令会自动完成的工作

1. 检查工作树、所有分支/worktree、远端与版本标签，保留用户已有改动。
2. 只实现本次需求，并按 SemVer 确定未使用的版本号；用户已指定版本时先核查该标签是否可用。
3. 修改必要代码、补充测试，同步版本、README、CHANGELOG 和既有交接文档。
4. 运行完整测试、源码冒烟、干净打包和 EXE 冒烟；未执行的真实设备验收不宣称通过。
5. 把已验证 EXE 纳入仓库根目录 `PaimonAssistant.exe`，公布文件大小与 SHA256，检查敏感信息和待提交文件。
6. 安全合入本地 `main`，创建发布提交和注释标签 `vX.Y.Z`；标签必须指向 `main` 的同一发布提交，包含源码及 EXE。
7. 非强制推送 `main` 与该版本标签，优先原子推送；核对远端提交完全一致。
8. 从 `main` 和标签固定下载链接实际下载 EXE，比较完整大小与 SHA256，不只检查网页是否存在。
9. 交付核验成功后，清理已集成的本地/远端临时分支和已完成 worktree，最终 Branches 只剩 `main`。删除前检查独有提交和未提交内容；无法证明安全时先保留并报告，不强删。
10. 只有用户另外要求时才创建 GitHub Release；它不是 Tags 交付的替代品。最后报告 `main`、标签、EXE 下载链接、测试和清理结果。

仅同步已验收版本或修改发布文档时，可以复用具有可核验来源的 EXE：确认源码、版本、依赖与打包配置没有变化，SHA256 与已验收产物一致，并重新运行源码/EXE 冒烟。任何构建输入变化都需要重新构建。

## 分支与版本的固定规则

- `main` 保存最新已交付版本；GitHub 的 Branches 和本地分支列表最终只保留 `main`。
- 版本使用 `vX.Y.Z` 注释标签，不用 `feature/req-0002-*`、`feature/req-0005-*` 等长期分支代替版本标签。
- 开发期间可使用临时分支/worktree；确认成果进入 `main` 且远端与 EXE 下载核验通过后再清理。
- 已合入提交可安全删除分支。通过 cherry-pick 集成、补丁等价但 SHA 不同的分支，先保存 Git bundle 并核验，再删分支引用；独有工作不得静默丢弃。
- worktree 若有未提交、未跟踪或本机配置，先逐项检查并备份；不清理用户未知文件、stash、有效共用环境，也不沿目录联接递归删除共用 `.venv`。
- 发布结束时保持工作区在 `main`，刷新远端跟踪分支，保留全部历史版本标签。

## 版本号规则

| 改动类型 | 示例 | 版本变化 |
|---|---|---|
| 修复、兼容性优化、文档维护 | 修复重连错误 | `v0.1.0 -> v0.1.1` |
| 向后兼容的新功能 | 增加保存接收内容 | `v0.1.0 -> v0.2.0` |
| 不兼容的重大变化 | 更换配置格式且无法兼容旧格式 | `v0.2.0 -> v1.0.0` |

## 旧版本保护

该命令明确禁止：

- `git push --force` 或 `git push --force-with-lease`
- 改写 Git 历史
- 删除、重建或改指向任何已发布版本标签（包括当前版本）；同名标签指向不同提交时停止，默认新增补丁版本
- 将公开仓库擅自改成私有（本项目就是公开开源项目）
- 除项目模板 `.pi/prompts/update-paimon.md` 外，把 `.pi/` 的其他内容、`.venv/`、缓存、日志、凭据或本机文件提交进仓库
- 提交 `build/`（PyInstaller 中间产物）与 `dist/`（本地构建输出目录）；这两个目录已由 `.gitignore` 忽略

该命令要求并允许的做法：

- **每次发布必须把已验证 EXE 纳入仓库根目录并提交**，`main` 和该版本标签均包含它；下载链接不能只指向会被删除的临时分支。
- 仓库为公开开源项目（`moumouku/zhufei_helper`），不擅自改变可见性。
- 同名标签已经指向目标发布提交时可以核验后继续，避免重复操作；若指向不同提交则停止，不强制移动标签。

正常更新只在历史后面增加新提交和新标签，例如：

```text
v0.1.0 -> 初始版本提交
v0.1.1 -> 后续修复提交
v0.2.0 -> 后续功能提交
```

GitHub 默认页面显示 `main` 的最新内容；Tags 保存各个版本快照。README 必须同时提供最新 main EXE 和标签固定 EXE 的下载链接，并注明版本、大小及 SHA256。推送完成必须实际下载核验。

当前发布路径示例：

- 最新版：[main/PaimonAssistant.exe](https://raw.githubusercontent.com/moumouku/zhufei_helper/refs/heads/main/PaimonAssistant.exe)
- 固定版本：[v0.7.0/PaimonAssistant.exe](https://raw.githubusercontent.com/moumouku/zhufei_helper/refs/tags/v0.7.0/PaimonAssistant.exe)
- 版本列表：[Tags](https://github.com/moumouku/zhufei_helper/tags)

Git 标签和 GitHub Release 是两种不同对象；本项目默认交付 `main` + Tags + EXE，不自动要求创建 Release。

## 项目模板位置

```text
E:\projects_learning\zhufei_helps\.pi\prompts\update-paimon.md
```

文件名决定命令名，因此 `update-paimon.md` 对应 `/update-paimon`，只属于派蒙助手项目。pi 只会为已信任的项目加载 `.pi/prompts/`；模板修改后可运行 `/reload` 或重启会话使更新生效。
