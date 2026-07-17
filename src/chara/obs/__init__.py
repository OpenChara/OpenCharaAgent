"""Observability: diagnostic logging + the in-memory log ring.

Three records, three jobs — keep them distinct:

    transcript.db   the conversation itself (the chara's own history)
    audit.jsonl     the security trail of tool calls (what acted on what)
    logs/*.log      THIS package: runtime diagnostics (errors, retries,
                    subprocess lifecycles) for the operator and developers
"""
from .broker import broker
from .log import get_logger, setup_logging

__all__ = ["broker", "get_logger", "setup_logging"]
