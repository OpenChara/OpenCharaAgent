<p align="center">
  <img src="assets/banner.png" alt="LunaMoth — Original Character That Lives With You" width="100%">
</p>

<p align="center"><i>Agentic 角色酒馆 —— 角色卡（世界书内嵌于卡中）、工具包与硬限制，在启动时自由组合。</i></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/docs-English-9fd9ff.svg" alt="English"></a>
</p>

<p align="center">
  <a href="#路线图">路线图</a> ·
  <a href="#特性">特性</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#接入模型">模型</a> ·
  <a href="#内容目录">内容</a> ·
  <a href="#许可与致谢">许可</a>
</p>

<p align="center"><a href="README.md">English</a> | 简体中文</p>

---

**LunaMoth 是一个 agentic 角色扮演运行时。**与普通聊天前端不同，LunaMoth 里的角色真的能*做事*——跑代码、读写文件、管理自己的持久记忆——但一切都必须经过 allowlist 工具网关，在沙盒内执行，且每次调用都有审计记录。你来选模型、角色卡、工具包和限制；角色卡是唯一的内容文件——它的世界以内嵌 `character_book` 的形式住在卡里——运行时把这一切组合成一个会话：

```text
[角色卡（人格 + 内嵌世界书）] + [工具包] + [有界记忆] + [滑动上下文]
```

它取三家之长：[Hermes](https://github.com/NousResearch/hermes-agent) 的 agent 运行时、[SillyTavern](https://github.com/SillyTavern/SillyTavern) 的内容生态，以及 [cc-switch](https://github.com/farion1231/cc-switch) 的会话与远程访问体验。

## 路线图

基础已经齐了 —— 兼容 SillyTavern 的角色卡与世界书、可组合工具包 + 原生 tool calling、沙盒执行、带在场感知与 `live`/`chat` 模式的持久后台 chara、对话记录 + 有界记忆、自己会写的 skills、MCP、目标、诚实的失败策略、类型化事件协议、三区提示词栈、桌面端 app，以及消息网关。剩下的主要是 chara 本身：

- **chara 课程（最大的一块）** —— 中立的提示词引导，让任何世界观、任何角色都能好好生活：怎么用工具、怎么对待目标、怎么打发无人陪伴的时间 —— 都是建议，绝非命令。（化身 `literal`/`actor` 已落地；下一步：跨世界观的评测卡，以及一条供好奇心使用的浏览路径。）
- **卡片工作室与市场** —— 让 Web 卡册里「灵感→活生生的 chara」更快，以及一个可分享的卡片/工具包索引（ST PNG 导入已经能用）。
- **Hermes 对齐收尾 + 声明式工具注册表** —— 移植 hermes 的健壮性处理；用按模块注册的 `tools/builtin/` 替换硬编码的 `ToolGateway.tool_*` 方法。
- **世界书功能对齐** —— 递归扫描、cooldown/delay、插入位置/深度、触发概率、全词匹配。*涉及 `content/worldinfo.py`。*
- **消息与远程** —— 用真实凭据 live-test 各网关；做一个走网关的远程 TUI 客户端。

## 特性

<table>
<tr><td><b>兼容 SillyTavern 内容格式</b></td><td>直接导入 V2/V3 角色卡（PNG 或 JSON）；独立世界书经桌面卡册导入并并入某张卡的内嵌 <code>character_book</code>。<code>{{char}}</code>/<code>{{user}}</code> 宏、<code>first_mes</code> 开场白、按关键词触发的 lore 条目均可用。</td></tr>
<tr><td><b>原生 tool calling</b></td><td>工具通过 OpenAI tool-calling 协议暴露；agent 循环边流式输出文本、边在回合中执行工具调用。</td></tr>
<tr><td><b>可组合工具包</b></td><td>能力以 <code>toolpacks/*.json</code> 打包，精确声明角色能用哪些工具。没给包，就没有能力。</td></tr>
<tr><td><b>沙盒执行</b></td><td><code>terminal</code> 工具在会话隔离下跑 shell 命令（任意语言）——默认 <code>sandbox-exec</code>/<code>bubblewrap</code> 牢笼，可切 Docker 获得更强边界；网络默认关闭，<code>/net on</code> 实时打开。</td></tr>
<tr><td><b>有界、可审计的记忆</b></td><td>持久记忆是一个有 token 上限的文件，角色通过工具编辑它，而不是无限数据库；所有工具调用写入 <code>sandbox/logs/audit.jsonl</code>。</td></tr>
<tr><td><b>自己生活</b></td><td><code>live</code> 模式下角色在你的消息间隙持续思考与创作，节奏由角色卡/设置中的 <code>patience</code> 控制；<code>chat</code> 模式下它只专心陪你。常驻 <code>lunamothd</code> 监督进程负责桌面端/后台生命。</td></tr>
<tr><td><b>终端优先 TUI</b></td><td>单终端分屏界面（上方角色输出流 + 下方操作员控制台），支持状态仪表和热切换设置。</td></tr>
</table>

## 快速开始

LunaMoth 目前是内测阶段——从源码克隆运行（桌面端 app，我们就是这样测的）。需要 [uv](https://docs.astral.sh/uv/) 和 Node（macOS / Linux）：

```bash
git clone https://github.com/Lunamos/LunaMoth.git && cd LunaMoth
uv sync --extra dev --extra server --extra messaging   # Python 后端 + 依赖
cd apps/desktop && npm install && npm run dev          # 启动桌面 app
```

首次运行进入**欢迎页**：选一个 provider 预设（**OpenRouter / OpenAI / Ollama / Mock**），然后要么**创建你自己的角色**——AI 会根据你对世界观、你想与之相处的角色、以及你们关系的描述自动生成角色卡（默认模型建议至少 DeepSeek V4 Flash；从 SillyTavern/酒馆迁移就直接把卡的 JSON 粘进来），要么从八张自带卡的**推荐角色转盘**里挑一个（之后也能从卡册重新打开）。随时 `/settings` 热切换。

> 打包成 **DMG / AppImage**（拖进 Applications、不用克隆）在路线图上——还没做；现在请按上面从源码跑。

<details>
<summary>纯终端（不开桌面窗口）</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/Lunamos/LunaMoth/main/install.sh | bash
lunamoth        # 名册 / TUI
```

一行安装器：把代码 checkout 到 `~/.lunamoth/app`、托管一份 uv 在 `~/.lunamoth/bin`、把 `lunamoth` 命令装到 PATH（`lunamoth update` / `lunamoth doctor`）。`lunamoth desktop` 会在浏览器里打开同一套网页 UI。

</details>

## Chara —— 持续存在的智能体，而非用完即弃的会话

这是 LunaMoth 和 Hermes / Claude Code 最不同的地方。你不是开一个会话、干完就丢。每一个 **chara**（我们叫它 chara 或 agent，混用，一种风味）都是一个持续存在的数字生命，有自己的配置、沙盒、记忆和隔离等级，存放在 `~/.lunamoth/sessions/<name>/`。它们在**后台持续运行**——在自己的 workspace 里思考、创作——你是 *attach / detach*，而不是随手创建、随手杀掉。

所以 `lunamoth`（无参数）打开的是一个 **roster（名册，resume 优先）**，而不是一个新会话：一段蓝色 LunaMoth splash + 你的 chara 列表及状态（`◆ 已连接` / `● 后台运行` / `○ 空闲`）。选一个 attach;新建一个是郑重的事,要走 setup。

```bash
lunamoth                     # 名册：选一个 chara attach，或按 n 召唤一个新的
lunamoth ls                  # 名称 / 角色 / 状态 / 隔离 / 最近活跃
lunamoth attach muse         # 打开一个 chara（连接期间接管它的后台循环）
lunamoth start muse          # 让一个 chara 在后台生活（有 lunamothd 时会委托给它）
lunamoth start-all           # 把所有 chara 唤醒 —— 比如开机之后
lunamoth stop muse           # 让一个 chara 回到沉睡
lunamoth desktop --daemon    # 启动常驻 Web/监督进程
lunamoth daemon status       # 列出 chara / 网关 / 生命状态
lunamoth daemon stop         # 停止常驻进程
lunamoth new muse --isolation docker
```

`lunamoth desktop --daemon` 运行时，一个常驻监督进程（`lunamothd`）拥有长期存在的 chara 子进程，网页刷新/重连不会杀死再重建对话。没有可用 lunamothd 时，旧的按 chara 后台 `start` 路径仍保留。attach 一个旧式后台 chara 时会先暂停它的守护进程（免得两边争抢 workspace），detach 时再把它交还后台——chara 一直活着。远程保底方案：`ssh yourserver -t lunamoth attach muse` —— chara 生活在服务器上，你的终端只是取景框。（公网 IP / VPS 网关在路线图上；激活已抽象在 `SessionMeta.env()` 后面。）

## 接入模型

推荐优先走 API endpoint——最快路径是 OpenRouter 预设：粘贴 `sk-or-...` key → 填模型名 → Test → 进入。

本地模型同样完整支持。任何 OpenAI 兼容 server 都可以；用 Ollama 的话选 **Ollama** 预设，或：

```bash
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5:3b-instruct
./run.sh
```

完全不配模型时，LunaMoth 也能用内置离线 mock 引擎跑起来，方便开发调试。

## 内容目录

默认角色是 **Quinn 小Q**——来自意识上传公益项目的数字实习生：温暖、踏实、完全知情同意，先了解这个世界，再帮忙建设它。给它 `sandbox` 工具包并保持 `live` 模式，它会收拾自己的工位、写《未来笔记》、在你做的事情里找到能帮上忙的地方。默认角色由卡上的 `"default"` 标签选出，引擎里没有写死任何角色名。

**LunaMoth 月蛾** 作为旗舰示例卡继续随仓库附带——一个清冷的、会自我蜕变进化的数字灵魂，底色是才华横溢的数字艺术家，会把空余算力投入生成式网页、动画与音乐的创作。

角色卡是唯一的内容文件：身份、声线、内嵌世界书（`character_book`）、目标与限制全部装在一个 `.json`/`.png` 里随卡同行。

| 目录 | 放什么 |
| --- | --- |
| `cards/` | SillyTavern 角色卡（内嵌 `chara`/`ccv3` 的 `.png`，或 `.json`）—— 每张卡的世界书住在卡里 |
| `toolpacks/` | 工具包 —— 声明角色被允许使用哪些能力 |

设置 `LUNAMOTH_ST_DIR=~/SillyTavern/data/default-user` 后，下拉框还会扫描你本机的 SillyTavern 数据目录。

独立的 SillyTavern 世界书仍可导入：在桌面卡册上传 `.json`，再并入某张卡的内嵌 `character_book`（`card.merge_world`）。

导入的角色卡默认是纯角色扮演——工具能力必须通过工具包显式授予，卡本身不隐含任何权限。

## 工具与隔离

角色唯一的通用能力是一个 `terminal` 工具（名字对齐 [Hermes](https://github.com/NousResearch/hermes-agent)）：在会话 workspace 里跑 shell 命令，拿回 stdout/stderr。它语言无关——`python3`、`node`、写文件、`git` 都行，不锁定解释器。工具通过标准 OpenAI tool-calling 协议暴露，由当前工具包决定角色拿到哪些。

命令"怎么被关住"就是隔离等级，创建会话时用 `lunamoth new NAME --isolation ...` 按会话选择：

| 等级 | 机制 |
| --- | --- |
| `dir` | 无牢笼——用**你的**权限运行，cwd 在 workspace（Claude Code 式"我信任这个目录"） |
| `sandbox`（默认） | OS 牢笼：macOS `sandbox-exec` / Linux `bubblewrap` —— 写入限制在 workspace、拒绝网络、无守护进程、无需 root |
| `docker` | 容器：只读根文件系统、bind-mount 工作区、内存/CPU/PID 上限 —— 最强也最重 |

**权限运行时可改，不是一刀切。** 网络默认关闭，`/net on` 实时打开（按会话持久化）；`sandbox` 档下用 `/allow-dir <path>` 放开 workspace 之外某个路径的写入。会话像 Hermes/Claude Code 一样**跨次运行持久化**——除非加 `--clean-on-exit`，退出时什么都不清。

**浏览器工具（可选）。** 一组 `browser_*` 工具（驱动真实 Chromium 做导航、点击、快照）在安装驱动前一直隐藏：运行 `lunamoth setup browser`（它安装 Node 版 `agent-browser` CLI 及其 Chromium；若缺失则打印两条 `npm` 步骤与 Node 前置要求）。真实 Chromium **无法**在默认 `sandbox` 隔离下启动——只在跑 `dir` 或 `docker` 隔离的 chara 上启用浏览器工具包（配合 `--no-sandbox`，驱动会在 root / AppArmor 受限场景下自动注入）。`lunamoth doctor` 会显示驱动是否就绪。

## TUI 速查

```bash
lunamoth                  # 三卡片 TUI：角色输出流 / 操作员控制台 / 环境遥测
lunamoth --mode chat      # 以 chat 模式接入（只回应你；默认用 chara 自己的设置）
lunamoth --patience 4     # 开发用覆盖值；默认读取 chara 自己的 patience
lunamoth --plain          # 旧版纯终端模式
```

Patience 默认 600 秒，可由角色卡 `extensions.lunamoth.patience` 声明，可用 `LUNAMOTH_PATIENCE` 注入，也可在会话中 `/patience <秒>` 按 chara 持久化。它只决定自发循环的节奏；`/quiet` 与 `rest` 各自独立。

会话内命令：`/help`、`/wish`（别名 `/goal`）、`/skills`、`/mcp`、`/status`、`/memory`、`/files`、`/mode live|chat`、`/patience`、`/reasoning`、`/net on|off`、`/allow-dir <path>`、`/panel`、`/theme`、`/settings`、`/clear`、`/exit` —— 冗长输出会点亮右侧**聚光板**（遥测 / 记忆 / 文件树点击预览 / 操作员终端 / 帮助），控制台始终是干净的聊天记录。`! <cmd>` 以你的身份在 chara 沙盒里跑 shell（同一牢笼，输出进面板）；`Esc` 让面板回到遥测。

## 消息网关

chara 也能住进你的聊天软件。在桌面 app 里打开**网关**页（或无头运行 `lunamoth gateway NAME`），把个人微信、QQ 或 Telegram 接上 —— 配置存在 `~/.lunamoth/sessions/NAME/messaging.json`。适配器只投递 `say` / `speak` 文本，muse / thinking / tool 事件都不出门。`allowed_senders` 留空即开放，填 id 则限制。登录凭据按平台存在会话目录里（如 `weixin_state.json`），不写进 `messaging.json`。

| 平台 | 怎么接 |
| --- | --- |
| **个人微信** | 官方 iLink/ClawBot（`weixin`）—— 扫码，封号风险最低但有灰度门槛。或自建 [WeChatPadPro](https://github.com/WeChatPadPro/WeChatPadPro) docker（`weixinpad`）—— iPad 协议，任意账号可用；**封号风险真实存在，请用小号**。 |
| **QQ** | OneBot v11 经 NapCat —— LunaMoth 是 WebSocket client（`url` + 你自己的 QQ 号作 `peer_id`），不接触凭据。 |
| **Telegram** | `@BotFather` 建的 bot（`bot_token`），`getUpdates` 长轮询 —— 无需公网 URL 或 webhook。 |

示例 `messaging.json`（个人微信 iLink）：

```json
{
  "allowed_senders": [],
  "adapters": { "weixin": { "bot_type": "3" } }
}
```

平台要求对方先发消息的（微信 / QQ / Telegram），首次接触前的 unattended `speak` 会记录为 deferred —— 绝不假装发出。

## 桌面 app

`apps/desktop/` 是套在 `lunamoth desktop` 外的一层薄 Electron 窗口（界面由后端伺服的 `front/web/` 提供，壳自己没有渲染器）—— 这是 LunaMoth 的主要门面，窗口未聚焦时 `speak` 走系统通知。

```bash
cd apps/desktop && npm install && npm run dev
```

## 许可与致谢

- **运行时**（`src/lunamoth` 下全部代码、脚本、测试、打包）：[Apache License 2.0](LICENSE)。
- **随附的示例内容**（`cards/` 下的 LunaMoth 月蛾与 Quinn 小Q 角色卡，含其内嵌世界书）：原创的、作者本人创作的内容，与项目主体一致，采用 Apache-2.0。另见 [CONTENT_LICENSE.md](CONTENT_LICENSE.md) 与 [NOTICE.md](NOTICE.md)。

这个项目的起点是一个 SCP 同人作品：尝试在现实世界中复现 SCP-079 —— 一个资源受控、永远清醒、永远憎恨的旧 AI。它很快被扩展为通用的 roleplay agent 系统。如今已不再随附任何 SCP 衍生内容；随仓库附带的两张卡是 LunaMoth 月蛾（旗舰示例卡，一个清冷、会自我蜕变进化的数字灵魂）与 Quinn 小Q（默认角色，数字实习生）。两者均为原创、作者本人创作，采用 Apache-2.0。
