/* The Chat view — the largest Track C piece. Connects/attaches a CharaClient via
 * useCharaStream, renders the streaming message list (the accumulator's items),
 * the composer (send/interrupt/attachments), the chat-head with the live status
 * word, the works + terminal sub-pages, and the right panel (status / skills /
 * wishes / memory / gateway / settings tabs).
 *
 * Faithful to index.html #view-chat + chat.js ChatController. Idle is server-side
 * only — this view never drives idle.
 */

import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";
import { useNavigate, type ChatSub } from "../hooks/useHashRoute";
import { useHub } from "../state/hub";
import { glyphOf, paletteClass } from "../lib/format";
import { useCharaStream, type Snapshot } from "../hooks/useCharaStream";
import { StreamItemView } from "../components/chat/StreamItems";
import { Composer } from "../components/chat/Composer";
import { ChatPanel } from "../components/chat/ChatPanel";
import { ChatWorks } from "../components/chat/ChatWorks";
import { ChatTerminal } from "../components/chat/ChatTerminal";

export function Chat({ name, sub }: { name: string; sub: ChatSub }) {
  const t = useT();
  const nav = useNavigate();
  const stream = useCharaStream(name);
  const [panelOpen, setPanelOpen] = useState(() => {
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

  const togglePanel = () => {
    setPanelOpen((open) => {
      const next = !open;
      try {
        localStorage.setItem("lm-panel-open", next ? "1" : "0");
      } catch {
        /* ok */
      }
      return next;
    });
  };

  return (
    <div className="view active" id="view-chat">
      <div className="chat-root" id="chat-root" data-life={lifeAttr}>
        <div className="chat-col">
          <div className="chat-head">
            <button className="back" onClick={() => nav("#/")}>
              ‹
            </button>
            <div className={`avatar-s avatar-btn ${paletteClass(stream.charName)}`} title="">
              <span className="glyph-txt">{glyphOf(stream.charName)}</span>
              <span className={`mini-dot${stream.connected ? "" : " off"}`} />
            </div>
            <div className="who">
              <b>{stream.charName}</b>
            </div>
            <div className="chat-tabs">
              <span
                className={sub === "chat" ? "on" : ""}
                onClick={() => nav(`#/chara/${encodeURIComponent(name)}`)}
              >
                {t("tab-chat")}
              </span>
              <span
                className={sub === "works" ? "on" : ""}
                onClick={() => nav(`#/chara/${encodeURIComponent(name)}/works`)}
              >
                {t("tab-works")}
              </span>
              <span
                className={sub === "term" ? "on" : ""}
                onClick={() => nav(`#/chara/${encodeURIComponent(name)}/term`)}
              >
                {t("tab-terminal")}
              </span>
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
            <button className={`icon-btn${panelOpen ? " on" : ""}`} onClick={togglePanel}>
              ▤
            </button>
          </div>

          <div className="chat-pages">
            {sub === "chat" && <ChatStreamPage stream={stream} />}
            {sub === "works" && <ChatWorks name={name} sandboxRoot={sandboxRoot} />}
            {sub === "term" && <ChatTerminal name={name} sandboxRoot={sandboxRoot} />}
          </div>
        </div>
        {panelOpen && (
          <>
            <div className="vsplit" />
            <ChatPanel stream={stream} name={name} />
          </>
        )}
      </div>
    </div>
  );
}

/* The chat stream page — the message list + work-status slot + composer. Auto-
   scrolls to the bottom on new items unless the operator scrolled up (chat.js
   scrollDown's near-bottom guard). */
function ChatStreamPage({ stream }: { stream: ReturnType<typeof useCharaStream> }) {
  const t = useT();
  const { hub } = useHub();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [superReadTs, setSuperReadTs] = useState(0);
  const technical = false; // the "technical" display mode toggle (default off)

  // Auto-scroll to bottom on new items when already near the bottom.
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160;
    if (nearBottom) sc.scrollTop = sc.scrollHeight;
  }, [stream.items, stream.work]);

  // Read the persisted super-chat read watermark once, then flush newly-seen
  // super bubbles (chat.js flushSuperReads) when the turn settles + page visible.
  useEffect(() => {
    let on = true;
    hub
      .call<{ read_ts?: number }>("superchat.read", { name: stream.charName, ts: 0 }, 10000)
      .then((r) => on && setSuperReadTs(Number(r?.read_ts) || 0))
      .catch(() => {});
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (stream.streaming || document.visibilityState !== "visible") return;
    const unread = stream.items
      .filter((it) => it.kind === "super" && it.ts !== undefined && it.ts > superReadTs)
      .map((it) => (it as { ts?: number }).ts || 0);
    if (!unread.length) return;
    const maxTs = Math.max(...unread);
    const timer = setTimeout(() => {
      hub
        .call<{ read_ts?: number }>("superchat.read", { name: stream.charName, ts: maxTs }, 10000)
        .then((r) => setSuperReadTs((prev) => Math.max(prev, Number(r?.read_ts) || maxTs)))
        .catch(() => {});
    }, 1600);
    return () => clearTimeout(timer);
  }, [stream.items, stream.streaming, stream.charName, superReadTs, hub]);

  const work = stream.work;
  const workText = work.active
    ? work.phase === "think"
      ? t("work-thinking", { n: work.thinkTokens })
      : work.phase === "tool"
        ? t("work-tool", { name: work.toolName || "tool" })
        : t("work-generating")
    : stream.statusWord;
  const workCls = work.active ? work.phase : "life";

  return (
    <div className="chat-page on" id="page-chat">
      <div className="stream" id="stream" ref={scrollRef}>
        <div className="stream-inner" id="stream-inner">
          <div className="chat-empty">
            <div className={`avatar-s ${paletteClass(stream.charName)}`}>
              <span className="glyph-txt">{glyphOf(stream.charName)}</span>
            </div>
            <b>{stream.charName}</b>
          </div>
          {stream.items.map((item) => (
            <StreamItemView
              key={item.id}
              item={item}
              charName={stream.charName}
              superReadTs={superReadTs}
              technical={technical}
              onPermission={stream.permissionReply}
              onClarify={stream.clarifyReply}
            />
          ))}
        </div>
      </div>
      <Composer
        charName={stream.charName}
        streaming={stream.streaming}
        resting={stream.resting}
        statusSlot={workText ? <div className={`work-status ${workCls}`}>{workText}</div> : null}
        onSend={stream.send}
        onInterrupt={stream.interrupt}
        onCommand={(line) => void stream.runCommand(line)}
        onError={stream.note}
      />
    </div>
  );
}
