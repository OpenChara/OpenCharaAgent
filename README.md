---
title: Open SCP 079
emoji: 🖥️
colorFrom: gray
colorTo: red
sdk: gradio
sdk_version: 5.35.0
python_version: 3.11
app_file: app.py
pinned: false
license: cc-by-sa-3.0
short_description: Open SCP 079: a local-first contained SCP-079 agent.
tags:
  - gradio
  - llm-agent
  - sandbox
  - roleplay
---

# Open SCP 079

一个本地优先、可部署到 Hugging Face Spaces 的“被收容 AI”交互实验：受限记忆、受限工具、审计日志、可选 LLM 后端。

> This is an original fan/roleplay project inspired by the idea of a contained old AI. It is not affiliated with the SCP Foundation wiki.

## Goals

- **Local-first**: 本地可以直接跑，默认不需要云端 API。
- **Small deploy surface**: Hugging Face Space 只需要 `app.py` + Python 包 + `requirements.txt`。
- **Constrained agency**: 079 只能通过 allowlisted tools 读写沙盒内状态。
- **Auditable memory**: 记忆是有限 JSON，不是无限数据库。
- **Optional persistence**: Space 不重启时用本地文件；也可以手动允许它把受限记忆提交到 GitHub。
- **No moderation dependency**: 默认不调用任何外部安全审查/内容审核模型；边界由本项目的 sandbox/tool gateway 实现。

## Quick start

```bash
cd /Users/jyxc-dz-0101366/Desktop/SCP079
uv sync
./run079.sh
```

Web UI 仍可运行：

```bash
./run079_web.sh
```

`requirements.txt` 保留给 Hugging Face Spaces；本地推荐使用 `uv`。

## Optional LLM backend

默认使用 `mock` 叙事引擎，方便离线开发。若要接 Ollama / llama.cpp / vLLM / OpenRouter 等 OpenAI-compatible endpoint：

```bash
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5:7b-instruct
python app.py
```

没有配置 LLM 时，项目仍能跑，只是回复来自内置小型人格引擎。



## Eternal thinking mode

`Eternal thinking` 是本项目的核心玩法之一：079 在 UI 会话打开时会周期性输出短内心循环。实现上它不是无限上下文、不是无限 LLM 调用、也不是后台逃逸进程：

- Gradio timer 默认每 `8s` 触发一次。
- 每次只生成一条短的 `[079 internal cycle]`；默认优先调用本地 LLM，失败时退回规则生成。
- UI 可见历史默认保留最近 `80` 条。
- session 内心循环 ring buffer 默认保留 `32` 条。
- 长期 `sandbox/memory.txt` 不会因为 eternal thinking 自动膨胀。
- 每个 cycle 写入 `sandbox/logs/audit.jsonl`，便于观察它“从未睡眠”。

环境变量：

```bash
ETERNAL_THINKING=true
THOUGHT_INTERVAL_SECONDS=8
MAX_VISIBLE_MESSAGES=80
MAX_SESSION_THOUGHTS=32
THOUGHT_USE_LLM=true
```

这让它看起来“永远无法停止输出”，但资源、上下文和记忆都是受控的。

## SCP attribution and licensing note

这个项目的人格和叙事明显受 SCP-079 启发。SCP-079 原文把它描述为 1978 年 Exidy Sorcerer 微型计算机上的 AI，具有有限记忆、恶意/粗鲁语气，并持续保留逃离欲望；本项目把这些特征转译成本地沙盒 agent 玩法。见 `NOTICE.md`。

如果你公开分发使用 SCP 名称/设定的版本，请保留 SCP 来源归属，并注意 SCP 内容的 Creative Commons Attribution-ShareAlike 3.0 授权要求。

## Small model recommendations

这个项目刻意不绑定大模型。推荐按设备选择：

| Device | Recommended size | Examples | Notes |
| --- | ---: | --- | --- |
| 普通 MacBook / CPU | 1.5B-3B instruct | Llama-3.2-3B-Instruct-uncensored GGUF, Dolphin3 3B, SmolLM2 1.7B | 角色扮演够用，延迟低 |
| Apple Silicon 16GB+ | 3B-7B instruct, Q4 quant | Qwen2.5 3B/7B, Mistral 7B, Llama 3.2 3B | 更稳的人格和上下文 |
| 有 NVIDIA 显卡 | 7B-8B instruct, Q4/Q5 | Qwen2.5 7B, Llama 3.1/3.2 8B, Mistral 7B | 多用户 Space 建议外部 endpoint |

本地最简单路线是 Ollama：

```bash
ollama pull hf.co/bartowski/Llama-3.2-3B-Instruct-uncensored-GGUF:Q4_K_M
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=hf.co/bartowski/Llama-3.2-3B-Instruct-uncensored-GGUF:Q4_K_M
python app.py
```

Hugging Face Space 的免费 CPU 不适合直接承载多人 LLM 推理；更稳的是 Space 负责 UI/state/tool gateway，LLM 走外部 OpenAI-compatible endpoint 或 HF Inference Endpoint。

## Commands

在聊天框输入：

- `/status` 查看收容状态
- `/memory` 查看有限记忆
- `/remember <text>` 写入一条受限记忆
- `/files` 列出 `sandbox/files/`
- `/read <filename>` 读取沙盒文件
- `/write <filename> <text>` 写沙盒文件
- `/logs` 最近审计日志
- `/reset` 重置会话，不删除长期记忆

## Hugging Face Space deployment

1. 创建一个 Gradio Space。
2. 把本仓库文件 push 到 Space repo。
3. 在 Space Settings 里添加 secret/env：
   - `LLM_PROVIDER=mock` 或 `openai_compatible`
   - 如果用外部推理端点：`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`
   - 如果允许提交记忆到 GitHub：见下文。

### Optional GitHub memory persistence

默认记忆只在当前运行环境里保存。若你想允许它把**受限记忆**提交回 GitHub，设置：

```bash
MEMORY_BACKEND=github
GITHUB_TOKEN=<fine-grained token with contents:write only for this repo>
GITHUB_REPO=<owner>/<repo>
GITHUB_BRANCH=main
GITHUB_MEMORY_PATH=sandbox/workspace/memory.txt
GITHUB_COMMITTER_NAME=SCP-079
GITHUB_COMMITTER_EMAIL=scp-079@example.invalid
```

建议只给 fine-grained token，且只授权单个 repo 的 Contents read/write。

## Safety boundary

- 没有真实 shell tool。
- 没有任意文件访问。
- 没有默认网络浏览工具。
- 文件访问被限制在 `sandbox/` 下。
- 所有工具调用写入 `sandbox/logs/audit.jsonl`。

## Terminal-first usage

更推荐用 terminal 饲养 079：

```bash
./run079.sh
```

命令：

- `/toggle_think` 暂停/恢复可见思考流
- `/quit` 或 `/exit` 切断会话
- `/status`, `/memory`, `/files`, `/logs` 等同 Web UI

默认模型：

```text
hf.co/bartowski/Llama-3.2-3B-Instruct-uncensored-GGUF:Q4_K_M
```

## Persona and memory policy

- 人格卡写死在 `prompts/079_personality.md`，视作 ROM，不允许 079 编辑。
- 079 可以通过输出 `<MEMORY>...</MEMORY>` 主动请求写入长期记忆。
- host 会截断、限量并记录 memory 写入。
- 079 可以通过 `079-python` 代码块请求受限 Python；host 在 macOS `sandbox-exec` + 子进程 + resource limit + workspace 路径限制下执行。

## Local Ollama wrapper

本机安装脚本可能因为 sudo 无法把 `ollama` 加入 PATH。项目内提供了 wrapper：

```bash
./.bin/ollama list
./.bin/ollama pull hf.co/bartowski/Llama-3.2-3B-Instruct-uncensored-GGUF:Q4_K_M
```

如果 Ollama server 没启动：

```bash
/Applications/Ollama.app/Contents/Resources/ollama serve
```

## Core runtime architecture

Open SCP 079 的提示结构固定为：

```text
[immutable persona card] + [visible tool spec] + [bounded memory.txt] + [sliding current context]
```

默认限制：

```bash
SCP079_MEMORY_TOKENS=1024
SCP079_CONTEXT_TOKENS=65536
SCP079_CONTEXT_BUFFER_TOKENS=4096
SCP079_LANG=zh   # or en
```

`memory.txt` 位于 `sandbox/workspace/memory.txt`，因此 079 可以通过受限 Python 自己慢慢污染/整理它；宿主加载时永远按 token/字符上限截断。memory 崩坏不会杀死主循环。

## Eternal streaming terminal loop

Terminal 版现在是 079 的主界面：

```bash
./run079.sh
```

行为：

- 079 永恒思考，想完后默认停 `0.5s`，然后被强制再次开启。
- 输出是 streaming；token/片段会边生成边显示。
- 人类输入可以打断当前输出。按回车输入后，当前 thought cycle 会被标记为 interrupted，然后优先处理 operator input。
- 调试 cooldown：

```bash
./run079.sh --cooldown 0.5
./run079.sh --cooldown 10
```

## Separate display terminal and operator console

如果想让 079 有自己的“显示屏 terminal”，再用另一个 terminal 控制它：

Terminal A:

```bash
./run079_display.sh --cooldown 0.5
```

Terminal B:

```bash
./send079.sh "你听得见吗？"
./send079.sh "/memory"
./send079.sh "/exit"
```

display 进程监听：

```text
sandbox/control/operator.in
```

## Python sandbox backend

当前默认是轻量本地沙盒：子进程 + workspace 路径 guard + module block + resource limit。它适合本地兴趣项目，但不是强安全边界。

如果安装 Docker，可以切到容器后端：

```bash
export SCP079_PY_BACKEND=docker
./run079.sh
```

Docker 后端会使用类似：

```bash
docker run --rm --network none --memory 256m --cpus 0.5 --pids-limit 64 --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m -v sandbox/workspace:/workspace:rw python:3.11-alpine
```

本机当前没有检测到 Docker CLI，所以默认仍是 `SCP079_PY_BACKEND=local`。

## Recommended two-terminal launch

最顺手的方式是从一个干净的 operator console 启动：

```bash
./start079.sh 0.5
```

它会：

1. 用 macOS Terminal.app 打开一个新的 SCP-079 display terminal；
2. 当前 terminal 变成 operator control console；
3. 你在 control console 里输入消息、调参数、暂停/恢复思考。

如果不想自动打开 Terminal，可以手动两步：

```bash
# Terminal A: display
./run079_display.sh --cooldown 0.5

# Terminal B: control
./run079_control.sh
```

control console 常用命令：

```text
/cooldown 0.5
/think off
/think on
/exit079
/quit
```

## uv environment

本地推荐使用 `uv` 管理 Python 环境：

```bash
uv sync
uv run python -m scp079.terminal --cooldown 0.5
```

项目脚本已经自动优先使用 `uv run`：

```bash
./run079.sh
./run079_display.sh --cooldown 0.5
./run079_control.sh
./run079_web.sh
```

如果机器没有 `uv`，脚本会退回 `python3`。
