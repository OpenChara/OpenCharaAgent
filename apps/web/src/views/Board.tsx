import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";
import { useNavigate } from "../hooks/useHashRoute";
import { useHub, type BoardSession } from "../state/hub";
import { useOverlay } from "../state/overlay";
import { statusOf, rpcErrText } from "../lib/status";
import { modeLabel, paletteClass } from "../lib/format";
import { deckToast } from "../components/ui/deckToast";
import { CardFace } from "../components/deck/visual";
import { BrandLoader } from "../components/ui/BrandLoader";
import type { DeckCard } from "../components/deck/types";

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
  // Optimistic autonomy override per chara (name → desired on/off): the slider
  // flips at once, reverts on failure (binding "no dead clicks" rule).
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const sessions: BoardSession[] = snapshot?.sessions ?? [];
  // The art lives on each chara's FROZEN card (the locked deck entry owned by it),
  // not on the roster row — match by owner to show the avatar/sprite on the board.
  const cards = (snapshot?.cards as DeckCard[] | undefined) || [];

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

  // The board's on/off IS the chara's autonomy — the SAME switch as the in-chat
  // status toggle, calling the SAME RPC (chara.set_autonomy). It never kills the
  // chat you're in: on = mode live (autonomous), off = mode chat (replies only,
  // no idle token burn). Entering/leaving a chara has no effect on its context.
  const toggleAutonomy = async (s: BoardSession, next: boolean) => {
    if (s.name in pending) return; // a flip is already in flight
    setPending((p) => ({ ...p, [s.name]: next })); // optimistic flip
    try {
      await hub.call("chara.set_autonomy", { name: s.name, on: next }, 30000);
      await refresh();
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setPending((p) => {
        const n = { ...p };
        delete n[s.name];
        return n;
      });
    }
  };

  // 全部启动 / 全部关闭: flip EVERY chara's autonomy at once. All sliders slide
  // immediately (optimistic), then we fire one set_autonomy per chara.
  const [allBusy, setAllBusy] = useState(false);
  const setAllAutonomy = async (on: boolean) => {
    if (allBusy || sessions.length === 0) return;
    const names = sessions.map((s) => s.name);
    setAllBusy(true);
    setPending((p) => {
      const n = { ...p };
      names.forEach((nm) => (n[nm] = on));
      return n;
    });
    try {
      await Promise.all(names.map((nm) => hub.call("chara.set_autonomy", { name: nm, on }, 30000)));
      await refresh();
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
      await refresh(); // partial success: re-sync so the succeeded charas don't revert
    } finally {
      setPending((p) => {
        const n = { ...p };
        names.forEach((nm) => delete n[nm]);
        return n;
      });
      setAllBusy(false);
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
        {sessions.length > 0 && (
          <>
            <button className="btn" disabled={allBusy} onClick={() => void setAllAutonomy(true)}>
              {t("board-start-all")}
            </button>
            <button className="btn" disabled={allBusy} onClick={() => void setAllAutonomy(false)}>
              {t("board-stop-all")}
            </button>
          </>
        )}
        <button className="btn primary" onClick={() => nav("#/deck")}>
          {t("new-chara")}
        </button>
      </div>

      <div className="board">
        {/* Until the FIRST hub.state lands, snapshot is null — that is "loading",
            NOT "no charas". Show the flit loader instead of the empty-state, so a
            new user no longer sees a blank board flash before the first-run
            overlay (which itself waits for snapshot) opens. */}
        {!snapshot ? (
          <BrandLoader />
        ) : (
          <>
            <div className="grid" id="board-grid">
          {sessions.map((s) => {
            const st = statusOf(t, s);
            // on/off == autonomy (!paused), with the optimistic override applied.
            const live = s.name in pending ? pending[s.name] : !s.paused;
            // The dot follows the optimistic state too (err still wins from st).
            const dot = st.dot === "err" ? "err" : s.name in pending ? (live ? "live" : "off") : st.dot;
            // Unread = superchats newer than the read watermark; cleared on entry.
            const unread = (s.superchat_unread || 0) > 0;
            return (
              <div
                key={s.name}
                className={`chara-card${!live && dot !== "err" ? " offline" : ""}`}
                role="button"
                tabIndex={0}
                aria-label={s.char_name}
                onClick={() => nav(`#/chara/${encodeURIComponent(s.name)}`)}
                onKeyDown={(e) => {
                  if (e.target !== e.currentTarget) return; // inner buttons handle their own keys
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    nav(`#/chara/${encodeURIComponent(s.name)}`);
                  }
                }}
              >
                <CardFace
                  card={cards.find((c) => c.locked && c.owner === s.name) ?? ({ name: s.char_name } as DeckCard)}
                  cls={`portrait ${paletteClass(s.char_name)}`}
                >
                  <span className={`dot ${dot}`} />
                  <button
                    className={`switch${live ? " on" : ""}`}
                    title={live ? t("act-sleep") : t("act-wake-up")}
                    style={{ position: "absolute", top: 12, right: 12, zIndex: 3 }}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      void toggleAutonomy(s, !live);
                    }}
                  />
                </CardFace>
                <div className="card-body">
                  <div className="card-name">
                    <b>{s.char_name}</b>
                    <div className="chips">
                      <span className="chip">{s.lang}</span>
                      <span className="chip">{modeLabel(t, s.paused ? "chat" : "live")}</span>
                    </div>
                  </div>
                  <div className={`status-line ${st.cls}${unread ? " unread" : ""}`}>
                    {unread && <span className="unread-dot" />}
                    {st.line}
                  </div>
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
          </>
        )}
      </div>
    </div>
  );
}
