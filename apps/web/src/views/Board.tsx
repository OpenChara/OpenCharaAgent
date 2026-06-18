import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";
import { useNavigate } from "../hooks/useHashRoute";
import { useHub, type BoardSession } from "../state/hub";
import { useOverlay } from "../state/overlay";
import { statusOf, rpcErrText } from "../lib/status";
import { modeLabel, paletteClass, glyphOf } from "../lib/format";
import { deckToast } from "../components/ui/deckToast";

/* Board — the roster of living charas. Faithful to index.html #view-board +
   app.js renderBoard: a grid of chara cards, each with a power toggle
   (optimistic: spins immediately, reverts on failure — the binding UI rule) and
   click-through to the chat. Deck-card avatars/sprites and the speaks-preview
   are wired in when the Deck/Chat tracks land; until then the palette+glyph
   fallback (app.js's no-card path) is used. */

const FIRST_RUN_SEEN = "lm-first-run-seen";

export function Board() {
  const t = useT();
  const nav = useNavigate();
  const overlay = useOverlay();
  const { hub, snapshot, refresh } = useHub();
  const [busy, setBusy] = useState<Set<string>>(new Set());

  const sessions: BoardSession[] = snapshot?.sessions ?? [];

  // First-run: once the hub snapshot has loaded and there are no charas, show the
  // welcome overlay a single time per browser (app.js openFirstRun on empty boot).
  const shown = useRef(false);
  useEffect(() => {
    if (shown.current || !snapshot) return;
    if (sessions.length > 0) return;
    let seen = false;
    try {
      seen = localStorage.getItem(FIRST_RUN_SEEN) === "1";
    } catch {
      /* private mode */
    }
    if (seen) return;
    shown.current = true;
    try {
      localStorage.setItem(FIRST_RUN_SEEN, "1");
    } catch {
      /* private mode */
    }
    overlay.open({ kind: "firstrun" });
  }, [snapshot, sessions.length, overlay]);

  const setBusyName = (name: string, on: boolean) =>
    setBusy((prev) => {
      const next = new Set(prev);
      if (on) next.add(name);
      else next.delete(name);
      return next;
    });

  const togglePower = async (s: BoardSession, live: boolean) => {
    if (busy.has(s.name)) return;
    setBusyName(s.name, true); // optimistic: button shows a spinner at once
    try {
      await hub.call(live ? "session.stop" : "session.start", { name: s.name }, 30000);
      await refresh();
    } catch (e) {
      // binding UI rule: surface the failure (the spinner clears in finally and
      // the card reverts to its real state on the next refresh).
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setBusyName(s.name, false);
    }
  };

  return (
    <div className="view active" id="view-board">
      <div className="toolbar">
        <h1>
          <span>{t("nav-charas")}</span>
          <span className="count">{sessions.length ? `· ${sessions.length}` : ""}</span>
        </h1>
        <div className="grow" />
        <button className="btn primary" onClick={() => nav("#/deck")}>
          {t("new-chara")}
        </button>
      </div>

      <div className="board">
        <div className="grid" id="board-grid">
          {sessions.map((s) => {
            const live = (s.status === "running" || s.status === "attached") && !s.paused;
            const st = statusOf(t, s);
            const isBusy = busy.has(s.name);
            return (
              <div
                key={s.name}
                className={`chara-card${st.dot === "off" ? " offline" : ""}`}
                onClick={() => nav(`#/chara/${encodeURIComponent(s.name)}`)}
              >
                <div className={`portrait ${paletteClass(s.char_name)}`}>
                  <div className="glyph">{glyphOf(s.char_name)}</div>
                  <span className={`dot ${st.dot}`} />
                  <div className="hover-acts">
                    <button
                      title={live ? t("act-sleep") : t("act-wake-up")}
                      disabled={isBusy}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        void togglePower(s, live);
                      }}
                    >
                      {isBusy ? <span className="spin" /> : "⏻"}
                    </button>
                  </div>
                </div>
                <div className="card-body">
                  <div className="card-name">
                    <b>{s.char_name}</b>
                    <div className="chips">
                      <span className="chip">{s.lang}</span>
                      <span className="chip">{modeLabel(t, s.mode)}</span>
                    </div>
                  </div>
                  <div className={`status-line ${st.cls}`}>{st.line}</div>
                </div>
              </div>
            );
          })}
        </div>

        {sessions.length === 0 && (
          <div className="empty-state" style={{ display: "flex" }}>
            <div className="empty-title">{t("empty-board")}</div>
            <div className="acts">
              <button className="btn primary" onClick={() => overlay.open({ kind: "firstrun" })}>
                {t("meet-luna")}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
