"""Builtin tool modules — each file self-registers into ``tools.registry``
at import via a top-level ``registry.register(...)`` call. The gateway calls
``discover_builtin_tools()`` (AST-scan + import) once at startup.

A tool module is an island: it imports from ``..registry`` and ``..context``
and nothing else under tools/, so modules can be added in parallel without
touching any shared file.
"""
