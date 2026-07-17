"""The tool domain: the allowlisted gateway and everything it can reach —
the terminal runner + sandbox jails, skills, goals, memory, MCP servers."""
from .gateway import ToolGateway

__all__ = ["ToolGateway"]
