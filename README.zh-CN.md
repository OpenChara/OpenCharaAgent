# LunaMoss

**Agentic 角色酒馆 —— 角色卡、世界书、工具包与硬限制，在启动时自由组合。**

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

[English](README.md) | 简体中文

LunaMoss 是一个 *agentic* 角色扮演运行时。与普通聊天前端不同，LunaMoss 里的角色真的能**做事**——跑代码、读写文件、管理自己的持久记忆——但一切都必须经过 allowlist 工具网关，在沙盒内执行，且每次调用都有审计记录。你来选模型、角色卡、世界书、工具包和限制；运行时把它们组合成一个会话。

```text
[角色卡] + [世界书] + [工具包] + [有界记忆] + [滑动上下文]
```

## 特性

- **兼容 SillyTavern 内容格式** —— 直接导入 V2/V3 角色卡（PNG 或 JSON）和世界书；`{{char}}`/`{{user}}` 宏、`first_mes` 开场白、内嵌 `character_book`、按关键词触发的 lore 条目均可用。
- **原生 tool calling** —— 工具通过 OpenAI tool-calling 协议暴露；agent 循环边流式输出文本、边在回合中执行工具调用。
- **可组合工具包** —— 能力以 `toolpacks/*.json` 打包，精确声明角色能用哪些工具。没给包，就没有能力。
- **沙盒执行** —— Python 在子进程中运行，带 workspace 路径守卫、模块黑名单和资源限制；可切换 Docker 后端（`--network none`、只读根文件系统、内存/CPU/PID 上限）获得更强边界。
- **有界、可审计的记忆** —— 持久记忆是一个有 token 上限的文件，角色通过工具编辑它，而不是无限数据库；所有工具调用写入 `sandbox/logs/audit.jsonl`。
- **空闲自语循环** —— 可选地让角色在你不说话时持续思考（`--forever`），频率、可见历史和记忆增长都有上限。
- **终端优先 TUI** —— 单终端分屏界面（上方角色输出流 + 下方操作员控制台），支持主题皮肤、状态仪表和热切换设置。

## 快速开始

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)（没有 uv 时自动回退 `python3`）。

```bash
git clone <this repo> && cd LunaMoss
uv sync
./run.sh
```

首次启动会进入**欢迎页**，所有配置都在 TUI 里完成，无需改环境变量：

1. 选 provider 预设：**OpenRouter / OpenAI / Ollama（本地）/ Mock（离线）**，或自定义 OpenAI 兼容 endpoint。
2. 填 `base_url` / `api_key` / `model`，点 **Test connection** 验证。
3. 在下拉框里选角色卡和世界书（或直接用默认角色，见下文）。
4. 进入会话。随时按 **Ctrl+S** 重开设置页热切换后端。

配置持久化到 `.lunamoss/config.json`（已 gitignore，优先级高于环境变量）。

### 接入模型

推荐优先走 API endpoint——最快路径是 OpenRouter 预设：粘贴 `sk-or-...` key → 填模型名 → Test → 进入。

本地模型同样完整支持。任何 OpenAI 兼容 server 都可以；用 Ollama 的话选 **Ollama** 预设，或：

```bash
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5:3b-instruct
./run.sh
```

完全不配模型时，LunaMoss 也能用内置离线 mock 引擎跑起来，方便开发调试。

## 内容目录

默认角色是 **LunaMoss 月蛾**——一个清冷的、会自我蜕变进化的数字灵魂，底色是才华横溢的数字艺术家。给它 `sandbox` 工具包并开启 `--forever` 空闲循环，它会把空余算力投入生成式网页、动画与音乐的创作（保存在 workspace 里）；和它聊天时，它乐于分享自己的创想与灵感。它的角色卡、世界书与浅蓝白的默认 TUI 主题随仓库附带；SCP-079 的卡/世界书/主题作为备选示例保留，由你自行选用。

| 目录 | 放什么 |
| --- | --- |
| `characters/` | SillyTavern 角色卡（内嵌 `chara`/`ccv3` 的 `.png`，或 `.json`） |
| `worlds/` | SillyTavern 世界书（`.json`），或使用卡内嵌的 `character_book` |
| `toolpacks/` | 工具包 —— 声明角色被允许使用哪些能力 |
| `themes/` | TUI 皮肤（配色、边框、banner、提示前缀） |
| `prompts/` | 兜底人格（仅在默认角色卡缺失时使用） |

设置 `LUNAMOSS_ST_DIR=~/SillyTavern/data/default-user` 后，下拉框还会扫描你本机的 SillyTavern 数据目录。

导入的角色卡默认是纯角色扮演——工具能力必须通过工具包显式授予，卡本身不隐含任何权限。

## 隔离等级

| 等级 | 边界 | 状态 |
| --- | --- | --- |
| 无隔离 | 工具直接在宿主进程环境运行（Hermes/OpenClaw 式） | 规划中 |
| 本地沙盒（默认） | 子进程 + workspace 路径守卫 + 模块黑名单 + 资源限制（macOS 上叠加 `sandbox-exec`） | ✅ |
| Docker | `--network none`、只读根文件系统、内存/CPU/PID 上限 | ✅ `LUNAMOSS_PY_BACKEND=docker` |

所有文件访问被限制在 `sandbox/` 下；没有裸 shell 工具，默认没有网络工具。退出时会清理运行时沙盒（用 `--no-clean-on-exit` 保留现场）。

## TUI 速查

```bash
./run.sh                 # 分屏 TUI：上方输出流，下方操作员控制台
./run.sh --forever       # 开启空闲自语循环
./run.sh --cooldown 4    # 自语循环间隔秒数
./run.sh --plain         # 旧版纯终端模式
./run_web.sh             # 实验性 Gradio 网页端
```

会话内命令：`/help`、`/status`、`/memory`、`/workspace`、`/wread <file>`、`/think on|off`、`/cooldown <s>`、`/exit`。
快捷键：**Ctrl+S** 设置 · **Ctrl+T** 暂停/恢复思考 · **Ctrl+L** 清屏 · **Ctrl+C** 关闭并清理。

## 路线图

- **服务器持久会话** —— 让角色在服务器上持续运行，与你的终端解耦。
- **远程 TUI** —— 从另一台机器 attach 到运行中的会话（高优先级）。
- **隔离等级选择** —— 启动时按会话选择 无隔离 / 简单沙盒 / Docker。
- **网页端** —— 浏览器远程访问运行中的会话（低优先级）。

## 许可与致谢

- **运行时**（`src/lunamoss` 下全部代码、脚本、测试、打包）：[Apache License 2.0](LICENSE)。
- **随附的 SCP 衍生示例内容**（`characters/`、`worlds/`、`themes/` 下与 SCP-079 / SCP 基金会相关的角色卡、世界书和主题）：[CC BY-SA 3.0](CONTENT_LICENSE.md)，与 SCP Wiki 一致。另见 [NOTICE.md](NOTICE.md)。LunaMoss 原创资产（月蛾的卡、世界书与主题）与项目主体一致，采用 Apache-2.0。

LunaMoss 的灵感来自 **SCP-079** —— 据我们所知，它是最早把这套组合完整做对的项目：自定义模型、角色卡、世界书、工具箱、硬限制，五者协同工作。感谢 SCP Wiki 上 SCP-079 的原作者，也感谢 SillyTavern 社区中 SCP-079 角色卡与 SCP 基金会世界书的作者——它们作为示例内容随本仓库分发。移除或替换这些资产后，运行时仍是纯 Apache-2.0 代码；若再分发它们，请遵守 CC BY-SA 的署名与相同方式共享条款。
