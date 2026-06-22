"""The backend /command registry — ONE implementation for every frontend.

The TUI, the plain terminal and any future client all execute these through
CharaHandle.command(); each frontend keeps only its OWN display commands
(/panel, /theme, /clear, /settings, /patience). Help text and behavior can
therefore never drift between frontends again.

A handler takes (agent, session, arg_string) and returns a Reply: `text` is a
ready-to-display body (frontends choose where), `data` is the structured form
for richer UIs.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from ..content.knobs import DEFAULT_PATIENCE, parse_patience
from ..presence import normalize_mode
from ..protocol.api import CommandInfo, Reply


@dataclass(frozen=True)
class Command:
    info: CommandInfo
    run: "Callable[..., Reply]"


def _persist(agent, **changes) -> None:
    from ..session.settings import save_settings

    agent.settings = replace(agent.settings, **changes)
    save_settings(agent.settings)


# ---- handlers -----------------------------------------------------------------------


def _status(agent, session, arg: str) -> Reply:
    # Read the env facts straight from state (the inspect_env tool was retired —
    # the chara already sees these in its volatile prompt tail every turn).
    data = {"ok": True, "data": dict(agent.state.load())}
    data["context_tokens_est"] = session.context.token_count()
    return Reply(True, agent.tools.as_json(data), data, verbose=True)


def _memory(agent, session, arg: str) -> Reply:
    rendered = agent.memory.render()
    return Reply(True, rendered if rendered.strip() else "(empty — the chara curates this via the `memory` tool)", verbose=True)


def _memory_path(agent, session, arg: str) -> Reply:
    return Reply(True, str(agent.memory.root))


def _tool_json(agent, name: str, **kwargs) -> Reply:
    result = agent.tools.call(name, **kwargs)
    return Reply(bool(result.get("ok")), agent.tools.as_json(result), result, verbose=True)


def _files(agent, session, arg: str) -> Reply:
    return _tool_json(agent, "list_files")


def _workspace(agent, session, arg: str) -> Reply:
    # workspace/ IS the one sandbox dir, so this is list_files (the dedicated
    # list_workspace tool was retired; routing here keeps the alias working).
    return _tool_json(agent, "list_files")


def _read(agent, session, arg: str) -> Reply:
    if not arg:
        return Reply(False, "usage: /read <filename>")
    return _tool_json(agent, "read_file", filename=arg.split()[0])


def _wread(agent, session, arg: str) -> Reply:
    if not arg:
        return Reply(False, "usage: /wread <filename>")
    return _tool_json(agent, "read_file", filename=arg.split()[0])


def _write(agent, session, arg: str) -> Reply:
    parts = arg.split(maxsplit=1)
    if len(parts) < 2:
        return Reply(False, "usage: /write <filename> <text>")
    return _tool_json(agent, "write_file", filename=parts[0], text=parts[1])


def _logs(agent, session, arg: str) -> Reply:
    tail = agent.audit.tail(20)
    return Reply(True, agent.tools.as_json(tail), tail, verbose=True)


def _compact(agent, session, arg: str) -> Reply:
    before = session.context.token_count()
    if not agent.llm.is_live():
        return Reply(False, "compaction needs a live model (offline/mock can't summarize).")
    if agent._maybe_compact(session, force=True):
        after = session.context.token_count()
        return Reply(True, f"compacted: ~{before} → ~{after} tokens (older turns folded into a summary; full history stays on disk).")
    return Reply(True, "nothing to compact yet (the window isn't long enough to be worth summarizing).")


def _reset(agent, session, arg: str) -> Reply:
    session.context.messages.clear()
    session.ticks = 0
    session.wi_sticky.clear()
    agent._freeze_memory()
    agent._freeze_skills()
    agent._invalidate_stable_prefix()
    # New transcript epoch: old history stays on disk, no longer reloaded.
    agent.transcript.reset()
    # Re-seed the card's opening line into the fresh epoch, exactly as a first
    # wake does. /reset is what creates the empty epoch, so it must re-emit
    # first_mes HERE — before any self-work cycle can write to the new epoch —
    # otherwise a live chara that self-works before the human reopens would
    # suppress the greeting (attach keys on an empty epoch, and self-work rows
    # under a tool-using chara are indistinguishable kind='chat'). Persisting it
    # now makes the greeting the epoch's first row, so it rides `restored` on
    # reopen with no double-show.
    greeting = (agent.greeting() or "").strip()
    if greeting:
        session.context.add("assistant", greeting)
    return Reply(True, "session context zeroed (new transcript epoch). durable memory remains.")


def _polaris(agent, session, arg: str) -> Reply:
    """View or set the chara's aspiration — its single lifelong ideal. This is the
    USER's to author; the chara can never change or complete it. `/aspiration` shows
    it; `/aspiration <text>` sets it; `/aspiration clear` removes it. (Internal
    codename: polaris — the data key + store keep that name.)"""
    want = arg.strip()
    if not want:
        cur = agent.polaris.get()
        body = (f"理想 Aspiration:\n  {cur}" if cur
                else "(no aspiration set)\n\n/aspiration <text>   set the chara's lifelong ideal")
        return Reply(True, body, {"polaris": cur}, verbose=True)
    if want.lower() == "clear":
        agent.polaris.set("")
        return Reply(True, "Aspiration cleared", {"polaris": ""})
    cur = agent.polaris.set(want)
    return Reply(True, "Aspiration set — it now quietly orients every turn", {"polaris": cur})


def _skills(agent, session, arg: str) -> Reply:
    skills = agent.skills.scan()
    if not skills:
        body = "(no skills yet)\n\nThe chara writes its own with create_skill;\nyou can drop SKILL.md dirs into ~/.lunamoth/skills/."
        return Reply(True, body, (), verbose=True)
    else:
        tag = {"own": "✎", "user": "⌂", "bundled": "·"}
        body = "\n".join(
            f"{tag.get(sk['origin'], '?')} {sk['name']} — {sk['description']}" for sk in skills
        ) + "\n\n✎ the chara's own  ⌂ ~/.lunamoth/skills  · bundled"
    return Reply(True, body, tuple(skills), verbose=True)


def _mcp(agent, session, arg: str) -> Reply:
    servers = agent.mcp.servers
    if not servers:
        body = (
            "(no MCP servers configured)\n\nAdd mcp.json next to the chara's config\n"
            "or the project root — Claude Code format:\n"
            '{"mcpServers": {"fetch": {"command": "uvx",\n  "args": ["mcp-server-fetch"]}}}\n\n'
            "Note: MCP servers run OUTSIDE the sandbox\njail — configuring one is a trust decision."
        )
        return Reply(True, body, (), verbose=True)
    allowed = set(agent.tools.mcp_allowed)
    lines = [
        f"{'●' if name in allowed else '○ (not in this tool pack)'} {name} — {servers[name].get('command', '?')}"
        for name in sorted(servers)
    ]
    return Reply(True, "\n".join(lines) + "\n\nTools appear to the chara as mcp__<server>__<tool>.",
                 tuple(sorted(servers)), verbose=True)


def _net(agent, session, arg: str) -> Reply:
    want = arg.strip().lower()
    if want in {"on", "off"}:
        agent.state.set_network(want == "on")
        return Reply(True, f"network access = {want.upper()} (terminal tool, this session)", {"net": want == "on"})
    cur = agent.state.load().get("network_access", False)
    return Reply(True, f"network access = {'ON' if cur else 'OFF'}  (usage: /net on|off)", {"net": bool(cur)})


def _allow_dir(agent, session, arg: str) -> Reply:
    if arg.strip():
        from pathlib import Path

        p = str(Path(arg.strip()).expanduser().resolve())
        agent.state.add_writable_path(p)
        return Reply(True, f"writable path added (sandbox): {p}")
    paths = agent.state.load().get("writable_paths", [])
    return Reply(True, "writable paths: " + (", ".join(paths) or "(workspace only)"))


def _mode(agent, session, arg: str) -> Reply:
    known = {"live", "chat", "on", "off", "auto", "always"}  # incl. pre-rename spellings
    want = arg.strip().lower()
    if want in known:
        mode = normalize_mode(want)
        _persist(agent, mode=mode)
        return Reply(True, f"mode = {mode} (persisted for this chara)", {"mode": mode})
    return Reply(True,
                 f"mode = {agent.settings.mode}  (usage: /mode live|chat — live: it keeps creating "
                 "while you watch; chat: it waits and only replies to you)",
                 {"mode": agent.settings.mode})


def _quiet(agent, session, arg: str) -> Reply:
    want = arg.strip()
    if want:
        try:
            seconds = max(0, int(float(want) * 60) if "." in want else int(want))
        except ValueError:
            return Reply(False, "usage: /quiet <seconds> — how long after your last word it resumes its own work")
        _persist(agent, quiet=seconds)
        return Reply(True, f"quiet period = {seconds}s (persisted — it resumes its own life after this much silence)",
                     {"quiet": seconds})
    return Reply(True,
                 f"quiet period = {agent.settings.quiet}s  (usage: /quiet <seconds> — while you talk it sets "
                 "its work aside; after this much silence it picks its life back up)",
                 {"quiet": agent.settings.quiet})


def _patience(agent, session, arg: str) -> Reply:
    want = arg.strip()
    if want:
        patience = parse_patience(want)
        if patience is None:
            return Reply(False, "usage: /patience <seconds> — base seconds between spontaneous cycles")
        _persist(agent, patience=patience, patience_override=True)
        return Reply(
            True,
            f"patience = {patience:g}s (persisted — base pause between spontaneous cycles)",
            {"patience": patience},
        )
    # Source of truth: agent.patience_resolved() owns the operator>card>default
    # precedence (the default + explicit-source rule live in knobs) — never
    # re-derive the source bit here.
    if hasattr(agent, "patience_resolved"):
        cur, source = agent.patience_resolved()
    else:
        cur, source = DEFAULT_PATIENCE, "default"
    return Reply(
        True,
        f"patience = {cur:g}s ({source})  (usage: /patience <seconds>)",
        {"patience": cur},
    )


def _thinking(agent, session, arg: str) -> Reply:
    want = arg.strip().lower()
    if want in {"on", "off"}:
        show = want == "on"
        _persist(agent, show_thinking=show)
        return Reply(True,
                     f"thinking text = {'shown dimmed' if show else 'hidden (✶ indicator only)'} (persisted)",
                     {"show_thinking": show})
    return Reply(True,
                 f"thinking text = {'shown' if agent.settings.show_thinking else 'hidden'}  "
                 "(usage: /thinking on|off — the ✶ status indicator always runs; /reasoning sets effort)",
                 {"show_thinking": bool(agent.settings.show_thinking)})


def _reasoning(agent, session, arg: str) -> Reply:
    want = arg.strip().lower()
    if want in {"off", "low", "medium", "high"}:
        _persist(agent, reasoning=want)
        agent.reconfigure(agent.settings)
        return Reply(True, f"reasoning = {want} (persisted)", {"reasoning": want})
    cur = agent.settings.reasoning or "medium"
    sup = "yes" if agent.llm.reasoning_supported() else "no (this model/route ignores it)"
    return Reply(True,
                 f"reasoning = {cur} · model supports the param: {sup}  "
                 "(usage: /reasoning off|low|medium|high)",
                 {"reasoning": cur})


def _steps(agent, session, arg: str) -> Reply:
    want = arg.strip()
    if want:
        try:
            n = max(1, int(want))
        except ValueError:
            return Reply(False, "usage: /steps <n> — max tool-call iterations per turn (e.g. 80)")
        _persist(agent, max_tool_steps=n)
        return Reply(True, f"tool steps = {n} per turn (persisted)", {"max_tool_steps": n})
    cur = getattr(agent.settings, "max_tool_steps", 80)
    return Reply(True, f"tool steps = {cur} per turn  (usage: /steps <n>)", {"max_tool_steps": cur})


def _model(agent, session, arg: str) -> Reply:
    want = arg.strip()
    if want:
        agent.swap_model(want)
        # Persist to THIS chara's session config so the choice survives a child
        # restart (board off→on, crash respawn, daemon restart, reboot). Only the
        # model id changes — the provider/key stay the same — so within a session
        # the route is steady and the prompt cache holds; the GLOBAL default is
        # untouched. (reasoning persists the same way.)
        _persist(agent, model=agent.settings.model)
        return Reply(True,
                     f"model = {agent.settings.model} (saved for this chara)",
                     {"model": agent.settings.model, "context_max": agent.context_limit()})
    return Reply(True,
                 f"model = {agent.settings.model}  (usage: /model <id> — saved for this chara)",
                 {"model": agent.settings.model, "context_max": agent.context_limit()})


def _provider(agent, session, arg: str) -> Reply:
    from ..session.settings import resolve_named_key

    label = arg.strip()
    if not label:
        return Reply(True,
                     f"provider = {agent.settings.provider or '—'} · {agent.settings.base_url or '—'}  "
                     "(usage: /provider <key-label> — switch this chara to a saved provider key)",
                     {"provider": agent.settings.provider, "base_url": agent.settings.base_url})
    entry = resolve_named_key(label)
    if not entry:
        return Reply(False, f"no such provider key: {label} (add it in Settings · Providers)")
    # Switch live (rebuilds the client), then persist provider/base_url/model to
    # THIS chara's session config — the api_key is resolved from the global keyring
    # and never written to the session (SEC-2). The key carries its own default
    # model; adopt it so the chara lands on a model the provider actually serves.
    agent.swap_provider(provider=entry["provider"], base_url=entry["base_url"],
                        api_key=entry["api_key"], model=entry.get("model") or None)
    _persist(agent, provider=agent.settings.provider, base_url=agent.settings.base_url,
             model=agent.settings.model)
    return Reply(True,
                 f"provider = {agent.settings.provider} · model = {agent.settings.model} (saved for this chara)",
                 {"provider": agent.settings.provider, "base_url": agent.settings.base_url,
                  "model": agent.settings.model, "context_max": agent.context_limit()})


def _help(agent, session, arg: str) -> Reply:
    lines = [f"{c.info.usage:<34} {c.info.help}" for c in _REGISTRY.values()]
    return Reply(True, "\n".join(lines), verbose=True)


# ---- registry ------------------------------------------------------------------------

def _cmd(name: str, usage: str, help_text: str, run) -> "tuple[str, Command]":
    return name, Command(CommandInfo(name, usage, help_text), run)


_REGISTRY: dict[str, Command] = dict([
    _cmd("status", "/status", "environment + context size", _status),
    _cmd("memory", "/memory", "the durable memory document", _memory),
    _cmd("memory_path", "/memory_path", "where the memory lives on disk", _memory_path),
    _cmd("files", "/files", "sandbox file listing", _files),
    _cmd("workspace", "/workspace", "workspace file listing", _workspace),
    _cmd("read", "/read <file>", "read a sandbox file", _read),
    _cmd("wread", "/wread <file>", "read a workspace file", _wread),
    _cmd("write", "/write <file> <text>", "write a sandbox file", _write),
    _cmd("logs", "/logs", "recent audit events", _logs),
    _cmd("aspiration", "/aspiration [text | clear]", "the chara's lifelong ideal (yours to set)", _polaris),
    _cmd("skills", "/skills", "skill index (the chara writes its own)", _skills),
    _cmd("mcp", "/mcp", "configured MCP tool servers", _mcp),
    _cmd("net", "/net on|off", "terminal network access", _net),
    _cmd("allow-dir", "/allow-dir <path>", "extra writable path (sandbox)", _allow_dir),
    _cmd("mode", "/mode live|chat", "live: keeps creating while you watch; chat: replies only", _mode),
    _cmd("quiet", "/quiet <seconds>", "silence before it resumes its own work (default 300)", _quiet),
    _cmd("patience", "/patience <seconds>", "base seconds between spontaneous cycles", _patience),
    _cmd("thinking", "/thinking on|off", "show the thinking text (default: ✶ indicator only)", _thinking),
    _cmd("reasoning", "/reasoning off|low|medium|high", "reasoning effort (default medium)", _reasoning),
    _cmd("model", "/model <id>", "session-scoped model hot-swap (empty: show current)", _model),
    _cmd("provider", "/provider <label>", "switch this chara to a saved provider key (empty: show current)", _provider),
    _cmd("steps", "/steps <n>", "max tool-call iterations per turn (default 80)", _steps),
    _cmd("compact", "/compact", "fold older turns into a summary now", _compact),
    _cmd("reset", "/reset", "zero session context (new transcript epoch)", _reset),
    _cmd("help", "/help", "this list", _help),
])

_ALIASES = {"presence": "mode", "skill": "skills",
            "polaris": "aspiration", "goal": "aspiration", "wish": "aspiration"}

# Pre-rename muscle memory, whole-line spellings (every frontend gets them).
_LINE_ALIASES = {
    "forever": "mode chat", "forever off": "mode chat", "pause": "mode chat",
    "forever on": "mode live", "resume": "mode live",
}


def infos() -> "tuple[CommandInfo, ...]":
    return tuple(c.info for c in _REGISTRY.values())


def execute(agent, session, line: str) -> Reply:
    spelled = line.strip().lstrip("/").lower()
    if spelled in _LINE_ALIASES:
        line = "/" + _LINE_ALIASES[spelled]
    parts = line.strip().split(maxsplit=1)
    if not parts:
        return Reply(False, "empty command")
    name = parts[0].lstrip("/").lower()
    name = _ALIASES.get(name, name)
    arg = parts[1] if len(parts) > 1 else ""
    cmd = _REGISTRY.get(name)
    if cmd is None:
        return Reply(False, "unknown command. try /help")
    try:
        return cmd.run(agent, session, arg)
    except Exception as e:  # surface to the operator, never crash the frontend
        agent.audit.write("command_error", command=line[:200], error=str(e))
        return Reply(False, f"command failed: {e}")
