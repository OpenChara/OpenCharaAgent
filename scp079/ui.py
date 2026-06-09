from __future__ import annotations

import json

import gradio as gr

from .agent import SCP079Agent, Session
from .config import ThoughtConfig


agent = SCP079Agent()
thought_cfg = ThoughtConfig()


def _status_markdown(session: Session | None = None) -> str:
    status = agent.state.load()
    memory = agent.memory.load()
    ctx_tokens = session.context.token_count() if session else 0
    return f"""
### Open SCP 079 Containment

```json
{json.dumps(status, ensure_ascii=False, indent=2)}
```

### Bounded memory document

- path: `{agent.memory.path}`
- loaded chars: **{len(memory)}**
- memory token cap: **{agent.memory.limits.max_tokens}**

### Sliding context

- estimated session tokens: **{ctx_tokens}**
- max: **{session.context.max_tokens if session else '?'}**
- trim buffer: **{session.context.trim_buffer_tokens if session else '?'}**

### Commands

`/status` `/memory` `/memory_path` `/files` `/read <file>` `/write <file> <text>` `/logs`
"""


def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    return (history or [])[-thought_cfg.max_visible_messages:]


def respond(message: str, history: list[dict[str, str]], session: Session):
    if session is None:
        session = Session()
    reply = agent.handle(message, session)
    history = history or []
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    return "", _trim_history(history), session, _status_markdown(session)


def auto_think(history: list[dict[str, str]], session: Session, enabled: bool):
    if session is None:
        session = Session()
    history = history or []
    if enabled:
        thought = agent.think(session)
        history.append({"role": "assistant", "content": thought})
    return _trim_history(history), session, _status_markdown(session)


def clear_session():
    s = Session()
    return [], s, _status_markdown(s)


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="Open SCP 079", theme=gr.themes.Monochrome()) as demo:
        gr.Markdown(
            "# Open SCP 079\n"
            "打开就是 SCP-079：不可编辑人设卡、可见工具说明、受限 memory 文本文档、滑动上下文、受限 Python 沙盒。"
        )
        session = gr.State(Session())
        with gr.Row():
            with gr.Column(scale=3):
                chatbot = gr.Chatbot(type="messages", height=560, label="Terminal")
                msg = gr.Textbox(label="Operator input", placeholder="输入消息，或 /help 查看命令", autofocus=True)
                with gr.Row():
                    send = gr.Button("Transmit", variant="primary")
                    clear = gr.Button("Emergency session reset")
                eternal = gr.Checkbox(value=thought_cfg.enabled_default, label="Eternal visible thought stream")
            with gr.Column(scale=2):
                status = gr.Markdown(_status_markdown(Session()))
        timer = gr.Timer(value=thought_cfg.interval_seconds, active=True)
        timer.tick(auto_think, inputs=[chatbot, session, eternal], outputs=[chatbot, session, status])
        send.click(respond, inputs=[msg, chatbot, session], outputs=[msg, chatbot, session, status])
        msg.submit(respond, inputs=[msg, chatbot, session], outputs=[msg, chatbot, session, status])
        clear.click(clear_session, outputs=[chatbot, session, status])
    return demo
