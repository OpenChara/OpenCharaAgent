/* eslint-disable react-refresh/only-export-components -- intentional co-location: this module exports a provider/component alongside its hooks or pure helpers, imported widely; splitting it purely for dev fast-refresh isn't worth the import churn. */
/* Settings · 关于 — software update + changelog, coupled to GitHub Releases (backend
 * server/hub/updates.py). We CHECK + surface the release notes and offer a one-click
 * in-place update, but never auto-update. The first-open nudge (useUpdateNudge, mounted
 * in App's Shell) shows a non-blocking toast once per version when one is available. */

import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useT } from "../../i18n";
import { useHub } from "../../state/hub";
import { navTo } from "../../hooks/useHashRoute";
import { deckToast, deckToastAction, deckWorkingToast } from "../ui/deckToast";
import { rpcErrText } from "../../lib/status";

interface Release {
  tag: string;
  name: string;
  body: string;
  published_at: string;
  url: string;
  prerelease: boolean;
}
export interface UpdateStatus {
  current: string;
  channel: string;
  latest: string;
  behind: number;
  update_available: boolean;
  releases: Release[];
  checked_at: number;
  manual_command?: string;
}

const errMsg = (e: unknown) => (e as { message?: string })?.message ?? "";

/** The first-open nudge: a non-blocking toast pointing to the changelog, shown at
 *  most once per available version (keyed in localStorage). Never auto-updates. */
export function useUpdateNudge() {
  const t = useT();
  const { hub } = useHub();
  useEffect(() => {
    let live = true;
    const id = window.setTimeout(() => {
      if (!hub.sock.open) return; // let the next launch try once connected
      hub
        .call<UpdateStatus>("update.status", {})
        .then((s) => {
          if (!live || !s.update_available) return;
          const tag = s.latest || `behind-${s.behind}`;
          let seen = "";
          try {
            seen = localStorage.getItem("lm-upd-seen") || "";
          } catch {
            /* private mode */
          }
          if (seen === tag) return;
          try {
            localStorage.setItem("lm-upd-seen", tag);
          } catch {
            /* private mode */
          }
          deckToastAction(t("upd-nudge"), t("upd-nudge-view"), () => navTo("#/settings/about"), 9000);
        })
        .catch(() => {});
    }, 2500); // after the shell settles + the hub connects
    return () => {
      live = false;
      window.clearTimeout(id);
    };
  }, [hub, t]);
}

export function UpdatePane() {
  const t = useT();
  const { hub } = useHub();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [applying, setApplying] = useState(false);
  const [done, setDone] = useState(false);
  // After a successful update: an auto-restart countdown the user can cancel, then a
  // "restarting…" state while the instance relaunches into the new code and the WS
  // (auto-)reconnects. null = no countdown running.
  const [restartIn, setRestartIn] = useState<number | null>(null);
  const [restarting, setRestarting] = useState(false);

  const triggerRestart = useCallback(() => {
    setRestartIn(null);
    setRestarting(true);
    // Fire-and-forget: the instance re-execs ~1s later, dropping this WS — the board
    // client auto-reconnects to the new process. A reject just means it already went.
    hub.call("update.restart", { delay: 1.0 }, 8000).catch(() => {});
  }, [hub]);

  // Tick the countdown; at 0, restart. Cancel = setRestartIn(null) stops it.
  useEffect(() => {
    if (restartIn === null) return;
    if (restartIn <= 0) {
      triggerRestart();
      return;
    }
    const id = window.setTimeout(() => setRestartIn((n) => (n === null ? null : n - 1)), 1000);
    return () => window.clearTimeout(id);
  }, [restartIn, triggerRestart]);

  const load = useCallback(
    (force: boolean) => {
      setChecking(true);
      hub
        .call<UpdateStatus>("update.status", { force })
        .then(setStatus)
        .catch((e) => deckToast(rpcErrText(t, { message: errMsg(e) }), true))
        .finally(() => setChecking(false));
    },
    [hub, t],
  );

  useEffect(() => {
    load(false); // cached status on open (force=false)
  }, [load]);

  const copyManual = () => {
    const cmd = status?.manual_command;
    if (!cmd) return;
    void navigator.clipboard?.writeText(cmd).then(
      () => deckToast(t("upd-copied")),
      () => {/* clipboard blocked — the command is shown inline regardless */},
    );
  };

  const apply = () => {
    setApplying(true);
    const stop = deckWorkingToast(t("upd-applying"));
    hub
      .call<{ ok: boolean; output: string; restart_required: boolean }>("update.apply", {}, 320_000)
      .then((r) => {
        if (r.ok) {
          setDone(true);
          setRestartIn(10); // auto-restart into the new code, cancelable
          deckToast(t("upd-done"));
        } else {
          // Auto-update failed — point the user at the by-hand command (always shown below too).
          deckToastAction(`${t("upd-failed")}: ${(r.output || "").slice(-200)}`, t("upd-copy"), copyManual, 12000);
        }
      })
      .catch((e) => deckToast(rpcErrText(t, { message: errMsg(e) }), true))
      .finally(() => {
        stop();
        setApplying(false);
      });
  };

  const isCurrent = (tag: string) =>
    !!status && (tag === `v${status.current}` || tag === status.current);

  return (
    <div className="update-pane">
      <div className="set-row">
        <div className="lbl">
          <span>{t("upd-current")}</span>
          <small>{status ? `v${status.current} · ${t(`upd-ch-${status.channel}` as "upd-ch-dev")}` : "…"}</small>
        </div>
        <button className="btn soft" disabled={checking} onClick={() => load(true)}>
          {checking ? t("upd-checking") : t("upd-check")}
        </button>
      </div>

      {status &&
        (status.update_available ? (
          <div className="upd-banner on">
            <div className="upd-banner-text">
              <strong>{t("upd-available")}</strong>
              <span>
                {status.latest
                  ? `v${status.current} → ${status.latest}`
                  : status.behind
                    ? t("upd-behind", { n: status.behind })
                    : ""}
              </span>
            </div>
            <button className="btn primary" disabled={applying || done} onClick={apply}>
              {applying ? t("upd-applying") : t("upd-apply")}
            </button>
          </div>
        ) : (
          <div className="upd-banner">{t("upd-uptodate")}</div>
        ))}

      {/* Always-available fallback: if the one-click update can't run, the user has
          the exact command to run by hand (the AstrBot pattern). */}
      {status?.update_available && status.manual_command && (
        <div className="upd-manual">
          <span className="upd-manual-lbl">{t("upd-manual")}</span>
          <code className="upd-manual-cmd">{status.manual_command}</code>
          <button className="btn soft" onClick={copyManual}>{t("upd-copy")}</button>
        </div>
      )}

      {restarting ? (
        <div className="upd-note">{t("upd-restarting")}</div>
      ) : restartIn !== null ? (
        <div className="upd-banner on upd-restart-bar">
          <div className="upd-banner-text">
            <strong>{t("upd-restart-in", { n: restartIn })}</strong>
          </div>
          <div className="upd-restart-acts">
            <button className="btn soft" onClick={() => setRestartIn(null)}>{t("upd-restart-cancel")}</button>
            <button className="btn primary" onClick={triggerRestart}>{t("upd-restart-now")}</button>
          </div>
        </div>
      ) : (
        done && <div className="upd-note">{t("upd-restart")}</div>
      )}

      <h3 className="upd-cl-title">{t("upd-changelog")}</h3>
      {status && status.releases.length ? (
        <div className="upd-changelog">
          {status.releases.map((r) => (
            <div className="upd-rel" key={r.tag}>
              <div className="upd-rel-head">
                <span className="upd-rel-tag">{r.name || r.tag}</span>
                {r.published_at && <span className="upd-rel-date">{r.published_at.slice(0, 10)}</span>}
                {isCurrent(r.tag) && <span className="upd-rel-cur">{t("upd-installed")}</span>}
              </div>
              <div className="upd-rel-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{r.body || t("upd-no-notes")}</ReactMarkdown>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="placeholder-pane">{checking ? t("upd-checking") : t("upd-no-notes")}</div>
      )}
    </div>
  );
}
