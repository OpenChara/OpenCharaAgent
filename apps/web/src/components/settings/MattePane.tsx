import { useCallback, useEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import { useHub } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { fmtSize } from "../../lib/format";
import { deckToast } from "../ui/deckToast";

/* R11 — the local matte (抠像 / background-removal) model manager, ported from
   the deleted front/web/app.js renderMatte. Lists every model with its install /
   active / in-progress state and offers download (with a live progress bar that
   advances by polling matte.status) / make-default / delete. When the optional
   `visuals` extra isn't installed, shows the install hint instead. */

interface MatteProgress {
  state?: string; // downloading | done | error
  done?: number; // bytes fetched
  total?: number; // total bytes (the model size)
  error?: string;
}
interface MatteModel {
  id: string;
  label: string;
  note: string;
  size: number; // exact byte size (matte.py MatteModel.size)
  installed: boolean;
  active: boolean;
  progress?: MatteProgress | null;
}
interface MatteStatus {
  deps: boolean;
  home: string;
  active: string;
  models: MatteModel[];
}

export function MattePane() {
  const t = useT();
  const { hub } = useHub();
  const [st, setSt] = useState<MatteStatus | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const poll = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await hub.call<MatteStatus>("matte.status", {}, 15000);
      setSt(s);
      return s;
    } catch {
      return null;
    }
  }, [hub]);

  // Poll while any model is downloading so the bar advances on its own.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const s = await refresh();
      if (!alive) return;
      const downloading = !!s && s.models.some((m) => m.progress && m.progress.state === "downloading");
      if (downloading) poll.current = setTimeout(tick, 1500);
    };
    void tick();
    return () => {
      alive = false;
      if (poll.current) clearTimeout(poll.current);
    };
  }, [refresh]);

  const setBusyId = (id: string, on: boolean) =>
    setBusy((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const act = async (id: string, method: string) => {
    if (busy.has(id)) return;
    setBusyId(id, true);
    try {
      const s = await hub.call<MatteStatus>(method, { model: id }, 20000);
      setSt(s);
      // a started download flips a model to "downloading" → resume polling
      if (s.models.some((m) => m.progress && m.progress.state === "downloading") && !poll.current) {
        poll.current = setTimeout(() => void refresh(), 1500);
      }
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setBusyId(id, false);
    }
  };

  return (
    <div className="settings-pane on">
      <h2>{t("matte-title")}</h2>
      <div className="sub">{t("matte-sub")}</div>

      {st && !st.deps && (
        <div className="matte-deps">
          <span>{t("matte-no-deps")} </span>
          <code>uv sync --extra visuals</code>
        </div>
      )}

      {st?.models.map((m) => {
        const prog = m.progress || {};
        const downloading = prog.state === "downloading";
        const failed = prog.state === "error";
        const isBusy = busy.has(m.id);
        // The backend reports {done,total} bytes, not a percent — derive it.
        const pct = prog.total ? Math.floor(((prog.done || 0) / prog.total) * 100) : 0;
        return (
          <div className={"matte-row set-row" + (m.active ? " on" : "")} key={m.id}>
            <div className="lbl">
              <span>{m.label}</span>
              <small>
                {m.note} · {fmtSize(m.size)}
              </small>
              {downloading && (
                <div className="matte-prog">
                  <div className="matte-bar">
                    <div className="matte-fill" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="muted small">
                    {t("matte-downloading")} {pct}% · {fmtSize(prog.done || 0)} / {fmtSize(prog.total || 0)}
                  </span>
                </div>
              )}
              {failed && <div className="okline bad small">{prog.error || t("matte-dl-failed")}</div>}
              {m.installed && !downloading && !failed && (
                <div className="muted small">{t("matte-installed")}</div>
              )}
            </div>
            <div className="matte-acts">
              {st && !st.deps ? null : !m.installed ? (
                <button
                  className="btn soft sm"
                  disabled={isBusy || downloading}
                  onClick={() => void act(m.id, "matte.download")}
                >
                  {downloading ? <span className="spin" /> : t("matte-download")}
                </button>
              ) : (
                <>
                  {!m.active && (
                    <button className="btn sm" disabled={isBusy} onClick={() => void act(m.id, "matte.use")}>
                      {t("keys-use")}
                    </button>
                  )}
                  {m.active && <span className="chip">{t("keys-active")}</span>}
                  <button
                    className="btn soft sm"
                    disabled={isBusy}
                    onClick={() => void act(m.id, "matte.delete")}
                  >
                    {isBusy ? <span className="spin" /> : "✕"}
                  </button>
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
