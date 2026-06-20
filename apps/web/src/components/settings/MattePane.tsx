import { useCallback, useEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { fmtSize } from "../../lib/format";
import { deckToast } from "../ui/deckToast";

/* Background-removal (抠像 → 背景去除) local models — an embeddable section shown
   at the BOTTOM of the Model pane. Each model has a one-click Install button that
   does EVERYTHING: it installs the matting engine (rembg/onnxruntime) the first
   time if needed, then downloads the weights — with a live progress bar (% +
   bytes, advanced by polling matte.status). No separate "install deps" step, no
   manual uv. The larger model is recommended; the Lite one is fine too. */

interface MatteProgress { state?: string; done?: number; total?: number; error?: string }
interface MatteModel { id: string; label: string; note: string; size: number; installed: boolean; active: boolean; progress?: MatteProgress | null }
interface MatteStatus { deps: boolean; home: string; active: string; models: MatteModel[] }

// states where the install is still working (keep polling + show busy/progress)
const ACTIVE_STATES = ["preparing", "installing_deps", "downloading"];
const inFlight = (m: MatteModel) => !!m.progress && ACTIVE_STATES.includes(m.progress.state || "");

export function MatteSection() {
  const t = useT();
  const { hub } = useHubApi();
  const [st, setSt] = useState<MatteStatus | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const poll = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try { const s = await hub.call<MatteStatus>("matte.status", {}, 15000); setSt(s); return s; }
    catch { return null; }
  }, [hub]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const s = await refresh();
      if (!alive) return;
      if (s && s.models.some(inFlight)) poll.current = setTimeout(tick, 1500);
    };
    void tick();
    return () => { alive = false; if (poll.current) clearTimeout(poll.current); };
  }, [refresh]);

  const setBusyId = (id: string, on: boolean) =>
    setBusy((prev) => { const next = new Set(prev); on ? next.add(id) : next.delete(id); return next; });

  const act = async (id: string, method: string) => {
    if (busy.has(id)) return;
    setBusyId(id, true);
    try {
      const s = await hub.call<MatteStatus>(method, { model: id }, 20000);
      setSt(s);
      if (s.models.some(inFlight) && !poll.current) poll.current = setTimeout(() => void refresh(), 1500);
    } catch (e) { deckToast(rpcErrText(t, e as { message?: string }), true); }
    finally { setBusyId(id, false); }
  };

  // the larger model is the recommended one
  const recId = (st?.models || []).reduce<string>((best, m) => (!best || m.size > (st!.models.find((x) => x.id === best)?.size || 0) ? m.id : best), "");

  return (
    <div className="aux-sec matte-sec">
      <h3 className="aux-title">{t("matte-title")}</h3>
      <div className="aux-subline">{t("matte-sub")}</div>

      {(st?.models || []).map((m) => {
        const prog = m.progress || {};
        const preparing = prog.state === "preparing" || prog.state === "installing_deps";
        const downloading = prog.state === "downloading";
        const failed = prog.state === "error";
        const working = preparing || downloading;
        const isBusy = busy.has(m.id);
        const pct = prog.total ? Math.floor(((prog.done || 0) / prog.total) * 100) : 0;
        return (
          <div className={"aux-row matte-row" + (m.active ? " active" : "")} key={m.id}>
            <div className="aux-main">
              <div className="aux-head">
                <b>{m.label}</b>
                {m.id === recId && <span className="prov-badge">{t("model-recommended")}</span>}
                <span className="aux-desc">{m.note} · {fmtSize(m.size)}</span>
              </div>
              {preparing ? (
                <div className="matte-prog"><span className="lm-thinking">{t("matte-deps-installing")}</span></div>
              ) : downloading ? (
                <div className="matte-prog">
                  <div className="matte-bar"><div className="matte-fill" style={{ width: `${pct}%` }} /></div>
                  <span className="muted small">{t("matte-downloading")} {pct}% · {fmtSize(prog.done || 0)} / {fmtSize(prog.total || 0)}</span>
                </div>
              ) : failed ? (
                <div className="okline bad small">{prog.error || t("matte-dl-failed")}</div>
              ) : (
                <div className="aux-cur">{m.installed ? (m.active ? t("keys-active") : t("matte-installed")) : t("aux-auto")}</div>
              )}
            </div>
            <div className="aux-acts">
              {!m.installed ? (
                <button className="btn soft sm" disabled={isBusy || working} onClick={() => void act(m.id, "matte.download")}>
                  {working ? <span className="spin" /> : t("matte-install")}
                </button>
              ) : (
                <>
                  {!m.active && <button className="btn text sm" disabled={isBusy} onClick={() => void act(m.id, "matte.use")}>{t("prov-use")}</button>}
                  <button className="btn text sm" disabled={isBusy} onClick={() => void act(m.id, "matte.delete")}>{isBusy ? <span className="spin" /> : "✕"}</button>
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
