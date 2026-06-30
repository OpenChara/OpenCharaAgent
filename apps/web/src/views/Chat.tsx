/* The Chat view — the largest Track C piece. Connects/attaches a CharaClient via
 * useCharaStream, renders the streaming message list (the accumulator's items),
 * the composer (send/interrupt/attachments), the chat-head with the live status
 * word, the works + terminal sub-pages, and the right panel (status / skills /
 * wishes / memory / gateway / settings tabs).
 *
 * Faithful to index.html #view-chat + chat.js ChatController. Idle is server-side
 * only — this view never drives idle.
 */

import { Fragment, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useT, useLang } from "../i18n";
import { useNavigate, type ChatSub } from "../hooks/useHashRoute";
import { isMobileViewport } from "../hooks/useIsMobile";
import { useHubApi, useHubState, type BoardSession } from "../state/hub";
import { chatTimeLabel, currentTimezone, glyphOf, paletteClass } from "../lib/format";
import { assetUrl } from "../rpc";
import { readVisualPrefs } from "../lib/visual";
import { useCharaStream, type Snapshot } from "../hooks/useCharaStream";
import { StreamItemView, TimeSeparator } from "../components/chat/StreamItems";
import { Composer } from "../components/chat/Composer";
import { ChatPanel } from "../components/chat/ChatPanel";
import { ChatWorks } from "../components/chat/ChatWorks";
import { ChatTerminal } from "../components/chat/ChatTerminal";
import Homepage from "../components/chat/Homepage";

export function Chat({ name, sub }: { name: string; sub: ChatSub }) {
  const t = useT();
  const nav = useNavigate();
  const stream = useCharaStream(name);
  const [panelOpen, setPanelOpen] = useState(() => {
    // On a phone the panel is a full-screen page over the chat, so it starts CLOSED
    // (the conversation is the primary surface). On desktop it's the persistent side
    // column — restore the saved preference (default open).
    if (isMobileViewport()) return false;
    try {
      return localStorage.getItem("lm-panel-open") !== "0";
    } catch {
      return true;
    }
  });

  const snap = stream.snapshot as Snapshot | null;
  const netOff = !!snap && snap.net_on === false;
  const sandboxRoot = snap ? String(snap.sandbox_root || snap.workspace_root || "") : undefined;
  const lifeAttr = stream.streaming ? "working" : stream.resting ? "resting" : "";
  const avatarUri = snap?.avatar_uri ? String(snap.avatar_uri) : "";
  // The header dot mirrors the board: green (breathing) when autonomy is ON, grey
  // when OFF — read from the SAME roster `paused` the board uses, so inner and
  // outer are always consistent. Grey too when the socket is down.
  const { snapshot: hubSnap } = useHubState();
  const rosterEntry = (hubSnap?.sessions as BoardSession[] | undefined)?.find((s) => s.name === name);
  const autonomyOn = rosterEntry ? !rosterEntry.paused : true;
  const headDotOff = !stream.connected || !autonomyOn;

  const togglePanel = () => {
    setPanelOpen((open) => {
      const next = !open;
      // Persist only on desktop — on a phone the panel is a transient full-screen page,
      // not a layout preference (persisting "open" would cover the chat on next load).
      if (!isMobileViewport()) {
        try {
          localStorage.setItem("lm-panel-open", next ? "1" : "0");
        } catch {
          /* ok */
        }
      }
      return next;
    });
  };
  // Tapping the chara avatar opens the profile panel (the standard messaging pattern;
  return (
    <div className="view active" id="view-chat">
      <div className="chat-root" id="chat-root" data-life={lifeAttr}>
        <div className="chat-col">
          <div className="chat-head">
            <button className="back" onClick={() => nav("#/")} aria-label={t("back")}>
              ‹
            </button>
            <div className={`avatar-s ${avatarUri ? "" : paletteClass(stream.charName)}`}>
              {avatarUri ? (
                <img src={avatarUri} alt="" loading="lazy" decoding="async" />
              ) : (
                <span className="glyph-txt">{glyphOf(stream.charName)}</span>
              )}
              <span
                className={`mini-dot${headDotOff ? " off" : ""}`}
                title={autonomyOn ? t("p-autonomy") : t("st-paused")}
              />
            </div>
            <div className="who">
              <b>{stream.charName}</b>
            </div>
            <div className="chat-tabs" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={sub === "chat"}
                className={sub === "chat" ? "on" : ""}
                onClick={() => nav(`#/chara/${encodeURIComponent(name)}`)}
              >
                {t("tab-chat")}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={sub === "works"}
                className={sub === "works" ? "on" : ""}
                onClick={() => nav(`#/chara/${encodeURIComponent(name)}/works`)}
              >
                {t("tab-works")}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={sub === "term"}
                className={sub === "term" ? "on" : ""}
                onClick={() => nav(`#/chara/${encodeURIComponent(name)}/term`)}
              >
                {t("tab-terminal")}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={sub === "home"}
                className={sub === "home" ? "on" : ""}
                onClick={() => nav(`#/chara/${encodeURIComponent(name)}/home`)}
              >
                {t("tab-home")}
              </button>
            </div>
            <div className="grow" />
            {netOff && (
              <button
                className="icon-btn warn"
                title={t("net-off-tip")}
                onClick={() => void stream.runCommand("/net on")}
              >
                ⚠︎⌁
              </button>
            )}
            <button
              className={`icon-btn chat-panel-btn${panelOpen ? " on" : ""}`}
              onClick={togglePanel}
              aria-label={t("p-profile")}
            >
              ☰
            </button>
          </div>

          <div className="chat-pages">
            {sub === "chat" && <ChatStreamPage stream={stream} name={name} avatarUri={avatarUri} snap={snap} />}
            {sub === "works" && <ChatWorks name={name} sandboxRoot={sandboxRoot} />}
            {sub === "term" && <ChatTerminal name={name} sandboxRoot={sandboxRoot} />}
            {sub === "home" && <Homepage name={name} />}
          </div>
        </div>
        {panelOpen && (
          <>
            <div className="vsplit" />
            <ChatPanel stream={stream} name={name} onClose={() => setPanelOpen(false)} />
          </>
        )}
      </div>
    </div>
  );
}

/* The chat stream page — the message list + work-status slot + composer. Auto-
   scrolls to the bottom on new items unless the operator scrolled up (chat.js
   scrollDown's near-bottom guard). */
function ChatStreamPage({
  stream,
  name,
  avatarUri,
  snap,
}: {
  stream: ReturnType<typeof useCharaStream>;
  name: string;
  avatarUri: string;
  snap: Snapshot | null;
}) {
  const t = useT();
  const { lang } = useLang();
  const { hub, refresh } = useHubApi();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [superReadTs, setSuperReadTs] = useState(0);

  // Technical display mode (Settings · 显示 = Technical) surfaces raw tool-call
  // previews (e.g. a generate_image prompt). lm-display is the persisted source of
  // truth (the body.technical class is only set while Settings is mounted, so read
  // localStorage to survive a fresh load); re-read on a live toggle (Settings flips
  // the body class) or a cross-tab change. Previously nothing passed `technical`
  // down at all, so the setting had no effect on the stream.
  const readTechnical = () => {
    try { return localStorage.getItem("lm-display") === "technical"; } catch { return false; }
  };
  const [technical, setTechnical] = useState(readTechnical);
  useEffect(() => {
    const sync = () => setTechnical(readTechnical());
    const obs = new MutationObserver(sync);
    obs.observe(document.body, { attributes: true, attributeFilter: ["class"] });
    window.addEventListener("storage", sync);
    return () => { obs.disconnect(); window.removeEventListener("storage", sync); };
  }, []);

  // The chara's living backdrop: a low-opacity background image with a readability
  // veil, plus the sprite (立绘). Operator presentation prefs (on/off, opacities,
  // sprite position) are read per-chara from localStorage; the snapshot supplies
  // the URLs (bg_url / sprite_url|keyvisual_url need the auth token via assetUrl).
  const sandboxRoot = snap ? String(snap.sandbox_root || "") : "";
  const workspaceRoot = snap ? String(snap.workspace_root || snap.sandbox_root || "") : "";
  const prefs = readVisualPrefs(stream.charName);
  const bgUrl = prefs.bgOn && snap?.bg_url ? assetUrl(String(snap.bg_url)) : "";
  const spriteUrl =
    prefs.spritePos !== "off" ? assetUrl(String(snap?.sprite_url || snap?.keyvisual_url || "")) : "";
  // veilOpacity drives the readability wash (higher = more legible text); the bg
  // image stays at the CSS-default opacity and is dimmed by the veil on top of it.
  const visualVars = {
    "--chat-veil-opacity": String(prefs.veilOpacity / 100),
    "--chat-sprite-opacity": String(prefs.spriteOpacity / 100),
  } as CSSProperties;

  // Scroll behavior. Two distinct jobs that were wrongly collapsed into one
  // near-bottom guard (which is always false on first paint of a tall log, so
  // entry stayed pinned at the TOP and new replies landed below the fold):
  //   1. First load / chara switch → jump to the newest message UNCONDITIONALLY.
  //   2. Steady state → follow new items only when already near the bottom.
  const lastChara = useRef<string>("");
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    if (stream.ready && lastChara.current !== stream.charName) {
      lastChara.current = stream.charName;
      sc.scrollTop = sc.scrollHeight;
      // A second pass next frame catches late layout (avatars/images resizing).
      requestAnimationFrame(() => {
        sc.scrollTop = sc.scrollHeight;
      });
      return;
    }
    const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160;
    if (nearBottom) sc.scrollTop = sc.scrollHeight;
  }, [stream.items, stream.work, stream.ready, stream.charName]);

  // Opening the chat marks all current superchats READ ("点进去就是已读"): once
  // attached, set the watermark to now, adopt the returned read_ts for per-bubble
  // read styling, and refresh the roster so the board's unread mark clears at once.
  // Keyed by the SESSION name (the route prop) — NOT stream.charName (the card
  // display name), which resolves to a different value and targets the wrong session.
  useEffect(() => {
    if (!name || !stream.ready) return;
    let on = true;
    hub
      .call<{ read_ts?: number }>("superchat.read", { name, ts: Date.now() / 1000 }, 10000)
      .then((r) => {
        if (!on) return;
        setSuperReadTs(Number(r?.read_ts) || Date.now() / 1000);
        void refresh(); // board unread mark clears immediately
      })
      .catch(() => {});
    return () => {
      on = false;
    };
  }, [name, stream.ready, hub, refresh]);

  // Flush newly-seen super bubbles that arrive WHILE the chat is open (turn settled
  // + page visible), so they don't re-surface as unread on the board.
  useEffect(() => {
    if (stream.streaming || document.visibilityState !== "visible") return;
    const unread = stream.items
      .filter((it) => it.kind === "super" && it.ts !== undefined && it.ts > superReadTs)
      .map((it) => (it as { ts?: number }).ts || 0);
    if (!unread.length) return;
    const maxTs = Math.max(...unread);
    const timer = setTimeout(() => {
      hub
        .call<{ read_ts?: number }>("superchat.read", { name, ts: maxTs }, 10000)
        .then((r) => setSuperReadTs((prev) => Math.max(prev, Number(r?.read_ts) || maxTs)))
        .catch(() => {});
    }, 1600);
    return () => clearTimeout(timer);
  }, [stream.items, stream.streaming, name, superReadTs, hub]);

  const work = stream.work;
  const workText = work.active
    ? work.phase === "think"
      ? t("work-thinking", { n: work.thinkTokens })
      : work.phase === "tool"
        ? t("work-tool", { name: work.toolName || "tool" })
        : t("work-generating")
    : stream.statusWord;
  const workCls = work.active ? work.phase : "life";

  // WeChat-style time separators: a centered time line before a message when its gap
  // from the previous timestamped message exceeds 5 min (and always before the first).
  // Only user / say / super messages carry a time; tool & reasoning items don't.
  const tz = currentTimezone();
  const timeMarks = useMemo(() => {
    const SEP_GAP = 5 * 60; // seconds
    const marks = new Map<string, string>();
    const tsOf = (it: { kind: string; ts?: number }) =>
      it.kind === "user" || it.kind === "say" || it.kind === "super" ? it.ts : undefined;
    let prev = 0;
    for (const it of stream.items) {
      const ts = tsOf(it);
      if (ts === undefined) continue;
      if (prev === 0 || ts - prev >= SEP_GAP) marks.set(it.id, chatTimeLabel(t, lang, ts, tz));
      prev = ts;
    }
    return marks;
  }, [stream.items, lang, tz, t]);

  return (
    <div className="chat-page on" id="page-chat" style={visualVars}>
      {bgUrl && (
        <>
          <div
            className="chat-bg"
            style={{ backgroundImage: `url("${bgUrl.replace(/"/g, "%22")}")` }}
            aria-hidden="true"
          />
          <div className="chat-veil" aria-hidden="true" />
        </>
      )}
      {spriteUrl && (
        <div className={`chat-sprite pos-${prefs.spritePos}`} aria-hidden="true">
          <img src={spriteUrl} alt="" loading="lazy" decoding="async" />
        </div>
      )}
      <div className="stream" id="stream" ref={scrollRef}>
        <div className="stream-inner" id="stream-inner">
          <div className="chat-empty">
            <div className={`avatar-s ${avatarUri ? "" : paletteClass(stream.charName)}`}>
              {avatarUri ? (
                <img src={avatarUri} alt="" loading="lazy" decoding="async" />
              ) : (
                <span className="glyph-txt">{glyphOf(stream.charName)}</span>
              )}
            </div>
            <b>{stream.charName}</b>
            {/* The "it's alive" moment: after waking a chara with no opening +
                no history, don't leave a blank stream — invite the first word. */}
            {stream.ready && stream.items.length === 0 && (
              <div className="chat-empty-hint">{t("chat-say-hi", { name: stream.charName })}</div>
            )}
          </div>
          {stream.items.map((item) => (
            <Fragment key={item.id}>
              {timeMarks.has(item.id) && <TimeSeparator label={timeMarks.get(item.id)!} />}
              <StreamItemView
                item={item}
                charName={stream.charName}
                superReadTs={superReadTs}
                technical={technical}
                avatarUri={avatarUri}
                sandboxRoot={sandboxRoot}
                workspaceRoot={workspaceRoot}
                onPermission={stream.permissionReply}
                onClarify={stream.clarifyReply}
              />
            </Fragment>
          ))}
        </div>
      </div>
      <Composer
        charName={stream.charName}
        persistKey={name}
        streaming={stream.streaming}
        resting={stream.resting}
        statusSlot={workText ? <div className={`work-status ${workCls}`}>{workText}</div> : null}
        onSend={stream.send}
        onInterrupt={stream.interrupt}
        onForceStop={stream.forceStop}
        onCommand={(line) => void stream.runCommand(line)}
        onError={stream.note}
      />
    </div>
  );
}
