from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SANDBOX_ROOT = Path(os.getenv("SCP079_SANDBOX", ROOT / "sandbox")).resolve()


@dataclass(frozen=True)
class LLMConfig:
    provider: str = os.getenv("LLM_PROVIDER", "mock").strip().lower()
    base_url: str = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
    api_key: str = os.getenv("OPENAI_API_KEY", "")
    model: str = os.getenv("OPENAI_MODEL", "dolphin-phi:2.7b")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.85"))
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "420"))


@dataclass(frozen=True)
class GitHubMemoryConfig:
    enabled: bool = os.getenv("MEMORY_BACKEND", "local").strip().lower() == "github"
    token: str = os.getenv("GITHUB_TOKEN", "")
    repo: str = os.getenv("GITHUB_REPO", "")
    branch: str = os.getenv("GITHUB_BRANCH", "main")
    path: str = os.getenv("GITHUB_MEMORY_PATH", "sandbox/memory.json")
    committer_name: str = os.getenv("GITHUB_COMMITTER_NAME", "SCP-079")
    committer_email: str = os.getenv("GITHUB_COMMITTER_EMAIL", "scp-079@example.invalid")


@dataclass(frozen=True)
class ThoughtConfig:
    enabled_default: bool = os.getenv("ETERNAL_THINKING", "true").strip().lower() in {"1", "true", "yes", "on"}
    interval_seconds: float = float(os.getenv("THOUGHT_INTERVAL_SECONDS", "8"))
    max_visible_messages: int = int(os.getenv("MAX_VISIBLE_MESSAGES", "80"))
    max_session_thoughts: int = int(os.getenv("MAX_SESSION_THOUGHTS", "32"))
    use_llm: bool = os.getenv("THOUGHT_USE_LLM", "true").strip().lower() in {"1", "true", "yes", "on"}
