from __future__ import annotations

import base64
import json
from typing import Any


from .config import GitHubMemoryConfig


class GitHubMemoryStore:
    """Tiny GitHub Contents API adapter for one JSON memory file."""

    def __init__(self, cfg: GitHubMemoryConfig):
        self.cfg = cfg

    @property
    def ready(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.token and self.cfg.repo)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.cfg.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self) -> str:
        return f"https://api.github.com/repos/{self.cfg.repo}/contents/{self.cfg.path}"

    def load(self) -> tuple[dict[str, Any] | None, str | None]:
        if not self.ready:
            return None, None
        import requests
        r = requests.get(self._url(), headers=self._headers(), params={"ref": self.cfg.branch}, timeout=15)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        payload = r.json()
        raw = base64.b64decode(payload["content"]).decode("utf-8")
        return json.loads(raw), payload.get("sha")

    def save(self, data: dict[str, Any], message: str = "SCP-079 memory update") -> None:
        if not self.ready:
            return
        _, sha = self.load()
        content = base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")
        body: dict[str, Any] = {
            "message": message,
            "content": content,
            "branch": self.cfg.branch,
            "committer": {"name": self.cfg.committer_name, "email": self.cfg.committer_email},
        }
        if sha:
            body["sha"] = sha
        import requests
        r = requests.put(self._url(), headers=self._headers(), json=body, timeout=20)
        r.raise_for_status()
