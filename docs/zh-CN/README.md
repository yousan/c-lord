# c-lord

[![CI](https://github.com/yousan/c-lord/actions/workflows/ci.yml/badge.svg)](https://github.com/yousan/c-lord/actions/workflows/ci.yml)
[![Python 3.10 – 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg)](../../pyproject.toml)
[![MIT license](https://img.shields.io/badge/license-MIT-green.svg)](../../LICENSE)

**在 Discord 中并行运行多个 Claude Code 会话——每个子区使用自己的 git 克隆，会话之间绝不共用工作目录。**

c-lord 把一个 Discord 服务器变成控制室，用来指挥运行在你自己机器上的 Claude Code 会话。给每项任务开一个子区，然后就可以离开；之后用手机、笔记本或终端查看所有任务的进展——会话不会被中断，发生过的一切都留在子区里。

<p align="center">
  <img src="../assets/demo-mock.svg" alt="示意图：一个 Discord 频道里有四个 Claude Code 子区，其中一个已打开，显示一条请求、一条在终端里输入的消息、带文件的回复，以及连接到同一会话的终端（所有名称均为虚构）" width="760">
</p>

## 为什么选择 c-lord

- 🎛️ **Discord 成为多个 Claude Code 会话的控制室。** 一个子区就是一个真实的、交互式的 Claude Code，运行在你机器上的 tmux 里——和你在终端里用的是同一个东西。把文件拖进子区，Claude 就能拿到；Claude 生成的文件会作为附件发回来。想直接用键盘？对那个会话执行 `tmux attach` 即可。
- ⏳ **没有时间限制。** 耗时长的任务不会因为耗时长而被停掉。只有当 Claude 的画面连续 5 分钟没有任何变化时，c-lord 才会放弃这个回合，而且即使这样也不会杀掉 Claude。
- 🧪 **每个子区一个完整独立的工作区。** 每个子区都在你仓库的独立克隆中工作，因此可以同时运行自己的后端和前端，以及自己的 `docker compose` 环境，而不会碰到别的子区。c-lord 知道哪些容器属于哪个子区，并会随子区一起停止它们。（用 `/clord-init` 把频道绑定到仓库即可启用；主机端口仍然是共享的，请给每套环境分配各自的端口。）
- 📜 **一切都保存在 Discord 里。** 回复、工具活动、文件——甚至你直接在 tmux 窗格里输入的内容——都会出现在子区中。你的团队可以跟着看，其他 AI 代理也可以读取历史记录来接手工作。
- 🔒 **由你决定谁可以操作。** 开箱即用时，只有 Discord 应用的所有者可以使用（和 c-lord 对话就等于在你的机器上执行命令）。需要时可以向某个身份组或指定的人开放。

**开始使用：** [快速开始](#快速开始) · [工作原理](#核心思路并行会话各用各的克隆) · [命令](../COMMANDS.md) · [配置](#配置) · [架构](#架构)

[English](../../README.md) · [日本語](../ja/README.md) · 简体中文 · [한국어](../ko/README.md) · [Español](../es/README.md) · [Português](../pt-BR/README.md) · [Français](../fr/README.md)

本页译自[英文版 README](../../README.md)；两者内容不一致时，请以英文版为准。

> **基于 [@ebibibi](https://github.com/ebibibi)（Masahiko Ebisuda）的 [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge)。** 本项目最初由 ebibibi 创建，这里依据 MIT 许可证使用。感谢他打下的出色基础。

> c-lord 是一个独立的开源项目。Anthropic 并未制作、赞助或认可它；“Claude”和“Claude Code”是 Anthropic 的商标。c-lord 只是驱动你自己安装并登录的 Claude Code CLI。

**本页目录：** [工作原理](#核心思路并行会话各用各的克隆) · [快速开始](#快速开始) · [在子区中工作](#在子区中工作) · [工作区](#工作区) · [命令](#命令) · [配置](#配置) · [定时任务](#定时任务) · [GitHub 与 CI](#github-与-ci-自动化) · [REST API](#rest-api) · [作为库使用](#把-c-lord-当作库使用) · [架构](#架构) · [安全](#安全) · [开发](#开发) · [项目如何构建](#这个项目是如何构建的)

## 核心思路：并行会话，各用各的克隆

每个 Discord 子区都是一个独立的 Claude Code 会话，运行在你服务器上属于它自己的 tmux 窗口中。把任务发到几个子区，它们就会并排运行；合上笔记本，它们继续跑；在子区里回复，就能从上次停下的地方接着做。

让并行会话互不干扰的是**隔离**：频道绑定到仓库（`/clord-init`）之后，每个子区都在自己独立的 `git clone` 中工作。会话之间绝不共用工作目录，所以一个子区里未提交的改动不可能出现在另一个子区里。它们只通过 git 远程仓库打交道——分支、推送和 PR——就像人类贡献者一样。

```text
子区 A（功能开发）  ──→  Claude Code（独立克隆）  ─┐
子区 B（PR 审查）   ──→  Claude Code（独立克隆）   ├─→  git 远程（分支 · PR）
子区 C（文档）      ──→  Claude Code（独立克隆）  ─┘
```

**目前会话之间不做任何协调。** 本 README 的早期版本描述过四种协调机制，它们的实际状态如下（[#758](https://github.com/yousan/c-lord/issues/758)）：

| 机制 | 实际状态 |
|---|---|
| **并发提示**（告诉 Claude 正在并行工作的指示） | **未实现**——会话运行在 tmux TUI 中（#53），那里没有按回合传递系统提示的通道，所以它从未送达过；c-lord 已不再生成它（#766） |
| **活动会话注册表** | 存在于内存中，并被 `/workspace-cleanup` 使用，但**会话看不到它**（没有任何东西把它发给会话） |
| **协调频道** | **默认关闭。** 设置 `COORDINATION_CHANNEL_ID` 后，会在某个会话的回合结束时发一行通知；没有开始事件 |
| **AI Lounge** | REST 端点可用，但**没有任何东西告诉 Claude 这个 lounge 存在**，所以实际上不会有任何内容发布——见 [AI Lounge](#ai-lounge未送达会话) |

也就是说：工作目录是分开的，但没有实时协调。克隆之外的任何东西——同一个远程分支、共用的端口或数据库、staging bot——仍然可能冲突。请给每个子区分配自己的分支。

### AI Lounge（未送达会话）

> **状态：尚未接通**（[#758](https://github.com/yousan/c-lord/issues/758)）。lounge 的设计本来是作为临时的系统上下文注入每个会话。由于会话运行在 tmux TUI 中（#53），没有按回合传递的通道，所以 c-lord 不生成它（#766）：Claude 从未被告知 lounge 的存在，实际上也不会有任何内容发布。

REST 端点仍然可用，所以你可以自己使用 lounge——例如在仓库的 `CLAUDE.md` 里说明它：

```bash
# 发布一条留言（存入 SQLite；同时转发到 lounge 频道，该频道默认为 COORDINATION_CHANNEL_ID）
curl -X POST "http://localhost:8080/api/lounge" \
  -H "Content-Type: application/json" \
  -d '{"message": "Starting auth refactor on feature/oauth", "label": "feature dev"}'

# 读取最近的留言
curl "http://localhost:8080/api/lounge"
```

## 快速开始

你需要一台保持开机的 Linux 机器（WSL2 也可以），并安装好以下软件：

- Python 3.10 或更高版本，以及 [uv](https://docs.astral.sh/uv/)
- `git` 和 `tmux`
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)，并且已经登录——请先手动运行一次 `claude` 确认
- 一个你有权添加 bot 的 Discord 服务器

### 第 1 步——创建 Discord bot

1. 打开 [Discord Developer Portal](https://discord.com/developers/applications)，创建一个 **New Application**。
2. 在它的 **Bot** 页面，打开 **Message Content Intent**（位于 *Privileged Gateway Intents* 下），并复制 bot 的 **token**。
3. 在 **OAuth2 → URL Generator** 中，勾选 `bot` 和 `applications.commands` 这两个 scope，然后勾选以下 bot 权限：

   | 权限 | 用途 |
   |---|---|
   | View Channel（查看频道） | 能看到频道本身（见下方说明） |
   | Send Messages（发送消息）、Send Messages in Threads（在子区中发送消息） | 回复 |
   | Create Public Threads（创建公开子区） | 每个会话一个子区 |
   | Read Message History（读取消息历史） | 补读 bot 离线期间发布的消息 |
   | Add Reactions（添加反应）、Manage Messages（管理消息） | 在你的消息上显示状态灯，以及清除它 |
   | Manage Webhooks（管理 Webhook）*（可选）* | 让子代理的消息以各自的名字显示（`CLORD_SUBAGENT_VOICES=1`） |

4. 打开生成的 URL，把 bot 添加到你的服务器。

> **私密频道 / 设有权限限制的频道：** 如果某个频道对 `@everyone` 拒绝了
> **View Channel**（即私密频道），上面的服务器级邀请是*不够*的——Discord
> 的频道级拒绝会覆盖它。你必须把 **bot 的身份组**（或 bot 成员本身）加入该频道的
> 权限覆盖，并至少授予 **View Channel**、**Create Public Threads**、
> **Send Messages** 和 **Send Messages in Threads**。只授予“Manage Threads”
> **没有**用。
>
> 当 bot 在某个频道缺少这些权限时，**`/clord` 会（以仅你可见的方式）回复
> 需要授予的具体权限**（#443）——无论 bot 能否看到该频道，斜杠命令都能
> 送达它。请先把权限修好再继续：没有 **View Channel**，bot 收不到那个频道里的
> 任何消息，你在那里的子区中写的内容就永远到不了 Claude。

### 第 2 步——运行设置向导

向导会替你写好 `.env`，不需要手动编辑任何东西。

```bash
# 直接从 GitHub 运行，无需安装：
uvx --from "git+https://github.com/yousan/c-lord.git" c-lord setup

# 或者从克隆运行（要把它作为服务运行时也应该用这种方式）：
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv run c-lord setup
```

它会按以下顺序询问六项内容：

1. **Claude Code CLI**——只检查 `claude` 能否运行，不能的话会告诉你。
2. **Bot token**——token 会通过 Discord API 验证（以 `Bot xxx` 形式粘贴的 token 会保存为 `xxx`；连续 3 次留空或无效后，它会停止，并且不写入 `.env`）。
3. **频道**——列出 bot 能看到的文字频道；按编号选择一个（不必复制 ID），或者粘贴数字形式的频道 ID（频道 URL 或 `²` 这样的数字会被要求重新输入）。它将成为 `DISCORD_CHANNEL_ID`，即 bot 的主频道。
4. **你的 Discord 用户 ID**——可选；它决定谁会被 @ 提及。如果填的是用户名而不是数字 ID，会被要求重新输入，因为 `c-lord start` 不接受用户名。
5. **工作目录**——没有自己克隆的运行所使用的默认目录。
6. **模型**——`sonnet`、`opus` 或 `haiku`。

然后它写入 `.env`（用 `--env PATH` 可以写到别处），并询问是否立即启动 bot。

### 启动 / 停止

```bash
c-lord start    # 启动 bot（读取当前目录下的 .env）
c-lord start --env /path/to/.env   # 指定 .env 的位置
c-lord version  # 打印正在运行的构建版本，例如 v1.4.183-bd80c47e-20260908
```

在克隆中运行时，请在这些命令前加上 `uv run`。这样启动的 bot 在前台运行，按 Ctrl-C 停止。要让它持续运行，请参见[作为服务运行](#作为服务运行systemd---user重启后依然存活)。

> **配置优先级（#324）：** `.env` 文件中定义的键总是优先于继承来的进程环境变量——
> 你从哪个目录启动，就决定了 bot 的身份。文件中*没有*的键仍会回退到进程环境，
> 所以只用环境变量的配置方式照常可用。

### 第 3 步——绑定仓库并启动会话

会话通过斜杠命令启动，而不是通过在频道里直接输入的普通消息（这类消息会被忽略，因此频道仍然可以当作普通的聊天场所）。

```text
/clord-init repo:https://github.com/you/your-project.git     ← 每个频道执行一次
/clord Fix the flaky login test                               ← 新建一个子区并在其中启动 Claude
```

`/clord-init` 记录该频道使用哪个仓库。默认情况下，Discord 只向拥有 **Manage Server** 权限的成员提供这个命令。此后每次 `/clord` 都会新建一个子区，为它克隆该仓库，并在克隆中启动 Claude Code。你在**该子区内**写的所有内容都会发给同一个 Claude 会话。如果某个子区要用别的仓库，请指明：`/clord repo:<url> <prompt>`——这在没有绑定的频道里也可用。

主机使用它自己的 git 凭据进行克隆，所以私有仓库需要在主机上配置好 SSH 密钥或 credential helper。克隆失败时，子区会显示 git 本身的错误信息以及需要检查的地方（[规范](../specs/workspace-setup-failure.md)）。

### 作为服务运行（systemd --user，重启后依然存活）

要让 c-lord 在后台持续运行，并在**主机重启时自动启动**，请把它安装为
`systemd --user` 服务。在你的克隆中执行：

```bash
bash scripts/install-systemd.sh
```

这会把 [`deploy/c-lord.service`](../../deploy/c-lord.service) 原样安装为
`~/.config/systemd/user/c-lord.service`，把主机特有的值（这个克隆的路径、你的
`uv`、你的 `PATH`）写入 drop-in 文件 `c-lord.service.d/10-install.conf`，
启用该服务，并打开 **linger**，这样即使没有人登录，服务也会在开机时启动。
`deploy/c-lord.service` 有改动后请重新运行一次。之后用标准的 systemd 命令管理：

```bash
systemctl --user restart c-lord.service     # 在 `git pull` 之后
systemctl --user stop    c-lord.service
systemctl --user status  c-lord.service
journalctl --user -u c-lord.service -f      # 实时日志
```

unit 安装之后，**这个克隆的 bot 只能由 systemd 来启动。** 不要杀掉它再手动启动——
systemd 会尝试重新拉起它，撞上单实例锁，然后放弃（`Start request repeated too quickly`），
结果 bot 就在无人监管的状态下运行。`bash scripts/staging.sh restart|stop`
了解这一点：在某个用户 unit 的 `WorkingDirectory=` 所指向的克隆中，它会调用
`systemctl --user restart|stop` 而不是杀进程；当 `systemctl --user` 不可用时，
它会报错退出（不杀进程）。`bash scripts/staging.sh status` 会显示每个 bot
进程是否位于该 unit 的 cgroup 内。

为什么用 `--user` 而不是系统服务：c-lord 驱动的是用户的 `tmux`、`~/.claude`
会话文件、`uv` 和 `claude` CLI，所以以**你本人**的身份、带着你的环境运行最为干净——
root／系统服务则必须通过 `User=`／`Environment=` 把这一切重新搭建一遍。
`loginctl enable-linger "$USER"` 让用户服务在注销后继续存活并在开机时启动；
安装脚本会替你执行它。

> **手动安装：** 复制 [`deploy/c-lord.service`](../../deploy/c-lord.service)，改好两处
> `CHANGE-ME` 路径（克隆目录和 `uv`），然后执行
> `systemctl --user daemon-reload && systemctl --user enable --now c-lord.service && loginctl enable-linger "$USER"`。

> **WSL 说明：** 如果 `systemctl --user` 报告 `Failed to connect to bus`，说明用户的
> D-Bus 会话没有起来。请确认 `/etc/wsl.conf` 的 `[boot]` 下有 `systemd=true`，然后重启
> WSL（`wsl --shutdown`）；或者通过杀掉 bot、让 `Restart=always` 重新拉起它的方式来重启
> （有多个克隆时 `pkill -f c_lord.main` 不安全——请按 PID 杀）。

## 在子区中工作

下面是你在 Discord 中实际看到的内容。每一项都链接到定义其应有行为的页面；完整索引见 [docs/specs/](../specs/README.md)，日常使用指南见 [docs/USER_GUIDE.md](../USER_GUIDE.md)。

### 与 Claude 对话

- **一个子区，一段对话。** 在子区里回复会延续同一个 Claude Code 会话（`--resume`），上下文保持完整——无论是今天、明天，还是 bot 重启之后。
- **新消息会打断当前工作。** 如果你再次发言时 Claude 还在工作，正在运行的回合会被中断，改为执行你的新指示。不需要先 `/stop`。
- **停止。** `/stop`，或者回合运行期间位于子区底部的 **⏹ Stop** 按钮，会停止该回合以及 Claude 留在后台运行的 shell 命令。对话会保留；你的下一条消息会接着进行（[规范](../specs/stop-button.md)）。
- **发送内容没有长度限制。** 粘贴的长文会完整送达。如果某条消息确实无法送达，子区会明确说明——绝不会悄无声息地结束一个空回合（[规范](../specs/input-delivery.md)）。
- **传入文件。** 一条消息最多可附 10 个文件。每个文件都保存在该子区的克隆内（`.clord/attachments/…`，不纳入 git），并把路径告诉 Claude（[规范](../specs/attachments.md)）。
- **传出文件。** Claude 交付的文件会以原文件名附加到子区，默认上限 25 MB（[规范](../specs/user-file-delivery.md)）。
- **长回答完整送达。** 超过 Discord 2000 字符上限的回答会拆成多条消息，并且不会拆断代码块（[规范](../specs/long-reply-split.md)）。
- **终端依然可用。** 对该子区的窗口执行 `tmux attach` 然后输入；你在那里输入的内容会作为你的发言出现在子区里，Claude 的回答照常跟在后面（[规范](../specs/mirrored-events.md)）。
- **bot 停机期间发布的消息不会丢失。** 重新连接或重启后，它会补读这段时间内发到各子区的消息并执行，同时用一行文字说明延迟的原因（[规范](../specs/gateway-backfill.md)）。
- **子区名称由你做主**——c-lord 绝不会自行重新概括子区名称。子区保留创建时的名称（只在前后加上 `W<N> │` 前缀和 `#<issue>` 编号）；想根据最近的对话重新生成概括时，在子区里运行 `/thread-rename`（sonnet）。`CLORD_AUTO_TOPIC=1` 可恢复旧的自动概括（#705）

### 了解当前状况

- **实时状态**——你的消息上的表情反应状态灯：Claude 工作时为 🟢 运行中，轮到你时为 🟡 等待中（出错时 ❌，沉默 10 秒 / 30 秒时 ⏳ / ⚠️）。高负载下反应依然及时。子区名称里的 🟢/🟡 状态灯默认关闭（#329）；设置 `CLORD_THREAD_LAMP=1` 可启用这个更新较慢、最终一致的侧边栏视图（#246）
- **工具活动**——从 Claude Code 自己的会话记录（transcript）中读取，而不是每个工具发一条：陷入沉默的回合会显示一行进度（`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`，意为“工作中 5:56 · 🔧 Bash(…) · 工具 61 次”，见下文*回合进度行*），该回合的工具调用和进度语句会作为 `progress.txt` 附在回答上（#723）
- **回合进度行**——回合沉默 90 秒后，会出现一行小字（`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`），每 15 秒原地刷新，一旦有真正的输出返回就立即消失；连工具活动都停止时显示 `⏳ 待機中`（“等待中”）。当 Claude 写下进度语句（窗格中以“●”开头的行）时，这一行显示最新的那句而不是工具（#883）——这些语句同样会进入 `progress.txt`，绝不会单独成为消息。回合之外不会发布。用 `CLORD_TURN_PROGRESS=0` 关闭（#539）
- **上下文用量**——每个回合以一行页脚结束（`📊 7% context (69k/1.0M) · …`）：上下文窗口占用百分比（输入 + 缓存 token，不含输出）、模型、版本和费用；超过 83.5%、下一回合可能触发自动压缩时，它会变成 ⚠️ 警告（`docs/specs/context-footer.md`）
- **压缩通知**——Claude Code 压缩上下文时，子区收到的是一行 `🗜️ コンテキストを圧縮しました`（“已压缩上下文”），而不是原始的续接摘要（#628）
- **硬停滞灯**——30 秒没有任何活动后，你的消息上的反应变为 ⚠️（扩展思考或上下文压缩时也可能这样安静）；不会向子区发布消息（#473）
- **回合结束提醒**——回合结束时，子区会 @ 提及发起请求的人，这样你会收到通知。没有产生回答的回合会如实说明，而不是声称已完成（[规范](../specs/turn-end-notification.md)）。
- **超时通知**——带有已用时间和恢复指引的嵌入消息，只在 Claude 真正卡死时发出（窗格在整个时间窗口内没有变化，*并且*不是空闲地停在提示符处）；正常结束的回合绝不会触发它（#541），等待你回答的提问／计划菜单也不会（#751）
- **用量限额与登录**——触及套餐的用量限额时会如实报告，并附上重置时间（[规范](../specs/usage-limit.md)）。Claude Code 的登录过期时，所有者会收到一条私信，写明主机和停下的子区，并可以直接通过这条私信重新登录（[规范](../specs/login-alert.md)）。
- **📊 Session Status 面板**——默认关闭（#761）。设置 `CLORD_SESSION_STATUS_BOARD=1` 后，会在 `DISCORD_CHANNEL_ID` 中维护一块实时面板，把每个会话列为 🟢 Auto-processing / 🟡 Waiting for input。关闭时，bot 启动不会向该频道发布任何内容，并会撤掉之前启动留下的面板；子区里回合结束时的 `🟡 Claude has finished … @you` 提醒不受影响。见 `docs/specs/session-status-board.md`

### 当 Claude 向你提问时

- **提问**——Claude 调用 `AskUserQuestion` 时，选项会以按钮或选择菜单的形式出现在子区里，Claude 按你选的继续。bot 重启后这些按钮仍然有效（[规范](../specs/ask-menu-lifecycle.md)）。
- **Plan Mode**——Claude 调用 `ExitPlanMode` 时，Claude Code 显示的批准菜单会以与 `AskUserQuestion` 相同的方式桥接到 Discord：选项以按钮形式出现，Claude 按你选的那一项继续（#251）
- **工具权限提示**——不在 Discord 上显示：c-lord 会在窗格中接受 Claude Code 的权限提示，让回合继续进行。Claude 能做什么取决于会话运行时的权限模式／设置，而不是逐个工具的按钮

### 拆分工作

- **子代理日志**——在一个子区内拆分给子代理的工作会留在 Discord 上：每次运行有一行 `🤖 サブエージェント「…」開始` / `終了 · 6:12 · ツール 48 件`（“子代理「…」开始”／“结束 · 6:12 · 工具 48 次”），结束行附带 `subagent-*.md`（完整报告、任务说明、工具调用），开始行上的那份副本在运行期间会不断更新。`CLORD_SUBAGENT_VOICES=1` 还会以各代理自己的名字发布它们的消息和报告（#971，`docs/specs/subagent-log.md`）

**什么时候新开子区，什么时候在一个子区内拆分工作**（#969）：只有当工作需要自己的分支、自己的开发服务器，或者需要与你单独对话时，才新开子区。要拆分同一项工作——调研、审查、整理——请让 Claude 在子区内使用子代理：不会启动额外的 `claude` 进程（每个约 285 MB），每个子代理的开始、结束和完整报告仍然会出现在子区里（[子代理日志](../specs/subagent-log.md)）。Agent Teams（`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`）是实验性功能，c-lord 重启或恢复会话后，它的队友不会回来；请不要使用它的分屏显示（`--teammate-mode tmux`），那会往 c-lord 驱动的窗口里添加窗格。

### 技能、模型、提交

- **技能**——`/skill` 运行 `~/.claude/skills/` 中的某个 Claude Code 技能。名称支持自动补全，bot 运行期间新增的技能会在一分钟内出现。在频道里使用会新开一个子区；在子区里使用则延续该会话。
- **运行时切换模型**——`/model` 可在会话、频道或全局范围内查看并更改模型和 effort（`include-existing` 还会让已打开的会话从下一回合起切换）
- **是谁要求的提交**——Claude 在子区里创建的提交带有 `Co-authored-by:` trailer，分别记录发起该回合的 Discord 用户和 Claude。这是由 git hook 完成的，而不是靠要求 Claude 记住；`CLORD_COAUTHOR=0` 可关闭（[规范](../specs/commit-coauthors.md)）。

## 工作区

**工作区**是属于一个子区的全部东西：它的 Claude 会话、它的克隆，以及从该克隆启动的 docker 环境。

- **每个子区一个 git 克隆**——频道绑定到仓库（`/clord-init`）后，每个子区都在自己独立的 `git clone` 中工作；会话之间除了 git 远程仓库之外什么都不共享
- **三个级别的“关闭”，每一级都包含前一级。** 这些操作都不会删除对话历史或 docker 卷（[术语表](../specs/workspace-vocabulary.md)）：

  | | 停止的内容 | 自动触发 | 手动操作 |
  |---|---|---|---|
  | **休眠** | `claude` 进程（docker 继续运行） | 空闲 4 小时后（`CLORD_IDLE_SLEEP_HOURS`） | ——下一条消息会悄悄把它唤醒 |
  | **停止** | ＋ docker 环境 | 空闲 7 天后（`CLORD_IDLE_STOP_DAYS`） | `/workspace-stop`，用 `/workspace-start` 恢复 |
  | **删除** | ＋ 克隆本身 | 30 天未使用后，见下文 | `/workspace-delete` |

- **保持唤醒的数量有上限。** 超过 `CLORD_MAX_RESIDENT_WORKSPACES`（默认根据主机内存计算）时，空闲最久的工作区会被置于休眠。新建工作区绝不会因此等待（[规范](../specs/resident-cap.md)）。
- **频道状态**——`/clord-status` 列出本频道的会话，包括目录大小、`tmux attach` 的目标和 `claude --resume` 命令；`show_all` 会把已关闭的会话也列出来（类似 `docker ps -a`）。它取代了旧的 `/sessions`、`/session-dirs`、`/resume-info`。
- **工作区清理**——`/workspace-cleanup` 回收没有任何运行中的会话在使用的克隆目录（用 `dry_run` 预览）；存有工作成果（未提交的改动、未推送的提交、`.env` 之类被忽略的文件）的目录绝不会被删除，回复中会指出是哪些（#1016）
- **30 天整理，并附带通知**——每次启动时，**30 天**未使用的工作区会被整理。这个期限与 Claude Code 自己的会话记录保留期（`cleanupPeriodDays`）一致，所以到那时 Claude 通常本来也已经不记得这段对话了。只有当克隆中没有未提交的工作时才会删除它；把子区与其会话关联起来的记录会保留。仍然打开的子区会收到一条 🧹 通知，说明发生了什么、保留了什么，然后被归档；已经关闭的子区则不发消息直接整理，因此保持关闭状态。之后在这样的子区里发言，会重新连接到磁盘上仍然存在的内容，或者在同一子区里开始一段新对话。详情：[Why a thread loses its record](../COMMANDS.md#why-a-thread-loses-its-record-the-30-day-sweep)。
- **克隆之间共享 git 对象。** 同一仓库的克隆都从一个本地镜像借用对象，所以新子区的克隆只需几秒钟，占用的磁盘也很少。你使用 git 的方式没有任何变化；`CLORD_GIT_MIRRORS=0` 可关闭（[规范](../specs/git-object-mirrors.md)）。

## 命令

所有命令的说明都在 **[docs/COMMANDS.md](../COMMANDS.md)** 中。最常用的有：

| 命令 | 使用位置 | 作用 |
|---|---|---|
| `/clord-init repo:<url>` | 频道 | 把频道绑定到仓库（不带参数：显示绑定情况） |
| `/clord <prompt>` | 频道 | 新开一个子区并启动会话；`repo:<url>` 为该子区指定仓库 |
| `/stop` | 子区 | 停止正在运行的回合；对话会保留 |
| `/clear` · `/compact` | 子区 | 在同一子区里从头开始 · 概括上下文以腾出窗口空间 |
| `/skill <name> [args]` | 频道或子区 | 运行一个 Claude Code 技能 |
| `/model` | 任意位置 | 查看或更改会话、频道或全局的模型和 effort |
| `/clord-status` | 任意位置 | 本频道的会话，附带 `tmux attach` 的目标和 `claude --resume` 命令 |
| `/workspace-stop` · `/workspace-start` · `/workspace-delete` · `/workspace-cleanup` | 子区 / 任意位置 | [工作区](#工作区)操作 |
| `/claude-restart` · `/resync` · `/tmux-screenshot` | 子区 | 重启 Claude 进程 · 把子区重新连接到它的窗格 · 发布窗格的截图 |
| `/thread-rename` | 子区 | 重新概括子区名称 |
| `/clord-reattach` | 子区 | 把已被整理的子区重新连接到磁盘上仍然存在的内容 |
| `/version` | 任意位置 | 正在运行的是哪个构建版本 |

大多数斜杠命令也有以 `!` 开头的文本命令形式（`!clord`、`!stop`……）。Webhook 能发送的就是这些——Discord 不允许 Webhook 使用斜杠命令——脚本和其他代理也是通过它们来启动工作的：向已绑定仓库的频道发布一条以 `!clord` 开头的 Webhook 消息，就会开启一个会话。

## 配置

所有设置都写在 `.env` 中。只有前两项是必填的。[.env.example](../../.env.example) 是一份带注释的起点，而不是完整列表：下面表格中的一些变量并不在其中，它里面也有几项（用于测试和监控工具）没有列在这里。

**连接**

| 变量 | 作用 | 默认值 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | bot 的 token | （必填） |
| `DISCORD_CHANNEL_ID` | 用于 Claude 聊天的频道 ID。与这里所有的 ID 设置（`DISCORD_OWNER_ID`、`COORDINATION_CHANNEL_ID`、`EXPECTED_BOT_USER_ID`）一样，只能是 ASCII 数字——填入链接、名称或 `²` 会让 `c-lord start` 停止，并给出该设置的名称和值（#1084）；数字前后的空格会被忽略（#1194） | （必填） |
| `EXPECTED_BOT_USER_ID` | bot 自己的用户 ID。如果进程以另一个 bot 的身份登录，会立即退出——一台主机上运行多个 bot 时值得设置 | （关闭） |

**Claude Code**

| 变量 | 作用 | 默认值 |
|---|---|---|
| `CLAUDE_COMMAND` | 要运行的 Claude Code 可执行文件 | `claude` |
| `CLAUDE_MODEL` | 新会话使用的模型。`/model` 可在运行时更改 | `sonnet` |
| `CLAUDE_EFFORT` | 以 `--effort` 传递的推理强度（`low` / `medium` / `high` / `xhigh` / `max`）。未设置时不传递该参数 | （CLI 默认值） |
| `CLAUDE_PERMISSION_MODE` | 会话运行时使用的权限模式 | `acceptEdits` |
| `CLAUDE_WORKING_DIR` | 没有自己克隆的运行（未指定 `working_dir` 的定时任务或 Webhook 触发器）所使用的目录 | 当前目录 |
| `CLORD_SESSION_PLUGINS` | 设为 `0` 时，c-lord 启动会话时不再排除 Discord 插件和用不到的语言服务器插件（[规范](../specs/session-plugins.md)） | `true` |

**谁可以使用**

| 变量 | 作用 | 默认值 |
|---|---|---|
| `DISCORD_OWNER_ID` | 获准操作 bot 的用户 ID，Claude 需要输入时也会 @ 提及此人。未设置时，bot 只允许 Discord 应用的所有者（#713） | （可选） |
| `CLORD_ALLOWED_ROLE` | Discord 身份组名称，其成员可以操作 bot（与 `DISCORD_OWNER_ID` 为“或”的关系） | （可选） |
| `CLORD_ALLOW_ANYONE` | 设为 `1` 时，服务器的**每一位**成员都可以操作 bot——这意味着可以在主机上执行 shell 命令。启动时会发出警告 | `false` |
| `CLORD_NOTIFY_DENIED` | 设为 `0` 时，未获许可的人在 bot 子区里发言后，应用所有者不再收到私信（发送者始终看不到任何提示；拒绝仍会记入日志——#346） | `true` |
| `CLORD_HOST_LABEL` | 登录失效私信中对这台机器的称呼（#1309——用于区分第二台主机） | 主机名 |
| `CLORD_LOGIN_RELAY` | 设为 `0` 时，保留登录失效私信，但不再提供通过它重新登录的功能（登录链接 + 回复验证码，#1310） | `true` |

**限制与工作区**

| 变量 | 作用 | 默认值 |
|---|---|---|
| `MAX_CONCURRENT_SESSIONS` | **同时运行的回合数**上限。不是常驻 `claude` 进程数的上限——信号量只包住回合的执行，回合结束即释放，而 tmux 窗格继续存在。常驻上限见 `CLORD_MAX_RESIDENT_WORKSPACES`（#576）。必须是 **≥ 1** 的整数——`0`、负数和非数字会让启动停止，并指出该键名（#1119）。超过 18 位的数字会被忽略，同时输出指出该键名的 WARNING，并使用默认值（#1256）。 | `3` |
| `CLORD_MAX_RESIDENT_WORKSPACES` | 同时持有存活 `claude` 的工作区数量上限。超过上限时，空闲最久的会被置于休眠；新建工作区绝不会被阻止。与空闲 TTL 不同，这个默认值**确实**取决于主机，所以它根据 `MemTotal`（优先采用 cgroup 限制）计算，而不是作为常量随包发布。`0` 表示禁用。见 `docs/specs/resident-cap.md`。 | 自动（`max(2, MemTotal_GiB × 0.4 / 0.45)`） |
| `SESSION_TIMEOUT_SECONDS` | 会话无活动超时。须为 **≥ 1** 的整数，否则启动停止并指出该键名（#1119）。超过 18 位的数字会被忽略，同时输出指出该键名的 WARNING，并使用默认值（#1256） | `300` |
| `CLORD_IDLE_SLEEP_HOURS` | 工作区休眠前的空闲小时数（可以是小数）。`0` 表示禁用 | `4` |
| `CLORD_IDLE_STOP_DAYS` | 工作区被停止前的空闲天数。`0` 表示禁用 | `7` |
| `CLORD_ORPHAN_SWEEP_DAYS` | 没有任何记录指向、干净且未被动过的克隆在多少天后被删除。`0` 表示禁用 | `30` |
| `SESSION_DIR_BASE` | 各子区克隆的存放位置。不要移动已有的目录：`--resume` 以目录路径为键 | `~/.c-lord/<instance>/sessions` |
| `CLORD_INSTANCE` | 上面的 `<instance>` | 克隆的目录名 |
| `CLORD_GIT_MIRRORS` | 设为 `0` 时，同一仓库的克隆不再共享 git 对象 | `true` |

**通知与事件**

| 变量 | 作用 | 默认值 |
|---|---|---|
| `CLORD_OWNER_FALLBACK` | 对于不是由人发起的回合（Webhook / CI / 调度器），回退通知所有者的范围：`all`（回合结束的 🟡 + 暂停 + 失败）、`blocked`（暂停 + 失败——正常时安静，出问题时提醒）、`off`（从不通知，失败也不通知） | `blocked` |
| `CLORD_EVENT_LOG` | 每当回合结束（`completion` / `blocked` / `failure`），在最后一条回复发布之后，向该文件追加一行 JSON——供派发任务的代理或 CI 用 `tail -F` 跟读，而不必轮询子区。仅在服务器端；见 [docs/specs/turn-end-events.md](../specs/turn-end-events.md)（#917） | （关闭） |
| `CLORD_EVENT_WEBHOOK_URL` / `CLORD_EVENT_WEBHOOK_SECRET` | 把同一事件以 JSON 形式 POST 到该 URL，并带上 `X-Clord-Signature: sha256=<HMAC-SHA256 of the body>`。没有设置 secret 时不发送任何内容。失败会重试几次，绝不会阻塞回合（#917） | （关闭） |
| `CLORD_EVENT_DEFER_MAX_SECONDS` | 后台子代理仍在运行时，`completion` 最多被扣留多久，之后无论如何都会发送 | `3600` |
| `COORDINATION_CHANNEL_ID` | 某个会话的回合结束时收到一行通知的频道；也是 AI Lounge 的默认频道。比 Discord ID 更长的数字（20 位）会被忽略，启动时输出指出该键名的 WARNING，视同未设置（#1256） | （可选） |

**子代理**

| 变量 | 作用 | 默认值 |
|---|---|---|
| `CLORD_SUBAGENT_LOG` | 设为 `0` 时关闭子代理日志：子代理每次运行有一行 `🤖 … 開始` / `終了`，结束行附带 `subagent-*.md`（完整报告、任务说明和每一次工具调用），开始行上的那份副本在代理工作期间会被替换。见 [docs/specs/subagent-log.md](../specs/subagent-log.md)（#971） | `true` |
| `CLORD_SUBAGENT_VOICES` | 设为 `1` 时，还会**以发言者的名字**发布父会话分派的任务、父会话与其代理之间的每一条 `SendMessage`，以及每份最终报告——这样就能在子区里看清谁对谁说了什么。bot 拥有 Manage Webhooks 权限时使用 Webhook（非必需：没有该权限时把名字放在文本前面）。工具调用仍留在附件里（#971） | `false` |
| `CLORD_SUBAGENT_PROGRESS_SECONDS` | 运行中的子代理所附进度文件的替换间隔（仅在内容有变化时；最小 10） | `60` |

**子区显示的内容**

| 变量 | 作用 | 默认值 |
|---|---|---|
| `CLORD_RENDER_TABLE_IMAGES` | 设为 `1`、`true` 或 `yes` 时，把 GFM 管道表格渲染成 PNG 图片附加到 Discord 消息上 | （可选） |
| `CLORD_SHOW_URL_EMBEDS` | 设为 `1`/`true`/`yes`/`on` 时，允许 Discord 为 Claude 回复中的 URL 展开 OGP／链接预览卡片。默认关闭——回复保持紧凑（没有预览卡片）。 | `false` |
| `CLORD_AUTO_TOPIC` | 设为 `1`/`true`/`yes`/`on` 时，c-lord 会在**新**子区的第一条消息时用 LLM（haiku）概括出子区名称——即 #705 之前的行为。默认关闭：子区保留创建时的名称，需要时用 `/thread-rename`（sonnet）重新概括。 | `false` |
| `CLORD_SESSION_STATUS_BOARD` | 设为 `1`/`true`/`yes`/`on` 时，在 `DISCORD_CHANNEL_ID` 中显示 📊 Session Status 面板（#761）。默认关闭：启动时不向该频道发布任何内容，并撤掉之前启动留下的面板。子区里的回合结束提醒不受影响。 | `false` |
| `CLORD_THREAD_RETITLE` | 设为 `1`/`true`/`yes`/`on` 时，如果 LLM 判断工作内容已经变化，就在对话中途重新给子区起名（#121）。默认关闭（#414）——它改名改得太频繁。 | `false` |
| `CLORD_THREAD_LAMP` | 设为 `1` 时，在子区名称上添加 🟢/🟡 状态灯（侧边栏视图）。默认关闭，因为 Discord 对子区改名的限制很严；你的消息上的反应状态灯始终开启（[规范](../specs/thread-lamp.md)） | `false` |
| `CLORD_TURN_PROGRESS` | 设为 `0` 时关闭回合进度行；`CLORD_TURN_PROGRESS_QUIET_SECONDS` 调整子区需要先沉默多久 | `true` / `90` |
| `CLORD_COAUTHOR` | 设为 `0` 时不再给 Claude 的提交添加 `Co-authored-by:` trailer | `true` |

**进程与 tmux**

| 变量 | 作用 | 默认值 |
|---|---|---|
| `CLORD_TMUX_SCREENSHOT_ROWS` | `/tmux-screenshot` 在截图前把窗口扩大到的高度（行数）。`0` 表示按原样截取 | `100` |
| `CLORD_INSTANCE_ID` | 用于区分共用同一个 tmux 服务器的多个 c-lord 实例。只有当两个实例从同一目录启动时才需要设置 | 启动目录的真实路径 |
| `CLORD_SHUTDOWN_TIMEOUT_SECONDS` | 收到 SIGTERM/SIGINT 后，进程最多可以用多久退出；超时后它会把堆栈转储到日志，并以状态码 70 退出。`0` 表示禁用 | `10` |

REST API 有它自己的设置（`CLORD_API_HOST`、`CLORD_API_PORT`、`CLORD_API_URL`、`CLORD_API_SECRET`、`CLORD_API_ALLOW_ANY_PEER`）——见 [REST API](#rest-api)。

### URL 链接预览（CLORD_SHOW_URL_EMBEDS）

当 Claude 的回复中包含 URL（例如 GitHub Issues 链接）时，Discord 通常会把它展开成一张很高的 OGP 预览卡片（标题、描述、图片），把真正的回答挤到下面，让子区显得杂乱。c-lord 默认**抑制**这些预览（每条回复都设置了 `SUPPRESS_EMBEDS` 标志），所以链接保持在行内，回答读起来干净利落。设置 `CLORD_SHOW_URL_EMBEDS=true` 可恢复 Discord 默认的链接预览展开。

### 表格图片渲染（CLORD_RENDER_TABLE_IMAGES）

启用后，Claude 回复中的 Markdown 管道表格会用 [Pillow](https://python-pillow.github.io/) 渲染成 PNG 图片，与文本一起附加到 Discord 消息上。每个单元格被拆分为文本段和表情符号段：**文本用支持 CJK 的字体绘制，表情符号用彩色表情字体以全彩绘制**，所以 🟢/🔴 状态灯能保留颜色。

**镜像发布的每一条消息都在覆盖范围内——进度更新和最终回答都一样**（#683）。Discord 完全不绘制 Markdown 表格，没有被渲染的表格就是一列原始的 `|` 字符，而中间消息在回合结束后仍留在子区里。因此，如果只对最终回答这样处理，读者就无法预料哪些表格是可读的。

**图片跟随表格所在的那条消息。** 超过 2000 字符的回复会被拆成多条消息；每个表格的 PNG 附加在包含该表格的那条消息上（被拆分切在两行之间的表格会完整地绘制在它开始的那条消息上）。在 #750 之前，所有图片都附在*最后一条*消息上，于是前半部分的表格在原处仍是原始的管道符，而它的图片却挂在后面某个不相关的段落下。`progress.txt` 仍然附在最后一条消息上。

**每条消息最多 10 个表格转成图片。** 这是 Discord 的附件上限，超过它会导致整条消息发送失败——包括文本——所以第十个之后的表格保持原始 Markdown，而不是让整条消息丢掉。这个限制是按消息计的，不是按回合计的：每条进度消息都有自己的额度。当回复还带有 `progress.txt` 时，该文件占一个名额，表格使用其余九个。

**单元格内的行内 Markdown 在绘制前会折叠为纯文本**——图片无法交互，保留原始语法只会带来杂讯。`[label](url)` / `![alt](url)` 变成 `label`（URL 在图片里无法点击，只会添乱），`**bold**` / `*italic*` / `` `code` `` 只保留内部文本。下划线强调（`_x_`、`__x__`）被有意保持原样，以免 `_is_allowed` / `__init__` 这样的标识符被破坏。

**`~~strike~~` 是例外：它会被画成真正的删除线。** 图片*能够*在文字上画一条线，而 Claude 在表格里用删除线表示“这一行是要去掉的”——Discord 在消息正文里会给它加删除线，如果在图片里把它折叠成纯文本，两者就会不一致，表格的意思也会被颠倒。删除线会跟随文本跨越换行，对单元格的一部分（`~~a~~ b`）和整个单元格都适用。

**安装：**

```bash
uv add "c-lord[table]"
# 或者：pip install "c-lord[table]"
```

> 同一个 `[table]` extra 也为 **`/tmux-screenshot`** 提供支持——这是一个调试命令，
> 会发布当前 tmux 窗格的 PNG（保留 ANSI 颜色、布局和状态灯）。没有安装该 extra 时，
> 这个命令会回复安装提示，而不是图片。

**字体支持（CJK 文本 + 彩色表情）：**

c-lord 分别渲染文本和表情符号，各组中使用找到的第一个字体：

| 字体文件路径 | 分组 / 备注 |
|---|---|
| `~/.local/share/fonts/NotoSansJP.ttf` | **文本**——推荐用于日语 |
| `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` | 文本——覆盖日文 / 中文 / 韩文 |
| `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | 文本——最后的后备（仅拉丁字符） |
| `~/.local/share/fonts/NotoColorEmoji.ttf` | **彩色表情**——🟢🔴🟡✅⚠️ 以全彩渲染 |
| `/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf` | 彩色表情——另一个系统路径 |
| `~/.local/share/fonts/NotoEmoji-Regular.ttf` | 单色表情——没有彩色字体时的后备（没有颜色，但不会出现豆腐块） |

如果没有彩色表情字体，c-lord 会回退到单色表情字体（有字形，没有颜色），再回退到直接绘制原始字符。请使用 **CBDT** 版的 Noto Color Emoji——Pillow 用 `embedded_color=True` 渲染它。

> **截图里的日文变成了空方框（□）？** 那是*这台*机器／这个用户缺少字体，不是 bug——
> 上面的路径一个都没找到，于是渲染回退到了只含拉丁字符的字体。字体是按用户、
> 按主机安装的（`~/.local/share/fonts/`），所以即使在别处已经正常，也不会自动带到
> 另一个 Linux 用户账户或一台新主机上。
>
> **如果你不习惯自己运行终端命令，也没关系——把下面这段直接交给你的 AI 助手**
> （你的 Claude Code 会话，或者 c-lord 的 Claude 子区本身），让它替你运行即可。
> 这段命令的写法保证 AI 可以在无人看管的情况下安全执行：它只写入你自己的主目录，
> 不需要 `sudo`，也不会触碰 `~/.local/share/fonts/` 之外的任何东西。

**日语 + 彩色表情的快速安装（Linux）：**
```bash
mkdir -p ~/.local/share/fonts
# 日语文本
curl -Lo ~/.local/share/fonts/NotoSansJP.ttf \
  https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJK-Regular.ttc
# 彩色表情（CBDT）
curl -Lo ~/.local/share/fonts/NotoColorEmoji.ttf \
  https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf
fc-cache -fv
```

过长的单元格会按限定宽度换行（`MAX_COL_WIDTH`，按显示宽度计算，CJK 字符算两格），这样宽表格在手机上也保持可读。

> **其他语言的说明（中文、韩文、阿拉伯文、泰文等）：** 请安装覆盖目标文字的字体（例如简体中文用 Noto Sans SC，韩文用 Noto Sans KR），并把它的路径加入 `c_lord/discord_ui/table_renderer.py` 中的 `_JP_FONT_PATHS` 列表。欢迎贡献多语言字体检测功能。

## 定时任务

定时任务是 c-lord 按固定间隔替你运行的一段提示词。c-lord 只管*什么时候*；关于*做什么*的一切——检查什么、发布什么、如何避免重复——都写在提示词里。任务保存在 SQLite 中，在 bot 运行期间通过 [REST API](#rest-api) 添加、修改和删除，所以既不用改代码，也不用重启。通常的做法是在子区里让 Claude 来注册任务；Claude 可以从 `CLORD_API_URL` 得到 API 地址：

```bash
curl -X POST "$CLORD_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "weekly-deps",
        "prompt": "Check for outdated dependencies and open an issue if there are any",
        "interval_seconds": 604800,
        "channel_id": 123456789012345678
      }'
```

`name`、`prompt`、`interval_seconds` 和 `channel_id` 都是必填的，并且该频道必须已用 `/clord-init` 绑定到仓库；`working_dir` 和 `run_immediately` 是可选的。用 `{"enabled": false}` 调用 `PATCH /api/tasks/{id}` 可以暂停任务而不删除它。有一个循环每 30 秒醒来一次，启动所有到期的任务；上一次运行还没结束的任务会被跳过，不会重复启动。

每次运行都会发布一条 `🔄 **[Scheduled]** <task name>` 消息，在它上面开一个子区，在任务的 `working_dir` 中创建一个 tmux 窗口，并在那里运行 Claude——回答会像其他回合一样出现在子区里。定时运行应该是什么样子（以及什么算 bug）写在 [docs/specs/scheduled-tasks.md](../specs/scheduled-tasks.md) 中。

## GitHub 与 CI 自动化

任何能向 Discord Webhook 发消息的东西都可以把工作交给 Claude。根据提示词由谁来写，有两种方式。

**由发送方写提示词。** 在已绑定仓库的频道里，以 `!clord` 开头的 Webhook 消息会开启一个会话，效果与 `/clord` 完全相同。持有 Webhook URL 就等于拥有权限，所以请把这个 URL 当作机密对待。

**提示词固定在你这一侧。** `WebhookTriggerCog` 把消息前缀映射到你在代码中定义的提示词。Webhook 只能选择触发*哪一个*触发器，无法提供让 Claude 照着执行的文本。对 CI 来说这是更安全的选择。这个 cog 默认不注册——请在[你自己的 bot](#把-c-lord-当作库使用) 中添加它：

```python
from c_lord import ClaudeConfig, WebhookTrigger, WebhookTriggerCog

docs_sync = WebhookTrigger(
    prompt="Read the latest commits on main and update the docs to match. Open a PR.",
    working_dir="/srv/checkouts/my-project",
    timeout=900,  # 秒
)

await bot.add_cog(
    WebhookTriggerCog(
        bot=bot,
        runner=ClaudeConfig(model="sonnet"),
        triggers={"🔄 docs-sync": docs_sync},      # 消息前缀 → 要运行的内容
        channel_ids={123456789012345678},          # 只监听这里
        allowed_webhook_ids={234567890123456789},  # 只接受这个 Webhook
    )
)
```

除非你传入 `dangerously_skip_permissions=False`，否则 `WebhookTrigger` 会跳过 Claude Code 的权限检查，因为没有人在场回答提示——这也是应当限制 `channel_ids` 和 `allowed_webhook_ids` 的又一个理由。

**触发时发生什么：** Webhook 消息上会开一个子区，Claude 在触发器的 `working_dir`（或 runner 的）中属于它自己的 tmux 窗口里运行，回答发布到该子区。如果窗口无法创建（例如主机上 tmux 不可用），运行不会开始：子区会说明原因，Webhook 消息被加上 ❌，并提及 bot 所有者（#629）。

### 示例：自动文档同步（在你自己的 bot 中实现）

> **说明：** 这是一种你可以在自己的 bot 中实现的模式——c-lord 不内置文档同步工作流。参考实现请见 [EbiBot](https://github.com/ebibibi/discord-bot)。

每次向 `main` 推送时，你的 bot 可以触发 Claude Code 去：
1. 拉取最新改动并分析 diff
2. 更新英文文档
3. 翻译成日语（或任何目标语言）
4. 创建一个带双语摘要的 PR
5. 启用自动合并——CI 通过后自动合并

**GitHub Actions（添加到你的仓库）：**

```yaml
# .github/workflows/docs-sync.yml
on:
  push:
    branches: [main]
jobs:
  ask-claude:
    # Claude 自己的文档提交带有 [docs-sync]；没有这一行，它们会再次触发工作流
    if: "!contains(github.event.head_commit.message, '[docs-sync]')"
    runs-on: ubuntu-latest
    steps:
      - name: Post the trigger to Discord
        env:
          WEBHOOK: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: |
          curl --fail -sS -H "Content-Type: application/json" \
            --data '{"content": "🔄 docs-sync"}' "$WEBHOOK"
```

bot 这一侧就是上面展示的 `WebhookTriggerCog`，以 `🔄 docs-sync` 作为触发器。当 Claude 自己的 PR 合并进 `main` 时，这次推送本会再次触发工作流。`if:` 这一行会切断这个循环，前提是你的提示词要求 Claude 在提交信息中写上 `[docs-sync]`。

### 在回合结束时收到通知

派发了工作的脚本不必轮询子区。设置 `CLORD_EVENT_LOG` 或 `CLORD_EVENT_WEBHOOK_URL` 后，c-lord 会在每个回合结束、最后一条回复发布之后发出一个事件——`completion`、`blocked` 或 `failure`（[规范](../specs/turn-end-events.md)）。

### 通过 Webhook 升级 bot

`AutoUpgradeCog` 面向把 c-lord 作为包安装的 bot。与 `WebhookTriggerCog` 一样，需要你自己添加：

```python
from c_lord import AutoUpgradeCog, UpgradeConfig

await bot.add_cog(
    AutoUpgradeCog(
        bot,
        UpgradeConfig(
            package_name="c-lord",
            working_dir="/srv/my-bot",
            restart_command=["systemctl", "--user", "restart", "my-bot.service"],
            restart_approval=True,
            slash_command_enabled=True,
        ),
    )
)
```

当一条以 `trigger_prefix`（默认 `🔄 upgrade`）开头的 Webhook 消息到达时，这个 cog 会开一个子区，并在其中报告每一步：

1. 在 `working_dir` 中运行 `uv lock --upgrade-package <package_name>`，然后运行 `uv sync`（可分别用 `upgrade_command` / `sync_command` 替换）。
2. 记下哪些子区有回合正在运行，然后等待运行中的工作结束——每个暴露了 `active_count` 属性的 cog（`DrainAware` 协议）都会被询问，包括你自己的。超过 `drain_timeout`（300 秒）后不再等待。
3. 运行 `restart_command`。如果没有设置，就不会重启任何东西，子区会说明升级已完成。

重启之后，第 2 步记下的子区会收到一行不打扰人的通知，说明 c-lord 已重启。**不会**再次向 Claude 发送提示：`claude` 进程活在 tmux 里，从未被停止，所以没有什么需要恢复的（#406）。

`upgrade_approval=True` 会在第 1 步之前征求确认，`restart_approval=True` 会在新版本安装完成后、第 2 步和第 3 步之前征求确认；两者都可以通过在升级子区里添加 ✅ 反应或点击 Approve 按钮来答复。`slash_command_enabled=True` 会添加 `/upgrade`，它不需要 Webhook 就能启动同一套流程。只有获准操作 bot 的用户才能运行它。它在频道里和子区里都可以用——在子区里运行时，升级子区会开在父频道中——如果升级已经在进行，它会告诉你。

从自己的克隆运行的 c-lord 完全不需要这些：`git pull && systemctl --user restart c-lord.service`。

## REST API

这是 c-lord 的控制面，用于通知、定时任务和启动会话。它在每次运行时都会启动——
与回答的投递方式无关（#712/#543）。需要 aiohttp：

```bash
uv add "c-lord[api]"
```

它绑定到 `CLORD_API_HOST:CLORD_API_PORT`（默认 `127.0.0.1:8080`）。在**未设置**
`CLORD_API_PORT` 的情况下，端口被占用不是问题：API 会移到下一个空闲端口
（8081、8082……然后是任意空闲端口），并在日志中记录它去了哪里，所以同一主机上的两个
克隆都能得到 API，无需任何配置（#258）。你**设置了**的端口会被原样使用：如果它被占用，
bot 会记录一条 WARNING 并在没有 API 的情况下运行，而不是启动失败。不是 ASCII 数字的值
（`²`、某个单词）会被忽略，同时输出指出 `CLORD_API_PORT` 的 WARNING，视同未设置；
数字前后的空格会被忽略（#1218）。超过 65535 的数字属于你设置了但无法使用的端口：
WARNING 会指出 `CLORD_API_PORT`，bot 在没有 API 的情况下运行（#1237）。同样，
`CLORD_TRUSTED_BOT_IDS` 中不是 ASCII 数字、或者比 Discord ID 更长（20 位）的条目会被忽略，
启动时输出 WARNING，列表中的其他 ID 继续有效。这也包括全角数字（`１２３`），
它们以前会被读成 ID 123——请用 ASCII 数字书写 ID。

每个会话中的 Claude 都通过 `CLORD_API_URL` 环境变量得知 API 的位置——那是实际绑定的端口，
所以永远不需要手动让它与 `CLORD_API_PORT` 保持一致。只有在想让会话指向别处
（比如反向代理）时，才需要自己设置 `CLORD_API_URL`。API 没有运行时，
会话根本不会得到 `CLORD_API_URL`。

**它只为运行 bot 的那个 Unix 用户服务**（#457）。`POST /api/spawn` 会启动一个
Claude Code 会话，所以能访问这个端口就等同于以该用户身份拿到了 shell——而回环地址是
*网络*边界，不是 *UID* 边界：多用户主机上的每个账户都能连接 `127.0.0.1`。因此会从
`/proc/net/tcp` 读取对端的 UID，并与 bot 自己的 UID 比对，`/api/health` 也不例外；
其他人一律得到 `403`。无需任何配置——你自己的调用照常可用。要让其他用户或其他主机
访问，请设置 `CLORD_API_SECRET`，并让对方发送 `Authorization: Bearer …`。
见 [docs/SECURITY.md](../SECURITY.md#rest-api-exposure-712-457)。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 存活检查 |
| POST | `/api/spawn` | 新开一个子区，并用 `prompt` 在其中启动 Claude（可选 `channel_id`、`thread_name`）。子区一创建就返回响应 |
| POST | `/api/threads/{thread_id}/messages` | 把 `content` 发布到某个子区 |
| POST | `/api/threads/{thread_id}/sleep` | 让该子区的工作区休眠，效果与空闲休眠相同 |
| POST | `/api/notify` | 发布一条通知（`message`，可选 `title`、`color`、`channel_id`） |
| POST | `/api/schedule` | 稍后发布一条通知（`message`，ISO 8601 格式的 `scheduled_at`） |
| GET | `/api/scheduled` | 列出仍在等待的通知 |
| DELETE | `/api/scheduled/{id}` | 取消其中一条 |
| POST | `/api/tasks` | 注册一个[定时任务](#定时任务) |
| GET | `/api/tasks` | 列出任务 |
| PATCH | `/api/tasks/{id}` | 修改任务：`enabled`、`prompt`、`interval_seconds`、`working_dir` |
| DELETE | `/api/tasks/{id}` | 删除任务 |
| POST | `/api/mark-resume` | 在你自己重启 bot 之前登记一个子区，这样它会在下次启动时收到重启通知 |
| GET | `/api/lounge` | 读取最近的 AI Lounge 消息（会话并不知道 lounge 的存在，所以除非你自己做了安排，否则这里不会有内容——#758） |
| POST | `/api/lounge` | 向 AI Lounge 发布一条消息（可选 `label`）——c-lord 的会话不会主动调用它（#758） |

每个字段、状态码和边界情况都写在 [docs/COMMANDS.md](../COMMANDS.md#rest-api) 中。下面两个例子以 bot 自己的 Unix 用户身份运行，所以不需要任何请求头：

```bash
# 从脚本启动一个会话——响应中会带回子区的 ID
curl -X POST http://127.0.0.1:8080/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review the newest open PR and summarise the risks", "thread_name": "PR review"}'

# 告诉主频道发生了某件事
curl -X POST http://127.0.0.1:8080/api/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "CI", "message": "Nightly build is green"}'
```

## 把 c-lord 当作库使用

c-lord 是一个普通的 Python 包，`c-lord start` 只是建立在它之上的一个程序。要把它放进你自己的 bot——旁边再加上你自己的 cog——安装它并调用 `setup_bridge()`：

```bash
uv add git+https://github.com/yousan/c-lord.git
```

```python
import asyncio
import os

from c_lord import ClaudeConfig, setup_bridge
from c_lord.bot import ClaudeDiscordBot


async def main() -> None:
    channel_id = int(os.environ["DISCORD_CHANNEL_ID"])
    owner_id = int(os.environ["DISCORD_OWNER_ID"])

    bot = ClaudeDiscordBot(channel_id=channel_id, owner_id=owner_id)
    config = ClaudeConfig(model="sonnet", permission_mode="acceptEdits")

    async with bot:
        await setup_bridge(
            bot,
            config,
            claude_channel_id=channel_id,
            allowed_user_ids={owner_id},
        )
        # await bot.add_cog(YourCog(bot))   ← 你自己的 cog 放在这里
        await bot.start(os.environ["DISCORD_BOT_TOKEN"])


asyncio.run(main())
```

`setup_bridge()` 会注册 c-lord 的标准 cog——按上面的调用共九个：聊天、频道与仓库的绑定、技能、工作区命令、30 天整理、调度器、会话记录镜像、登录中继和 `/version`。这是有意为之：以后加入 c-lord 的标准功能，只要你更新包就会到达你的 bot，不需要改动你的代码。`WebhookTriggerCog` 和 `AutoUpgradeCog` 不在其中；这两个请自己用 `bot.add_cog()` 添加，如上文所示。

```bash
uv lock --upgrade-package c-lord && uv sync
```

请使用 `ClaudeDiscordBot` 作为 bot 类——c-lord 的 cog 会读取它携带的信息，例如主频道。其余部分可以参考独立运行的入口 [`c_lord/main.py`](../../c_lord/main.py)：读取 `.env`、单实例锁、启动 REST API，以及收到信号时关闭。

## 架构

### Discord 与 tmux 的对应关系

“Discord 子区 = Claude Code 会话”这个思路建立在一套 tmux 布局之上。知道一个子区实际位于哪里，调试和集成都会容易得多（你可以 `tmux attach` 实时观察一个会话）：

| 单位 | tmux | 对应关系 |
|------|--------------|---------|
| 子区所绑定的仓库 | tmux 会话 | 1:1 |
| 子区 | tmux 窗口 | 1:1 |

- **会话只会在频道里诞生。** `/clord` 会开一个*新*子区；在子区内使用时，它会延续该子区的会话，或者对记录已被清理的子区提出重新连接，或者在该子区从来不属于 c-lord 时拒绝（#551）。普通的聊天子区无法变成 Claude 会话——按照设计，就没有接管这类子区的命令。见 [docs/COMMANDS.md](../COMMANDS.md#chat--sessions)。
- **1 个仓库 = 1 个 tmux 会话——而不是 1 个频道。** 会话名称来自子区所绑定的仓库：子区有自己的绑定（`/clord-thread-init`，或 `/clord repo:<url>`）时用它自己的，否则用频道的（`/clord-init`）；未绑定的频道回退到默认的 `clord` 会话。因此，如果一个频道里的子区绑定到不同的仓库，它们就会分布在多个会话中；而绑定到同一仓库的两个频道则共用一个会话。某个子区究竟位于哪里——以及为什么这样决定——在 [docs/specs/tmux-layout.md](../specs/tmux-layout.md) 中有详细说明。
- **1 个子区 = 1 个 tmux 窗口 = 1 个 Claude Code 会话。** 每个子区都有自己的窗口（`w1`、`w2`……），其中运行一个 Claude Code 会话。所以你在子区里的一来一往，*就是*与那个 Claude 会话的一来一往——回复通过 `--resume` 延续它。窗口编号在**一个会话内**唯一，而不是在一个频道内唯一。

关于这一设计背后的“为什么”，见 [docs/PHILOSOPHY.md](../PHILOSOPHY.md)。

### 回答如何到达 Discord

```text
你，在 Discord 子区里                         Claude Code，在该子区的 tmux 窗口里
        │                                                    │
        │  你的消息被键入窗格  ─────────────────────────────▶│
        │                                                    │  工作：读取、编辑、运行工具
        │                                                    ▼
        │                          ~/.claude/projects/<dir>/<session>.jsonl
        │                          （Claude Code 自己写下的会话记录）
        │                                                    │
        ◀──── c-lord 跟读该文件并发布其中的内容 ─────────────┘
```

c-lord 不从终端画面上读取 Claude 的回答，也不要求 Claude 自己去发布。它读取 Claude Code 本来就在保存的会话记录，并转发其中的内容。抓取屏幕的做法总是把终端 UI 的碎片漏进 Discord；让 Claude 自己发布，则意味着它一旦忘记，整个回合就不见了。会话记录没有这两个问题，它也是回答到达 Discord 的唯一途径。

窗格仍然受到监视，但不是为了读取文本：而是为了察觉回合已经结束，把 Claude Code 显示的菜单（`AskUserQuestion`、计划批准）变成 Discord 按钮，接受 Claude Code 的权限提示，以及报告 c-lord 不认识的提示。[docs/ARCHITECTURE.md](../ARCHITECTURE.md) 完整讲解了这一切，[docs/DESIGN_DECISIONS.md](../DESIGN_DECISIONS.md) 记录了原因。

### 各部分的位置

```text
c_lord/
  cli.py            `c-lord` 命令：setup、start、version
  main.py           独立运行的入口——读取 .env、获取锁、启动一切
  setup.py          setup_bridge()：一次调用注册标准 cog
  bot.py            ClaudeDiscordBot
  tmux.py           tmux 会话和窗口；启动 claude、向窗格键入
  session_dir.py    每个子区一个 git 克隆
  claude/           基于 tmux 窗格的逐回合 runner、CLI 设置、上下文用量
  transcript/       跟读 Claude Code 的会话记录并整理成适合 Discord 的形式；子代理日志
  cogs/             聊天、频道↔仓库绑定、技能、工作区命令、调度器、
                    Webhook 触发器、自动升级、会话记录镜像
  discord_ui/       状态灯、提问按钮、回合进度行、表格和窗格图片
  database/         SQLite：会话、绑定、定时任务、通知、待处理的菜单
  ext/api_server.py REST API
  skills/           放入每个会话的 discord-read 技能
deploy/             systemd unit
scripts/            install-systemd.sh、staging.sh、证据与发布工具
docs/               指南，以及 docs/specs/——每项功能应有的行为
```

### c-lord 是什么，不是什么

- **是桥，不是 AI。** c-lord 没有自己的提示词、工具或记忆。这些都属于 Claude Code，你为 Claude Code 配置的一切——`CLAUDE.md`、技能、MCP 服务器、hook——在子区里都能用，因为运行的是同一个程序。
- **是真正的 CLI，不是 API。** 每个子区运行的都是你会在终端里运行的那个交互式 `claude`。c-lord 从不调用 Anthropic API。
- **tmux 保留。** c-lord 不取代 tmux；它让你从 Discord 管理 tmux。窗格始终在那里，随时可以 attach。
- **会话比连接活得更久。** Claude 运行在服务器上。笔记本休眠、火车进隧道或者 bot 重启，都不会让它停下。
- **你从 Discord 做判断，所以 Discord 必须展示足够的信息供你判断。** 进度、结果、文件和失败都会出现在子区里，失败时会说明什么失败了、接下来该做什么，而不是默不作声。
- **常见用法无需任何配置。** 新功能默认开启并带有合理的设置，使用方只需要更新包。

这些背后的考虑写在 [docs/PHILOSOPHY.md](../PHILOSOPHY.md)（日语）中。

## 安全

和 c-lord 对话就意味着在它所在的机器上执行命令。默认设置以此为前提。

- **用户授权**——`allowed_user_ids` / `CLORD_ALLOWED_ROLE` 限制谁可以调用 Claude。**两者都未配置时，只有 Discord 应用自己的所有者可以使用 bot**——和 c-lord 对话会在其主机上执行 shell 命令，所以“未配置”不等于“所有人”（#713）。`CLORD_ALLOW_ANYONE=1` 是有意向整个服务器开放（并会在启动时说明）。见 [docs/specs/authorization-default.md](../specs/authorization-default.md)
- **REST API 只响应 bot 自己的 Unix 用户**，除非你设置了 `CLORD_API_SECRET`（见 [REST API](#rest-api)）。
- **中间不经过 shell。** c-lord 启动的每个进程都以参数列表的方式启动（`asyncio.create_subprocess_exec`），从不通过 shell；提示词放在 `--` 之后传递，因此不会被当作选项解析。
- **使用前先校验。** 会话 ID 必须符合应有的格式才会传给 `--resume`；技能名称和仓库 URL 也以同样的方式校验。
- **bot token 不会通过继承泄漏到会话中。** 它会从 tmux 环境以及 `claude` 命令行的环境中移除。这防的是意外，而不是铁了心要拿到它的会话：`.env` 是同一个 Unix 用户可以读取的文件。

[docs/SECURITY.md](../SECURITY.md) 中有威胁模型以及有意接受的风险。

## 开发

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup                                # 每个克隆执行一次：启用 pre-commit hook

uv run pytest tests/ -v --cov=c_lord
uv run ruff check c_lord/ && uv run ruff format --check c_lord/
uv run pyright c_lord/
```

CI 在 Python 3.10、3.11 和 3.12 上运行这些检查。它们使用 mock，所以 CI 通过并不能说明改动在真实的 Discord 和真实的 tmux 上可用——因此改动在合并之前还必须在 staging bot 上演示它确实可用。[CONTRIBUTING.md](../../CONTRIBUTING.md) 说明了 pull request 需要具备什么；[docs/STAGING.md](../STAGING.md) 说明了 staging bot。

## 这个项目是如何构建的

**整个代码库由 [Claude Code](https://docs.anthropic.com/en/docs/claude-code)** 编写——Anthropic 的 AI 编程代理。人类作者（[@ebibibi](https://github.com/ebibibi)）以自然语言提供需求和方向，但未手动阅读或编辑源代码。

上面这段话描述的是上游项目 claude-code-discord-bridge，它依据 MIT 许可证为 c-lord 提供了最初的架构。这个衍生项目现在由 [@yousan](https://github.com/yousan) 维护，他负责定义需求、审查 pull request 并批准所有改动，实现工作则由 Claude Code 完成。

日常的流程是这样的。一项改动从带有验收标准的 Issue 开始。Claude Code——通常运行在 c-lord 的子区里，也就是说 c-lord 是用 c-lord 构建的——先写一个失败的测试，让它通过，在 staging bot 上用截图演示改动确实可用，然后开一个 pull request。`main` 由两项必需的检查把关，只有当 [Definition of Done](../../CLAUDE.md#definition-of-done-dod--single-source-of-truth) 的每一项都满足时，pull request 才会合并。

c-lord 自己的历史始于 2026-02-22，起点是该上游项目的一份副本。欢迎提交 bug 报告和 pull request，无论来自人还是代理。

## 实际案例

**[EbiBot](https://github.com/ebibibi/discord-bot)** — 基于此框架构建的个人 Discord Bot。包括自动文档同步（英文 + 日文）、推送通知、Todoist 看门狗、定期健康检查和 GitHub Actions CI/CD。可作为构建自己 bot 的参考。

EbiBot 是 ebibibi 自己的 bot，它建立在上游的 claude-code-discord-bridge 之上，而不是 c-lord 之上。

## 灵感来源

- [@ebibibi](https://github.com/ebibibi) 的 [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge)——本项目所基于的上游基础。
- [OpenClaw](https://github.com/openclaw/openclaw) — 表情状态反应、消息防抖、感知代码块分割
- [claude-code-discord-bot](https://github.com/timoconnellaus/claude-code-discord-bot) — CLI 调用 + stream-json 方案
- [claude-code-discord](https://github.com/zebbern/claude-code-discord) — 权限控制模式
- [claude-sandbox-bot](https://github.com/RhysSullivan/claude-sandbox-bot) — 每对话线程模型

## 许可证

[MIT](../../LICENSE)
