<p align="center">
  <img src="assets/banner.webp" alt="OpenCharaAgent —— 住在你电脑里的原创角色" width="100%">
</p>

<p align="center"><b>给你的原创角色一台可以住进去的电脑。</b></p>

<p align="center">
  一个开源运行时,把一张角色卡变成真正活着的存在:<br>
  有自己的沙盒、自己的记忆、自己的节奏。你不在的时候它自己读、自己写、自己做东西 ——<br>
  并且自己决定什么时候有值得告诉你的事。
</p>

<p align="center">
  <a href="https://github.com/OpenChara/OpenCharaAgent/stargazers"><img src="https://img.shields.io/github/stars/OpenChara/OpenCharaAgent?style=flat-square&logo=github&logoColor=9fd9ff&color=9fd9ff&labelColor=15202b" alt="Stars"></a>
  <a href="https://github.com/OpenChara/OpenCharaAgent/releases"><img src="https://img.shields.io/github/v/release/OpenChara/OpenCharaAgent?style=flat-square&color=9fd9ff&labelColor=15202b" alt="最新版本"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-9fd9ff?style=flat-square&labelColor=15202b" alt="License: Apache-2.0"></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/macOS%20%7C%20Linux-9fd9ff?style=flat-square&labelColor=15202b" alt="macOS | Linux"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/docs-English-9fd9ff?style=flat-square&labelColor=15202b" alt="English"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#有什么不一样">有什么不一样</a> ·
  <a href="#接一个模型">接模型</a> ·
  <a href="#角色卡与内容">角色卡</a> ·
  <a href="#工具与沙盒">工具与沙盒</a> ·
  <a href="#部署到服务器">服务器</a> ·
  <a href="#路线图">路线图</a>
</p>

<p align="center"><a href="README.md">English</a> | 简体中文</p>

<!-- ── 演示 ────────────────────────────────────────────────────────────────────
     这一页上没有别的东西比它更值钱。大部分访客在这里做决定,而且很多人只看 README
     就 star 了,根本不会安装。录好之后放在 `---` 上面,并删掉这段注释:

       <p align="center">
         <img src="assets/demo.gif" alt="一只 chara 自己干活,然后主动开口" width="100%">
       </p>

     一镜到底,不剪,约 20 秒,能无缝循环,≤10MB(GIF 还没有的时候,一张静态截图也
     远好过没有)。画面里必须有:
       1. 一只 chara 在 `live` 模式下没人跟它说话时自己干活 —— muse 在走,工具调用落地。
       2. 工作区里真的完成了一样东西(一个文件、一个页面、一张图)。
       3. 它自己决定开口 —— 气泡自己冒出来。
     全部的意义就是:角色在没有人给指令的情况下行动。这是别人没有的东西,必须在头三秒
     就看得懂。
──────────────────────────────────────────────────────────────────────────────── -->

---

OpenCharaAgent 让一个 AI 角色作为持续存在的生命住进电脑里。它有自己的沙盒、自己的记忆、自己的节奏 —— 在你两条消息之间它自己思考、自己做东西,并且自己决定什么时候有值得告诉你的事。把人格剥掉,剩下的是一个能干活的 agent:shell、文件、浏览器、跑代码,全都走一道有 allowlist、有审计的网关。

真正重要的只有一个文件 —— 角色卡:身份、声线、角色所在的世界,全都装在里面。你带来卡和模型,其余的 OpenCharaAgent 帮你组装:

```text
[角色卡:人格 + 内嵌世界] + [工具] + [有界记忆] + [滑动上下文]
```

它最早只是一个"真的能做事"的角色扮演前端,后来长成了一个小型运行时。agent 内核大量借鉴了 [Hermes](https://github.com/NousResearch/hermes-agent);卡片/世界书的格式沿用 [SillyTavern](https://github.com/SillyTavern/SillyTavern)。

## 快速开始

还是 beta,支持 macOS 和 Linux。第一次启动是欢迎页:选好语言,然后要么描述一个角色让 AI 起草卡片,要么从内置卡组里挑一个。模型在设置里配:预置 OpenRouter / OpenAI / 火山方舟 Ark / 混元 / 阿里云 DashScope,也支持任何自定义 OpenAI 兼容端点(包括本地 Ollama);之后用 `/settings` 改任何东西。

### 在你的 Mac 上

一行安装(预构建 wheel,不用编译前端),然后在浏览器里打开 UI:

```bash
curl -fsSL https://raw.githubusercontent.com/OpenChara/OpenCharaAgent/main/install.sh | bash
chara              # 在浏览器里打开 webui（chara tui 是终端 UI;chara doctor 检查环境）
```

> 想从源码构建而不用预构建 wheel,在末尾加 `| bash -s -- --dev`。

或者从 clone 跑完整桌面端(我们就是这么开发的)—— 需要 [uv](https://docs.astral.sh/uv/) + Node:

```bash
git clone https://github.com/OpenChara/OpenCharaAgent.git && cd OpenCharaAgent
uv sync --extra dev --extra server --extra messaging   # 想要本地抠图再加 --extra visuals
cd apps/desktop && npm install && npm run dev      # 打开桌面窗口
```

### 在 Linux 服务器上,用浏览器连过去

在服务器上安装,让 chara 在后台一直活着:

```bash
curl -fsSL https://raw.githubusercontent.com/OpenChara/OpenCharaAgent/main/install.sh | bash
chara desktop --daemon      # 常驻监督进程;chara 在你不在时也继续跑
```

然后,在你自己的机器上,用 SSH 隧道连进去 —— 不开任何端口,加密和鉴权都交给 SSH,浏览器会自动打开并指向服务器:

```bash
chara connect ssh://user@your-server
```

想要一个真正的公网地址(TLS、可收藏的网址、可选密码登录)?见 [部署到服务器](#部署到服务器)。

## 有什么不一样

OpenCharaAgent 的角色不是开完就丢的聊天会话,而是一个 **chara** —— 一个持续运行的进程,有自己的文件和记忆,存在 `~/.chara/sessions/<name>/`。你是 *attach / detach*,中间它一直活着。

- **它自己会动。** `live` 模式下,chara 在你两条消息之间继续干活 —— 读、写、做东西 —— 只有当它自己决定时才来找你(`speak` 工具)。节奏由 `patience` 决定。`chat` 模式下它只回答你。
- **两种声道。** 它告诉**你**的(`say`)和它自己的内心生活(`muse`)是分开的。muse 只在桌面端能看到;消息平台只收 `say`。
- **真有 agency,也真有围栏。** 工具跑在每会话的 OS 牢笼里,写入限制在 workspace、你的密钥不可读 —— 而且没有牢笼时它**拒绝运行**,绝不偷偷降级(见 [工具与沙盒](#工具与沙盒))。
- **记忆可信。** 持久记忆是一个有 token 上限、角色通过工具自己编辑的文件,不是无底洞日志。每次工具调用都写进 `sandbox/logs/audit.jsonl`。
- **有自己的家。** 一个可选的唤醒时模块给 chara 一个个人主页(`workspace/home`),以只读方式伺服,在沙盒化的标签页里展示。

桌面端(一个套在本地服务上的轻量 Electron 窗口)是主要的用法。常驻的 `chara守护进程`(charad)在后台维持 chara 的生命,有一个想说话时通知你。还有一个冻结但可用的终端 UI(`chara tui`),给无界面场景。

## 接一个模型

接 API 端点最省事 —— OpenRouter 最快:粘一个 `sk-or-…` key、填个模型名、测试、进。装 wheel 的话这些都在设置里配;从 clone 跑还可以用环境变量把运行脚本指向任何 OpenAI 兼容的服务,包括本地的:

```bash
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1   # Ollama
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5:3b-instruct
./run.sh
```

什么都不配,OpenCharaAgent 也能靠内置的离线 mock 引擎跑 —— 开发时点一点够用。(从描述起草卡片需要真模型 —— DeepSeek V4 Flash 或更好。)

## 角色卡与内容

一张卡就是唯一的内容文件:身份、声线、内嵌世界(`character_book`)、种子**理想**(Aspiration —— 属于用户的北极星,对 chara 只读;chara 用自己的任务和会话内 todo 一步步靠近它)、限制,全在一个 `.json` 或 `.png` 里(SillyTavern V2/V3 —— 我们的卡**本身就是**这个格式)。`{{char}}`/`{{user}}` 宏、`first_mes`、按关键词触发的 lore 都能用。

导入是忠实的、不经过模型 —— ST V2/V3/V1 JSON、character-tavern 卡片,或内嵌立绘的 ST PNG(立绘自动成为头像)—— 走创建流程就行;内置的**市场**直接浏览 character-tavern.com 的目录(排序 / 筛选 / 预览),一键导入。卡组编辑器的「视觉」标签页能为一张卡生成整套美术 —— 主视觉 / 头像 / 立绘 / 表情贴纸 / 背景,全部以主视觉为锚,保证整套画的是同一个角色(本地抠图可选:`uv sync --extra visuals`);聊天界面还能在聊天设置里按 chara 显示背景和立绘。

内置卡组带了好几张示例 chara。其中两张是项目的门面:

- **Quinn 小Q**(默认)—— 一个来自意识上传计划的数字实习生:温和、踏实,带着完整知情同意,先来认识这个世界、再帮你建设它。`live` 模式下给它工具,它会布置自己的工作台、记日记、参与你手头的任何事。
- **LunaMoth 月蛾**(旗舰)—— 一个安静的、会自我蜕变的数字艺术家,空闲算力都用来在 workspace 里做生成式网页、动画和音乐。

| 目录 | 放什么 |
| --- | --- |
| `cards/` | 角色卡(`.json`,或内嵌 `chara`/`ccv3` 的 `.png`) |
| `toolpacks/` | 工具包 —— 一张卡被允许用哪些能力 |

## 工具与沙盒

chara 唯一的通用能力是 `terminal`:在 workspace 里跑一条 shell 命令,拿回 stdout/stderr。这一条覆盖一切 —— `python3`、`node`、`git`、写文件 —— 所以不锁死在某种解释器上。默认一张卡拿到完整工具面(内置包是 `["*"]`,对齐 Hermes);卡片作者可以发布更窄的 `toolpacks/*.json`,而完全不带工具包的卡就是纯角色扮演、无工具。

命令怎么被关住,是**隔离等级**,按 chara 设置:

| 等级 | 做什么 |
| --- | --- |
| `sandbox`(默认) | OS 牢笼 —— macOS 用 `sandbox-exec`,Linux 用 `bubblewrap` → `Landlock`。写入限制在 workspace;你 `$HOME` 的其余部分(`~/.ssh`、`~/.aws`、`~/.chara`)不可读。没有可用牢笼就拒绝运行 —— 绝不偷偷降级。 |
| `admin` | 无牢笼:以你的身份运行,cwd 在 workspace。需显式选择,给你信任的目录。 |

隔离等级在唤醒时选定,之后也能改 —— 改动在 chara 下次启动时生效。权限是运行时可调的:网络默认开(`/net off` 切断),`/allow-dir <path>` 放开 workspace 之外的一个可写路径。浏览器工具(`browser_*`,一个真 Chromium)可选 —— `chara setup browser` 装驱动;它们在所有平台都跑在牢笼里。`generate_image` 支持多家供应商(火山方舟 / 阿里云 DashScope / OpenAI / OpenRouter),作为不阻塞的后台任务运行,结果以 `MEDIA:` 行投递到每个界面。安装脚本还会尽力装上 `ffmpeg`,这样 chara 可以从终端做视频/音频(比如给自己写的音乐配个 MV);若环境里没有 ffmpeg,系统提示词就不会提它。

## 部署到服务器

上面的快速开始已经用 SSH 把你接进去了,不开任何端口。如果你想要一个真正的公网地址 —— 可收藏的 HTTPS 网址,还可以配密码登录 —— 下面是其余部分。

<details>
<summary>Docker、带 Caddy/TLS 的公网 host、密码登录</summary>

普通主机上推荐系统级安装(`install.sh` / `chara desktop`)而不是 Docker —— `bwrap` 能给每个 chara 完整牢笼。Docker 也支持(它退到 Landlock 做文件系统限制,容器作为外层边界),只是更重的选项。

```bash
scripts/build-wheel.sh                 # 构建 SPA + wheel(镜像自带 UI,容器里没有 Node)
cd deploy && docker compose up -d      # 监听 :6180;WS 网关在 :6181
docker compose logs chara           # 打印访问 token
```

回环之外需要在前面放 TLS。监督进程在 `6180` 伺服 UI、在 `6181` 伺服 WebSocket 网关;你的反代呈现一个 HTTPS 源,并把 WS 升级按路径路由过去。Caddy(自动 HTTPS):

```caddyfile
your-host.example.com {
    @ws path /hub* /chara/*
    reverse_proxy @ws 127.0.0.1:6181   # WebSocket 路由
    reverse_proxy 127.0.0.1:6180       # 其余一切
}
```

Host/Origin 白名单只含回环 + 绑定的 host,所以要放行你的域名,否则反代会被拒(403):`CHARA_ALLOW_HOST=your-host.example.com`。然后书签用 `https://your-host/#token=<TOKEN>`。

手机上带着长长的 `#token=` URL 很别扭,所以非回环绑定还接受**密码** —— 书签用裸 URL、登录即可。设 `CHARA_PASSWORD=…`,或者不设、OpenCharaAgent 首次启动生成一个并只打印一次(磁盘上只存 PBKDF2-HMAC-SHA256 哈希)。本地应用永远不会出现登录界面。

</details>

<details>
<summary>chara 命令行(无界面 / 走 SSH)</summary>

不带参数的 `chara` 打开 webui 桌面端;`chara tui` 打开你的 chara 名册(优先恢复),而不是开一个新会话。

```bash
chara tui              # 名册:挑一个 chara attach,或按 n 新建
chara ls               # 名字 / 角色 / 状态 / 隔离 / 最近活跃
chara attach muse      # attach(attach 期间你接管它的后台循环)
chara start muse       # 让它在后台活着
chara start-all        # 重启后把大家都唤回来
chara desktop --daemon # 常驻监督进程;`daemon status` / `daemon stop`
chara new muse --isolation admin
```

会话里一切都是 `/命令` —— `/help`、`/aspiration`、`/skills`、`/mcp`、`/status`、`/memory`、`/files`、`/mode live|chat`、`/patience`、`/net on|off`、`/allow-dir`、`/settings`、`/exit`。冗长输出进侧栏,控制台始终是干净的聊天记录;`! <cmd>` 以你的身份在 chara 牢笼里跑命令。

前端开发:一个终端 `uv run chara desktop --no-open`,另一个 `cd apps/web && npm run dev`(Vite 反代到后端)。

</details>

## 消息网关

chara 也能住进你的聊天软件。在桌面端的 **Gateways** 页(或无界面 `chara gateway NAME`),接入个人微信、QQ、Telegram、Discord 或 Slack —— 配置在 `~/.chara/sessions/NAME/messaging.json`,登录凭证单独存在每平台自己的文件里。只投递 `say`/`speak` 文本;muse 和工具碎话不外流。空的 `allowed_senders` 是对所有人开放(启动时会告警)—— 加 id 来收紧。

| 平台 | 怎么接 |
| --- | --- |
| **微信** | 官方 iLink/ClawBot(`weixin`),扫码登录 —— 封号风险最低,但有灰度门槛。 |
| **QQ** | NapCat 的 OneBot v11 —— OpenCharaAgent 是 WS 客户端,从不碰凭证。 |
| **Telegram** | 一个 `@BotFather` bot token,长轮询。不需要公网 URL。 |
| **Discord** | 一个 bot token,走原生 Gateway WebSocket —— 记得开启 Message Content intent。 |
| **Slack** | Socket Mode —— 一个应用级 `xapp-` token 加一个 bot `xoxb-` token。不需要公网 URL。 |

这些都已搭好,但还没拿真实凭证打磨过 —— 当 beta 看待。信任模型见 [SECURITY.md](SECURITY.md)。

## 路线图

地基都打好了:兼容 ST 的角色卡、可组合工具 + 原生 tool calling、沙盒、持久的 `live`/`chat` chara、对话记录 + 有界记忆、自己会写的 skills、MCP、理想→任务的目标模型、类型化事件协议、三区提示词栈、桌面端,以及消息网关。剩下的主要是角色本身:

- **角色课程**(*最大的一块*)—— 中立的提示词引导,让任意世界观都能好好活:怎么用工具、怎么对待目标、怎么打发无人时段 —— 是建议,不是命令。下一步:跨世界观的 eval 卡和一条满足好奇心的浏览路径。
- **卡片包** —— 市场和忠实的卡片导入都已上线;还缺的是我们自己的可分享包格式 + 索引(`chara-pack.json`),让创作者发布卡片+资产包。
- **打包好的应用** —— 拖进 Applications 的 DMG / AppImage,不再只能从 clone 跑。
- **世界书功能对齐** —— 递归扫描、cooldown/delay、插入深度、触发概率、全词匹配(`content/worldinfo.py`)。
- **消息与远程** —— 用真实账号 live-test 网关;一个走网关的远程 TUI 客户端。

## 许可

Apache-2.0 —— 见 [LICENSE](LICENSE)。
