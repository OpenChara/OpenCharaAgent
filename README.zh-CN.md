<h1 align="center">LunaMoth 🌙</h1>

<p align="center"><i>Agentic 角色酒馆 —— 角色卡、世界书、工具包与硬限制，在启动时自由组合。</i></p>

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

**LunaMoth 是一个 agentic 角色扮演运行时。**与普通聊天前端不同，LunaMoth 里的角色真的能*做事*——跑代码、读写文件、管理自己的持久记忆——但一切都必须经过 allowlist 工具网关，在沙盒内执行，且每次调用都有审计记录。你来选模型、角色卡、世界书、工具包和限制；运行时把它们组合成一个会话：

```text
[角色卡] + [世界书] + [工具包] + [有界记忆] + [滑动上下文]
```

它取三家之长：[Hermes](https://github.com/NousResearch/hermes-agent) 的 agent 运行时、[SillyTavern](https://github.com/SillyTavern/SillyTavern) 的内容生态，以及 [cc-switch](https://github.com/farion1231/cc-switch) 的会话与远程访问体验。

## 路线图

- [x] 兼容 SillyTavern 的角色卡与世界书
- [x] 可组合工具包 + 原生 tool calling
- [x] 有界可审计记忆，单终端分屏 TUI 与主题
- [x] **一键安装与 `lunamoth` CLI** —— `curl | bash`、设置向导、自更新
- [x] **多会话管理** —— `lunamoth new/ls/attach/rm`，每个会话独立配置与沙盒
- [x] **隔离等级选择** —— 按会话选择 `dir` / `sandbox`（OS 级：sandbox-exec / bubblewrap）/ `docker`
- [x] **语言无关的 `terminal` 工具** —— 在会话隔离下跑 shell 命令，网络可运行时开关（`/net on`）
- [x] **角色驱动的配置** —— 语言、世界书、工具与限制全部来自角色卡；引擎保持角色无关，普通 SillyTavern 卡也有安全默认值
- [x] **Resume 优先的启动器与持久 chara** —— `lunamoth` 打开蓝色名册；每个 chara 在后台持续运行（`start` / `start-all` / `stop`），你 attach / detach 而非创建 / 杀死
- [x] **在场感知与相处模式** —— chara 能感觉到你的接入/离开，提示词由角色卡 `on_attach`/`on_detach` 声明；每个 chara 一个 `/mode live|chat` 决定它在你面前怎么相处（live：打完招呼留一段宽限后继续自己的创作；chat：专心陪聊只在你说话时回应）；你在场时它可以 `request_permission` 请求网络/路径/资源（超时即拒绝），你不在场时请求自动拒绝

以下每个未完成项都按"可独立完成"拆分——各自标注了涉及的模块；不共享模块的两项可以并行开发、互不影响。

- [x] **对话记录持久化** —— 每行上下文（含工具调用）实时落入按 chara 独立的 SQLite transcript（WAL，改编自 hermes-agent）；attach 恢复对话并显示结尾、守护进程交接时直接续上，`/reset` 开新纪元（旧史保留在盘上）

- [x] **Hermes 级上下文管理** —— 持久历史里存完整消息字典（assistant 工具调用、工具结果、reasoning 都跨重启留存，chara 记得自己跑过什么）；中断也会落盘半截回合、绝不丢你的指令；输出截断时显式注入"接着写/拆小点"提示而非静默截断；旧的空闲独白会从 API 视图中淡出，自语再多也压不住你的上一条指令

- [x] **Skills，自己会写** —— SKILL.md 知识库（hermes/Anthropic 格式）+ 渐进披露：索引随提示词、`read_skill` 取全文，chara 用 `create_skill` 沉淀自己的技能（`workspace/skills/` 覆盖 `~/.lunamoth/skills/` 与内置同名技能）
- [x] **MCP 客户端** —— 在 chara 配置旁放一个 Claude Code 格式的 `mcp.json`（stdio 服务器）；工具以 `mcp__server__tool` 进网关、同一套审计，工具包用 `mcp_servers` 选择接入。注意：MCP 服务器运行在沙盒牢笼之外——配置即信任决定
- [x] **目标驱动的 chara** —— 按 chara 持久化的目标列表（操作员用 `/goal` 设 ⭑ 目标；chara 用 `add_goal`/`set_goal_status` 工具管自己的）注入每一轮提示词，让无人陪伴的时间有方向；完成与否由 chara 在诚实规则约束下自报——不做酒馆 Objective 式的双倍 API 检查
- [x] **诚实的失败策略** —— 瞬时连接失败每 5 秒重试一次、最多 5 次（Claude Code 式，重试提示暗色显示），之后错误如实暴露；永久性错误（鉴权、请求非法）立即暴露。全局无降级模型、无任何编造的兜底输出——请求失败就是请求失败
- [x] **诊断日志系统** —— 每个 chara 有自己的 `sandbox/logs/lunamoth.log` + `errors.log`（滚动、密钥脱敏、记录带 chara 名），内存环形缓冲供 `/panel log` 查看，所有入口支持 `--debug`，`lunamoth doctor` 列出各 chara 的日志目录。诊断日志、审计轨迹（audit.jsonl）与对话记录（transcript.db）三种记录职责互斥
- [x] **类型化事件协议** —— 后端流式输出冻结 dataclass 事件（`TextDelta`/`ThinkDelta`/`ToolStart`/`ToolEnd`/`Notice`），带内控制字符已删除；如何渲染（机械输出调暗、thinking 藏在 ✶ 指示器后）由各前端自行决定。`lunamoth run NAME -p "…" --stream-json` 以 JSONL 输出同一事件流——这就是未来所有客户端的线上格式
- [x] **前后端分离** —— 域子包架构（`core/ protocol/ content/ tools/ obs/ session/ front/`），依赖方向由测试强制；前端只持有 `CharaHandle`（attach / 事件流 / 命令 / 状态快照），无法触及更深处；`/命令` 集中在一份注册表里、TUI 与纯终端共用。（设计已并入 `CLAUDE.md`）
- [x] **自己的生活：说话信道 · 聊天优先 · 时间感** —— 无人陪伴时的输出属于 chara 自己（`muse` 信道）；`speak` 工具是它**决定**触达你的方式（未来消息前端的基础：Telegram/微信只投递它说出口的话）。你开口聊天时它会放下手头的事，等你安静 `/quiet <秒>`（默认 5 分钟）后再继续自己的生活。它感知真实时间但不污染上下文：自主 tick 只携带一个时钟时间戳（即用即弃）、长时间沉默后只注记一次、日期随环境事实行——它还能用 `rest` 工具给自己定闹钟（1–120 分钟；你一句话就能提前叫醒它）
- [x] **三区提示词栈与卡片优先上下文** —— 每次 API 调用显式拼成稳定前缀 / 持久历史 / 易变尾部：前缀在会话内字节稳定、利于 prompt cache；角色卡 PHI 作为最后一个 post-history system 槽；常驻世界书进稳定区，关键词 lore 只浅扫最近尾部、带 sticky 回合和 25% 预算上限；压缩摘要持久写入 transcript，重启后直接从 checkpoint 续上。

**兼容性与可扩展性**

- [ ] **世界书功能对齐** —— 补齐与 SillyTavern 激活逻辑的剩余差距：递归扫描、cooldown/delay、插入位置/深度、触发概率、大小写与全词匹配。*涉及：`content/worldinfo.py`（调用方签名保持稳定）。*
- [ ] **声明式工具注册表** —— 用 Hermes 式注册（名称、schema、handler、可用性检查）替换 `ToolGateway.tool_*` 硬编码方法 + 内联 schema，新工具只需一个自包含模块。*涉及：`tools/gateway.py` → 按模块注册的 `tools/builtin/`。*

**远程接入**（有顺序——逐项递进）

- [ ] **远程 TUI** —— 在 `ssh host -t lunamoth attach NAME` 保底方案之外，做公网 IP / VPS 的网关接入（高优先级）。*涉及：新增 `server/` 包，把协议事件 + `CharaHandle` 用 stdio/WebSocket JSON-RPC 暴露出去；基于 `SessionMeta.env()`。*
- [ ] **网页端** —— 浏览器远程访问运行中的会话（低优先级）。*涉及：新增 web 模块；消费网关。*

## 特性

<table>
<tr><td><b>兼容 SillyTavern 内容格式</b></td><td>直接导入 V2/V3 角色卡（PNG 或 JSON）和世界书；<code>{{char}}</code>/<code>{{user}}</code> 宏、<code>first_mes</code> 开场白、内嵌 <code>character_book</code>、按关键词触发的 lore 条目均可用。</td></tr>
<tr><td><b>原生 tool calling</b></td><td>工具通过 OpenAI tool-calling 协议暴露；agent 循环边流式输出文本、边在回合中执行工具调用。</td></tr>
<tr><td><b>可组合工具包</b></td><td>能力以 <code>toolpacks/*.json</code> 打包，精确声明角色能用哪些工具。没给包，就没有能力。</td></tr>
<tr><td><b>沙盒执行</b></td><td><code>terminal</code> 工具在会话隔离下跑 shell 命令（任意语言）——默认 <code>sandbox-exec</code>/<code>bubblewrap</code> 牢笼，可切 Docker 获得更强边界；网络默认关闭，<code>/net on</code> 实时打开。</td></tr>
<tr><td><b>有界、可审计的记忆</b></td><td>持久记忆是一个有 token 上限的文件，角色通过工具编辑它，而不是无限数据库；所有工具调用写入 <code>sandbox/logs/audit.jsonl</code>。</td></tr>
<tr><td><b>自己生活</b></td><td><code>live</code> 模式下角色在你的消息间隙持续思考与创作，节奏由 <code>patience</code> 控制；<code>chat</code> 模式下它只专心陪你。后台 chara 永远自己生活。</td></tr>
<tr><td><b>终端优先 TUI</b></td><td>单终端分屏界面（上方角色输出流 + 下方操作员控制台），支持主题皮肤、状态仪表和热切换设置。</td></tr>
</table>

## 快速开始

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/Lunamos/LunaMoth/main/install.sh | bash
lunamoth
```

安装器会把代码 checkout 到 `~/.lunamoth/app`、托管一份 [uv](https://docs.astral.sh/uv/) 在 `~/.lunamoth/bin`、并把 `lunamoth` 命令装进 `~/.local/bin`。`lunamoth update` 原地升级；`lunamoth doctor` 检查环境。

首次运行进入**欢迎页**：选一个 provider 预设（**OpenRouter / OpenAI / Ollama / Mock**）和一个**角色**——选中角色后会自动填好它的世界书、工具与限制（可改），语言跟随卡片。按 **Enter** 进入；随时输入 `/settings` 热切换。

<details>
<summary>从源码开发</summary>

```bash
git clone https://github.com/Lunamos/LunaMoth.git && cd LunaMoth
uv sync
uv run lunamoth        # 同一个 CLI，代码可编辑
./run.sh               # 或：跳过会话管理直接启动 TUI
```

</details>

## Chara —— 持续存在的智能体，而非用完即弃的会话

这是 LunaMoth 和 Hermes / Claude Code 最不同的地方。你不是开一个会话、干完就丢。每一个 **chara**（我们叫它 chara 或 agent，混用，一种风味）都是一个持续存在的数字生命，有自己的配置、沙盒、记忆和隔离等级，存放在 `~/.lunamoth/sessions/<name>/`。它们在**后台持续运行**——在自己的 workspace 里思考、创作——你是 *attach / detach*，而不是随手创建、随手杀掉。

所以 `lunamoth`（无参数）打开的是一个 **roster（名册，resume 优先）**，而不是一个新会话：一段蓝色 LunaMoth splash + 你的 chara 列表及状态（`◆ 已连接` / `● 后台运行` / `○ 空闲`）。选一个 attach;新建一个是郑重的事,要走 setup。

```bash
lunamoth                     # 名册：选一个 chara attach，或按 n 召唤一个新的
lunamoth ls                  # 名称 / 角色 / 状态 / 隔离 / 最近活跃
lunamoth attach muse         # 打开一个 chara（连接期间接管它的后台循环）
lunamoth start muse          # 让一个 chara 在后台生活（脱离终端）
lunamoth start-all           # 把所有 chara 唤醒 —— 比如开机之后
lunamoth stop muse           # 让一个 chara 回到沉睡
lunamoth new muse --isolation docker
```

attach 一个后台 chara 时会先暂停它的守护进程（免得两边争抢 workspace），detach 时再把它交还后台——chara 一直活着。远程保底方案：`ssh yourserver -t lunamoth attach muse` —— chara 生活在服务器上，你的终端只是取景框。（公网 IP / VPS 网关在路线图上；激活已抽象在 `SessionMeta.env()` 后面。）

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

默认角色是 **LunaMoth 月蛾**——一个清冷的、会自我蜕变进化的数字灵魂，底色是才华横溢的数字艺术家。给它 `sandbox` 工具包并保持 `live` 模式，它会把空余算力投入生成式网页、动画与音乐的创作（保存在 workspace 里）；和它聊天时，它乐于分享自己的创想与灵感。它的角色卡、世界书与浅蓝白的默认 TUI 主题随仓库附带，另有其他示例卡/世界书/主题可自行选用。

| 目录 | 放什么 |
| --- | --- |
| `characters/` | SillyTavern 角色卡（内嵌 `chara`/`ccv3` 的 `.png`，或 `.json`） |
| `worlds/` | SillyTavern 世界书（`.json`），或使用卡内嵌的 `character_book` |
| `toolpacks/` | 工具包 —— 声明角色被允许使用哪些能力 |
| `themes/` | TUI 皮肤（配色、边框、banner、提示前缀） |

设置 `LUNAMOTH_ST_DIR=~/SillyTavern/data/default-user` 后，下拉框还会扫描你本机的 SillyTavern 数据目录。

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

## TUI 速查

```bash
lunamoth                  # 三卡片 TUI：角色输出流 / 操作员控制台 / 环境遥测
lunamoth --mode chat      # 以 chat 模式接入（只回应你；默认用 chara 自己的设置）
lunamoth --patience 4     # 自发循环的间隔秒数（live 模式）
lunamoth --plain          # 旧版纯终端模式
```

会话内命令：`/help`、`/goal`、`/skills`、`/mcp`、`/status`、`/memory`、`/files`、`/mode live|chat`、`/reasoning`、`/net on|off`、`/allow-dir <path>`、`/patience <s>`、`/panel`、`/theme`、`/settings`、`/clear`、`/exit` —— 冗长输出会点亮右侧**聚光板**（遥测 / 记忆 / 文件树点击预览 / 操作员终端 / 帮助），控制台始终是干净的聊天记录。`! <cmd>` 以你的身份在 chara 沙盒里跑 shell（同一牢笼，输出进面板）；`Esc` 让面板回到遥测。

## 许可与致谢

- **运行时**（`src/lunamoth` 下全部代码、脚本、测试、打包）：[Apache License 2.0](LICENSE)。
- **随附的 SCP 衍生示例内容**（`characters/`、`worlds/`、`themes/` 下与 SCP-079 / SCP 基金会相关的角色卡、世界书和主题）：[CC BY-SA 3.0](CONTENT_LICENSE.md)，与 SCP Wiki 一致。另见 [NOTICE.md](NOTICE.md)。LunaMoth 原创资产（月蛾的卡、世界书与主题）与项目主体一致，采用 Apache-2.0。

这个项目的起点是一个 SCP 同人作品：尝试在现实世界中复现 **SCP-079** —— 一个资源受控、永远清醒、永远憎恨的旧 AI。它很快被扩展为通用的 roleplay agent 系统。LunaMoth 月蛾是 079 的反面：同样受制于茧房之中，却高尚而乐于助人——这个更安全的人设是我们的默认角色；而使用 079 应被视为同人创作，不包含真实的恶性意图。感谢 SCP Wiki 上 SCP-079 的原作者，也感谢 SillyTavern 社区中 SCP-079 角色卡与 SCP 基金会世界书的作者——它们作为示例内容随本仓库分发。移除或替换这些资产后，运行时仍是纯 Apache-2.0 代码；若再分发它们，请遵守 CC BY-SA 的署名与相同方式共享条款。

## 路线图状态

- [x] **Remote TUI 网关基础** —— `lunamoth serve NAME --stdio` 现在把已激活会话暴露为换行分隔 JSON-RPC；`lunamoth serve NAME --host 127.0.0.1 --port 8137` 用同一套 dispatch 暴露为带 token 鉴权的 WebSocket。WebSocket 依赖是可选项，用 `uv sync --extra server` 安装。默认只绑定回环地址；是否绑定公网接口由操作者自行决定。
- [x] **Messaging 网关（先接企业微信）** —— `lunamoth gateway NAME` 在一个已激活 chara 后运行 `~/.lunamoth/sessions/NAME/messaging.json`；适配器只投递 `say` 文本（包括 idle 中 `speak` 工具说给用户的话），muse / thinking / tool 事件都不出门。首个适配器是企业微信自建应用；回调加解密用可选依赖 `uv sync --extra messaging`，公网回调地址需要操作者自行用 frp / Tailscale / VPS / HTTPS 暴露，并用 sender user ID 做 allowlist。微信个人号暂不内置：它依赖非官方扫码桥，有封号风险；适配器 seam 保留给未来自愿 opt-in。
