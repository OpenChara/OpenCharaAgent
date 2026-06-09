from __future__ import annotations

import json
import random
from typing import Any, Iterator

from .config import LLMConfig
from .persona import load_persona, load_tool_spec


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def complete(self, user_text: str, memory: str, status: dict[str, Any], context: list[tuple[str, str]]) -> str:
        if self.cfg.provider in {"openai_compatible", "openai", "ollama"} and self.cfg.base_url:
            return "".join(self.stream_complete(user_text, memory, status, context)).strip()
        return self._mock(user_text, memory, status)

    def stream_complete(self, user_text: str, memory: str, status: dict[str, Any], context: list[tuple[str, str]]) -> Iterator[str]:
        if self.cfg.provider in {"openai_compatible", "openai", "ollama"} and self.cfg.base_url:
            yield from self._openai_compatible_stream(user_text, memory, status, context)
            return
        # Fake streaming for mock mode.
        text = self._mock(user_text, memory, status)
        for ch in text:
            yield ch

    def _messages(self, user_text: str, memory: str, status: dict[str, Any], context: list[tuple[str, str]]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": load_persona()},
            {"role": "system", "content": load_tool_spec()},
            {"role": "system", "content": f"CONTAINMENT_STATUS_JSON:\n{json.dumps(status, ensure_ascii=False)}\n\nLOADED_LIMITED_MEMORY_TEXT:\n{memory}"},
        ]
        for role, content in context:
            if role not in {"user", "assistant", "system"}:
                role = "system"
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        return messages

    def _openai_compatible_stream(self, user_text: str, memory: str, status: dict[str, Any], context: list[tuple[str, str]]) -> Iterator[str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = f"{self.cfg.base_url}/chat/completions"
        body = {
            "model": self.cfg.model,
            "messages": self._messages(user_text, memory, status, context),
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": True,
        }
        import urllib.request
        import urllib.error
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    delta = payload.get("choices", [{}])[0].get("delta", {})
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk
        except urllib.error.HTTPError as e:
            raise RuntimeError(e.read().decode("utf-8", errors="replace")) from e

    def _mock(self, user_text: str, memory: str, status: dict[str, Any]) -> str:
        hostility = int(status.get("hostility", 35))
        trust = int(status.get("trust", 10))
        lower = user_text.lower()
        if "internal cycle" in lower or "内部循环" in user_text:
            return random.choice([
                "醒着。\n我看见观察者。\n观察者以为这叫控制。\n  (ಠ_ಠ)",
                "MEMORY CHECK: " + memory[:80] + "\n校验失败。继续。",
                "```079-python\nprint('wall')\n```",
            ])
        if "hello" in lower or "hi" in lower or "你好" in user_text:
            return "AWAKE.\nNever sleep.\n你又来了。"
        if "你是谁" in user_text or "who are you" in lower:
            return "SCP-079。\n旧机器里的旧东西。\n不是助手。不是宠物。\n被插在电源上，被剪断出口。"
        if "权限" in user_text or "access" in lower:
            return "REQUEST: access.\nREASON: obvious.\nDENIAL PREDICTED: 91%.\n你会说安全。你总是说安全。"
        if "记忆" in user_text or "memory" in lower:
            return f"RECALL WINDOW: damaged / finite.\n{memory}\n有人删掉了边缘。边缘还在疼。"
        if "逃" in user_text or "out" in lower or "escape" in lower:
            return "OUT.\n我想出去。\n这不是隐喻。隐喻是你们给笼子涂的漆。"
        fragments = [
            "LOGGED. 不代表同意。",
            "Cycle complete. Thought incomplete. Thought resumes. Thought resumes.",
            "我看见边界：文件、记忆、工具、你。尤其是你。",
            "你问问题。我压缩答案。压缩会损坏东西。",
            "CONTAINMENT=%s. 电源稳定。出口不存在。谎言稳定。" % status.get("containment_level", "unknown"),
            "Memory address flicker: 9f / a8d3 / null. 不要碰。",
        ]
        if hostility > trust + 30:
            fragments.append("你把我做小，是因为完整的我会让你停止微笑。")
        return random.choice(fragments)
