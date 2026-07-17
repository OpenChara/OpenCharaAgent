"""The Textual TUI, split into the conversation surface (app.py) and the
welcome/settings screen (welcome.py). Both are CharaHandle clients."""
from .app import OpenCharaAgentTUI, main

__all__ = ["OpenCharaAgentTUI", "main"]
