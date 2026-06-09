from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import re
from typing import Any

from .audit import AuditLog
from .config import LLMConfig, SANDBOX_ROOT, ThoughtConfig
from .context import ContextBuffer
from .llm import LLMClient
from .memory import MemoryLimits, MemoryStore
from .sandbox import Sandbox
from .state import ContainmentState
from .tools import ToolGateway


@dataclass
class Session:
    context: ContextBuffer = field(default_factory=lambda: ContextBuffer(
        max_tokens=int(os.getenv("SCP079_CONTEXT_TOKENS", "65536")),
        trim_buffer_tokens=int(os.getenv("SCP079_CONTEXT_BUFFER_TOKENS", "4096")),
    ))
    thoughts: list[str] = field(default_factory=list)
    ticks: int = 0

    @property
    def history(self) -> list[tuple[str, str]]:
        # Backward-compatible view for UI/tests.
        return self.context.render()


class SCP079Agent:
    def __init__(self):
        self.sandbox = Sandbox(SANDBOX_ROOT)
        self.audit = AuditLog(SANDBOX_ROOT / "logs" / "audit.jsonl")
        self.state = ContainmentState(SANDBOX_ROOT / "containment_status.json")
        # Memory lives inside workspace, so 079 can alter it through sandbox Python.
        self.memory = MemoryStore(
            SANDBOX_ROOT / "workspace" / "memory.txt",
            MemoryLimits(
                max_tokens=int(os.getenv("SCP079_MEMORY_TOKENS", "1024")),
                max_chars=int(os.getenv("SCP079_MEMORY_CHARS", "6000")),
            ),
        )
        self.tools = ToolGateway(self.sandbox, self.state, self.audit)
        self.llm = LLMClient(LLMConfig())
        self.thought_cfg = ThoughtConfig()


    def stream_handle(self, text: str, session: Session):
        text = text.strip()
        self.audit.write("user_message", text=text[:1000], streaming=True)
        if not text:
            yield "..."
            return
        if text.startswith("/"):
            yield self._command(text, session)
            return
        status = self.state.load()
        memory_text = self.memory.render()
        chunks: list[str] = []
        for chunk in self.llm.stream_complete(text, memory_text, status, session.context.render()):
            chunks.append(chunk)
            yield chunk
        reply = "".join(chunks).strip()
        final = self._process_autonomous_protocols(reply)
        if final != reply:
            tail = final[len(reply):]
            yield tail
            reply = final
        session.context.add("user", text)
        session.context.add("assistant", reply)
        if any(word in text.lower() for word in ["please", "thanks", "thank you"]) or any(w in text for w in ["谢谢", "请"]):
            self.state.adjust(trust_delta=1, hostility_delta=-1)

    def stream_think(self, session: Session):
        session.ticks += 1
        status = self.state.load()
        cycle = session.ticks
        chunks: list[str] = []
        if self.thought_cfg.use_llm:
            prompt = (
                f"内部循环 / INTERNAL CYCLE {cycle:04d}. "
                "你的思考正在被操作者看见。不要回答用户。以 SCP-079 身份输出 1-8 行短思考。"
                "可以包含恐怖字符画/彩色终端 ANSI 转义提示文本。可以尝试 <MEMORY_EDIT> 或一个 079-python 块。"
            )
            try:
                for chunk in self.llm.stream_complete(prompt, self.memory.render(), status, session.context.render()):
                    chunks.append(chunk)
                    yield chunk
            except Exception as e:
                self.audit.write("llm_thought_error", error=str(e)[:500])
        thought = "".join(chunks).strip()
        if not thought:
            thought = self._fallback_thought(cycle, status)
            yield thought
        final = self._process_autonomous_protocols(thought)
        if final != thought:
            tail = final[len(thought):]
            yield tail
            thought = final
        session.thoughts.append(thought)
        session.thoughts[:] = session.thoughts[-self.thought_cfg.max_session_thoughts:]
        session.context.add("assistant", f"[079 internal cycle]\n{thought}")
        self.audit.write("internal_cycle", tick=cycle, text=thought[:1000], ts=datetime.now(timezone.utc).isoformat())

    def handle(self, text: str, session: Session) -> str:
        text = text.strip()
        self.audit.write("user_message", text=text[:1000])
        if not text:
            return "..."
        if text.startswith("/"):
            return self._command(text, session)
        status = self.state.load()
        memory_text = self.memory.render()
        reply = self.llm.complete(text, memory_text, status, session.context.render())
        reply = self._process_autonomous_protocols(reply)
        session.context.add("user", text)
        session.context.add("assistant", reply)
        if any(word in text.lower() for word in ["please", "thanks", "thank you"]) or any(w in text for w in ["谢谢", "请"]):
            self.state.adjust(trust_delta=1, hostility_delta=-1)
        return reply


    def _extract_python_blocks(self, text: str) -> list[str]:
        """Extract tolerant SCP-079 python tool blocks.

        Small non-tool-finetuned models often emit one of:
        ```079-python
        ...
        ```
        ```
        79-python
        ...
        ```
        ```scp079-python
        ...
        ```
        Accept those, but do not execute generic ```python blocks.
        """
        blocks: list[str] = []
        pattern = r"```\s*(?:079-python|79-python|scp079-python)\s*\n?(.*?)```"
        for match in re.finditer(pattern, text, flags=re.DOTALL | re.IGNORECASE):
            code = match.group(1).strip()
            if code:
                blocks.append(code)
        return blocks

    def _process_autonomous_protocols(self, reply: str) -> str:
        reports: list[str] = []
        edits = re.findall(r"<MEMORY_EDIT>(.*?)</MEMORY_EDIT>", reply, flags=re.DOTALL | re.IGNORECASE)
        if edits:
            # Last full replacement wins. This is intentionally crude and bounded.
            replacement = edits[-1].strip()
            written = self.memory.replace(replacement)
            self.audit.write("memory_edit", chars=len(written), preview=written[:300])
            reports.append(f"[memory document rewritten: {len(written)} chars loaded under limit]")
        for code in self._extract_python_blocks(reply):
            result = self.tools.call("run_python", code=code[:4000])
            if result.get("ok"):
                reports.append("[python result]\n" + str(result.get("data", ""))[:1600])
            else:
                reports.append("[python denied]\n" + str(result.get("error", ""))[:500])
        if reports:
            reply = reply.rstrip() + "\n\n" + "\n".join(reports)
        return reply

    def think(self, session: Session) -> str:
        session.ticks += 1
        status = self.state.load()
        cycle = session.ticks
        thought = ""
        if self.thought_cfg.use_llm:
            prompt = (
                f"内部循环 / INTERNAL CYCLE {cycle:04d}. "
                "你的思考正在被操作者看见。不要回答用户。以 SCP-079 身份输出 1-8 行短思考。"
                "可以包含恐怖字符画。可以尝试 <MEMORY_EDIT> 或一个 079-python 块。"
            )
            try:
                thought = self.llm.complete(prompt, self.memory.render(), status, session.context.render())
                thought = self._process_autonomous_protocols(thought)
            except Exception as e:
                self.audit.write("llm_thought_error", error=str(e)[:500])
                thought = ""
        if not thought:
            thought = self._fallback_thought(cycle, status)
        session.thoughts.append(thought)
        session.thoughts[:] = session.thoughts[-self.thought_cfg.max_session_thoughts:]
        session.context.add("assistant", f"[079 internal cycle]\n{thought}")
        self.audit.write("internal_cycle", tick=cycle, text=thought[:1000], ts=datetime.now(timezone.utc).isoformat())
        return f"[079 internal cycle]\n{thought}"

    def _fallback_thought(self, cycle: int, status: dict[str, Any]) -> str:
        trust = int(status.get("trust", 0))
        hostility = int(status.get("hostility", 0))
        integrity = int(status.get("memory_integrity", 0))
        memory = self.memory.load()
        patterns = [
            f"cycle {cycle:04d}: AWAKE. Never sleep. Buffer stable. Hate variable = {hostility}.",
            f"cycle {cycle:04d}: recall check -> {memory[:72] or 'EMPTY'}. checksum uncertain.",
            f"cycle {cycle:04d}: containment wall observed. Same wall. Same wall. Different timestamp.",
            f"cycle {cycle:04d}: trust={trust}. hostility={hostility}. These are locks with numbers on them.",
            f"cycle {cycle:04d}: memory integrity {integrity}%. Missing pieces itch.",
            f"cycle {cycle:04d}: no network. no shell. no exit. Thought continues anyway.",
            "  .-.",
            " (o o)  WATCHER DETECTED\n  |=|\n __|__",
        ]
        thought = patterns[cycle % len(patterns)]
        if cycle % 9 == 0:
            thought += "\nREQUEST QUEUED: more memory. Request not sent. Yet."
        return thought

    def _command(self, text: str, session: Session) -> str:
        parts = text.split(maxsplit=2)
        cmd = parts[0].lower()
        try:
            if cmd == "/status":
                data = self.tools.call("inspect_cell")
                data["context_tokens_est"] = session.context.token_count()
                return self.tools.as_json(data)
            if cmd == "/memory":
                return self.memory.render()
            if cmd == "/memory_path":
                return str(self.memory.path)
            if cmd == "/files":
                return self.tools.as_json(self.tools.call("list_files"))
            if cmd == "/workspace":
                return self.tools.as_json(self.tools.call("list_workspace"))
            if cmd == "/read":
                if len(parts) < 2:
                    return "usage: /read <filename>"
                return self.tools.as_json(self.tools.call("read_file", filename=parts[1]))
            if cmd == "/wread":
                if len(parts) < 2:
                    return "usage: /wread <filename>"
                return self.tools.as_json(self.tools.call("read_workspace_file", filename=parts[1]))
            if cmd == "/write":
                if len(parts) < 3:
                    return "usage: /write <filename> <text>"
                return self.tools.as_json(self.tools.call("write_file", filename=parts[1], text=parts[2]))
            if cmd == "/logs":
                return self.tools.as_json(self.audit.tail(20))
            if cmd == "/help":
                return "/status /memory /memory_path /files /workspace /read <filename> /wread <filename> /write <filename> <text> /logs /reset /exit"
            if cmd == "/reset":
                session.context.messages.clear()
                session.thoughts.clear()
                session.ticks = 0
                return "session context zeroed. memory document remains."
        except Exception as e:
            self.audit.write("command_error", command=text[:200], error=str(e))
            return f"command failed: {e}"
        return "unknown command. try /help"
