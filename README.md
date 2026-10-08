# codex-setup

Codex 的个人工作约定、文件回收与 Git 备份配置。网络和工具缓存可直接使用，源码修改限定在工作区，删除内容保留 7 天，危险 Git 操作的备份保留 30 天。

将仓库交给 Codex：

> 按 README 配置本机 Codex，保留 Codex 管理的运行时内容，完成安装和验证。

## 配置本机

需要 Python 3.11 以上、Git 和支持 Hooks 的 Codex。Windows 使用 PowerShell 7，已在 Codex CLI 0.161.0 验证脚本行为。

Agent 按以下步骤配置：

1. 检查 `CODEX_HOME`，未设置时使用 `~/.codex`。读取现有配置，确认全局指令与 hook 的合并方式。
2. 运行安装预览，核对发现的缓存路径，再安装。安装器替换全局 AGENTS.md、config.toml 与 hooks.json，安装专用 hook 目录，现有内容替换前保存到回收目录。
3. config.toml 使用仓库模板和本机缓存路径，保留 node_repl、插件、marketplace、通知和运行时环境配置。旧的桌面偏好、引导状态、trust 记录、文档 MCP、窗口覆盖和命令放行记录退役。模型采用 gpt-6.1-sol 与 medium，其他可用模型可按需调整。
4. 在新会话检查 `/hooks`，由用户审核并信任新定义。测试联网、工具运行、回收恢复和 Git 备份，报告实际结果。

```powershell
python Hooks/setup.py --replace
python Hooks/setup.py --replace --apply --schedule-cleanup
codex
```

Linux 与 macOS 安装时省略 `--schedule-cleanup`。自定义安装目录使用 `--target`。本机数据目录可用 `CODEX_SETUP_STATE` 指定，需在 Codex 和清理任务中使用相同路径。

根目录 `config.toml` 是共享配置模板。安装器据此生成本机 `~/.codex/config.toml`，加入缓存和回收目录的写权限。Windows 配置 elevated sandbox。认证文件、系统 Skills、插件文件和会话数据保持原样。

permission profiles 当前为 beta，managed 环境可能覆盖用户权限。验证应在重新启动的目标会话中完成。[权限配置](https://learn.chatgpt.com/docs/permissions)

## 回收与备份

两个 PreToolUse hook 分别处理文件删除与危险 Git 操作：

- 明确路径的 shell 删除改写为回收命令；补丁删除前保存原内容，再执行完整补丁。
- Git 备份保存 staged、worktree 的 binary patch、index 和未跟踪文件。`clean -x/-X` 还保存忽略文件，HEAD、stash、分支与 tag 的恢复对象由专用 Git 引用保留。

回收请求无法可靠解析，或 Git 备份失败时，返回明确拒绝。两个检查独立运行，共用数据锁。启动包装器将内部检查超时、崩溃和无效输出转为拒绝。

Windows 数据保存在 `%LOCALAPPDATA%\CodexSetup`，其他平台为 `~/.local/share/codex-setup`。每个条目的 `manifest.json` 记录原路径、时间和恢复信息。

恢复回收条目时，指定完整目录，已有文件不会被覆盖：

```powershell
python "$env:USERPROFILE\.codex\hooks\codex-setup\recycle_delete.py" restore "<回收条目目录>"
```

Git 改动可在新的 worktree 中恢复，保留当前工作目录里的内容：

```powershell
git -C "<原仓库>" worktree add --detach "<恢复目录>" "refs/codex-setup/<条目 ID>/head"
git -C "<恢复目录>" apply --index "<条目目录>/staged.patch"
git -C "<恢复目录>" apply "<条目目录>/worktree.patch"
tar -xf "<条目目录>/untracked.tar" -C "<恢复目录>"
```

空 patch 跳过 apply，未有首个 commit 的仓库从新初始化的目录恢复。stash、分支和 tag 的恢复引用见 manifest。

## 定期清理

Windows 的 `--schedule-cleanup` 注册 `CodexSetup-Cleanup`，每天本机时间 03:00 执行，错过时间后补跑，在用户登录后以普通权限运行。Linux 与 macOS 由配置 Agent 为当前用户设置每日定时任务，调用已安装的 `cleanup.py`。

```powershell
python "$env:USERPROFILE\.codex\hooks\codex-setup\cleanup.py"
```

清理只处理本工具登记且已过期的条目，以及对应的 Git 引用。未知文件保留，符号链接与 Windows junction 不被遍历。回收和 Git 备份完成后也会运行清理。

## 上下文压缩

窗口大小和自动压缩采用所选模型的默认设置，随模型切换调整。阶段成果和待办保存在项目文件中，便于压缩后继续工作。

## 防护范围

Hook 处理已识别的工具调用，沙箱限制外部目录的写权限。任意程序内部的删除、覆盖或截断无法仅靠命令 hook 完整控制。尚未信任的 hook 会被跳过，hook 自身启动失败或被 Harness 超时终止也不保证拦截操作。[Hooks 接口](https://learn.chatgpt.com/docs/hooks)
