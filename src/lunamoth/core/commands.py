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

from ..content.knobs import (
    TEMPO_PRESETS,
    embodiment_copy,
    normalize_embodiment,
    parse_patience,
    parse_tempo,
    tempo_label,
)
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
    data = agent.tools.call("inspect_env")
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
    return _tool_json(agent, "list_workspace")


def _read(agent, session, arg: str) -> Reply:
    if not arg:
        return Reply(False, "usage: /read <filename>")
    return _tool_json(agent, "read_file", filename=arg.split()[0])


def _wread(agent, session, arg: str) -> Reply:
    if not arg:
        return Reply(False, "usage: /wread <filename>")
    return _tool_json(agent, "read_workspace_file", filename=arg.split()[0])


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
    session.thoughts.clear()
    session.ticks = 0
    session.wi_sticky.clear()
    agent._freeze_memory()
    agent._freeze_skills()
    agent._invalidate_stable_prefix()
    # New transcript epoch: old history stays on disk, no longer reloaded.
    agent.transcript.reset()
    return Reply(True, "session context zeroed (new transcript epoch). durable memory remains.")


def _goal(agent, session, arg: str) -> Reply:
    parts = arg.split(maxsplit=1)
    try:
        if not arg:
            goals = agent.goals.all()
            if not goals:
                body = "(no goals yet)\n\n/goal <text>      add a goal (yours show as ⭑)\n/goal done g3     mark done\n/goal drop g3     drop it"
            else:
                icon = {"active": "○", "done": "●", "dropped": "✕"}
                lines = [
                    f"{icon.get(g['status'], '?')} {g['id']}  {'⭑ ' if g.get('by') == 'operator' else ''}{g['text']}"
                    for g in goals
                ]
                body = "\n".join(lines) + "\n\n○ active  ● done  ✕ dropped\n/goal <text> · /goal done|drop <id>"
            return Reply(True, body, tuple(goals), verbose=True)
        if parts[0] in {"done", "drop", "active"} and len(parts) == 2:
            status = {"done": "done", "drop": "dropped", "active": "active"}[parts[0]]
            goal = agent.goals.set_status(parts[1].strip(), status)
            return Reply(True, f"goal {goal['id']} → {goal['status']}", goal)
        goal = agent.goals.add(arg, by="operator")
        return Reply(True, f"goal {goal['id']} added ⭑ — it now steers every turn", goal)
    except ValueError as e:
        return Reply(False, f"goal error: {e}")


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


def _tempo(agent, session, arg: str) -> Reply:
    want = arg.strip()
    if want:
        tempo = parse_tempo(want)
        if tempo is None:
            presets = "|".join(TEMPO_PRESETS)
            return Reply(False, f"usage: /tempo <{presets}|0.1..10> — chara time-flow rate")
        _persist(agent, tempo=tempo)
        return Reply(
            True,
            f"tempo = {tempo_label(tempo)} (persisted — spontaneous cycle pause = patience ÷ tempo)",
            {"tempo": tempo},
        )
    cur = agent.effective_tempo() if hasattr(agent, "effective_tempo") else 1.0
    source = "operator" if parse_tempo(getattr(agent.settings, "tempo", 0.0)) is not None else "card/default"
    presets = "|".join(TEMPO_PRESETS)
    return Reply(
        True,
        f"tempo = {tempo_label(cur)} ({source})  (usage: /tempo <{presets}|0.1..10>)",
        {"tempo": cur},
    )


def _patience(agent, session, arg: str) -> Reply:
    want = arg.strip()
    if want:
        patience = parse_patience(want)
        if patience is None:
            return Reply(False, "usage: /patience <seconds> — base seconds between spontaneous cycles")
        _persist(agent, patience=patience, patience_override=True)
        return Reply(
            True,
            f"patience = {patience:g}s (persisted — spontaneous cycle pause = patience ÷ tempo)",
            {"patience": patience},
        )
    cur = agent.effective_patience() if hasattr(agent, "effective_patience") else 600.0
    parsed = parse_patience(getattr(agent.settings, "patience", 600.0))
    explicit = bool(getattr(agent.settings, "patience_override", False))
    source = (
        "operator"
        if parsed is not None and (explicit or abs(parsed - 600.0) > 1e-9)
        else "card/default"
    )
    return Reply(
        True,
        f"patience = {cur:g}s ({source})  (usage: /patience <seconds>)",
        {"patience": cur},
    )


def _embodiment(agent, session, arg: str) -> Reply:
    want = normalize_embodiment(arg)
    usage = (
        "usage: /embodiment literal|actor\n"
        f"- {embodiment_copy('literal', 'en')}\n"
        f"- {embodiment_copy('actor', 'en')}\n"
        f"- {embodiment_copy('literal', 'zh')}\n"
        f"- {embodiment_copy('actor', 'zh')}"
    )
    if arg.strip():
        if not want:
            return Reply(False, usage)
        _persist(agent, embodiment_override=want)
        agent._invalidate_stable_prefix()
        return Reply(
            True,
            f"embodiment = {want} (persisted override; operator > card > literal)\n"
            f"{embodiment_copy(want, agent.lang)}",
            {"embodiment": want},
        )
    cur = agent.effective_embodiment() if hasattr(agent, "effective_embodiment") else "literal"
    return Reply(
        True,
        f"embodiment = {cur} (operator > card > literal)\n"
        f"{embodiment_copy(cur, agent.lang)}\n\n{usage}",
        {"embodiment": cur},
        verbose=True,
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
    _cmd("goal", "/goal [text | done <id> | drop <id>]", "the chara's goal list (⭑ = yours)", _goal),
    _cmd("skills", "/skills", "skill index (the chara writes its own)", _skills),
    _cmd("mcp", "/mcp", "configured MCP tool servers", _mcp),
    _cmd("net", "/net on|off", "terminal network access", _net),
    _cmd("allow-dir", "/allow-dir <path>", "extra writable path (sandbox)", _allow_dir),
    _cmd("mode", "/mode live|chat", "live: keeps creating while you watch; chat: replies only", _mode),
    _cmd("quiet", "/quiet <seconds>", "silence before it resumes its own work (default 300)", _quiet),
    _cmd("tempo", "/tempo <preset|0.1..10>", "chara time-flow rate (swift/steady/slow/glacial)", _tempo),
    _cmd("patience", "/patience <seconds>", "base seconds between spontaneous cycles", _patience),
    _cmd("embodiment", "/embodiment literal|actor", "how tools relate to the character's fiction", _embodiment),
    _cmd("thinking", "/thinking on|off", "show the thinking text (default: ✶ indicator only)", _thinking),
    _cmd("reasoning", "/reasoning off|low|medium|high", "reasoning effort (default medium)", _reasoning),
    _cmd("compact", "/compact", "fold older turns into a summary now", _compact),
    _cmd("reset", "/reset", "zero session context (new transcript epoch)", _reset),
    _cmd("help", "/help", "this list", _help),
])

_ALIASES = {"presence": "mode", "skill": "skills"}

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
