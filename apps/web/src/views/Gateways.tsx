/* Gateways — the gateway overview. Now ONE row per (chara, platform): each chara's
 * gateway breaks into its CONFIGURED platforms (gateway.platforms), and every
 * platform gets its own state chip + independent enable switch + Manage button.
 *
 * Both「新建网关」and「管理」open the GatewayModal — one card with a 角色 + 网关
 * selector at the top and the config (WeChat QR login, adapter fields, enable
 * switch) below. New: chara is choosable. Manage: chara + the row's platform are
 * pre-selected (the chara box is locked).
 *
 * Binding UI rule: each platform switch flips immediately (optimistic) and reverts
 * + surfaces the error on failure; the refresh button shows a working state. */

import { useCallback, useEffect, useState } from "react";
import { useT } from "../i18n";
import { useHub, type BoardSession } from "../state/hub";
import { rpcErrText } from "../lib/status";
import { gwPlatLabel, gwStatusBits } from "../components/gateways/status";
import { GatewayModal } from "../components/gateways/GatewayModal";
import { deckToast } from "../components/ui/deckToast";
import { BrandLoader } from "../components/ui/BrandLoader";
import { togglePlatform, type GwPlatformRow, type MessagingConfig } from "../components/gateways/gatewayModel";

interface GatewayRow {
  name: string;
  enabled?: boolean;
  gateway?: { platform?: string; state?: string; detail?: string; platforms?: GwPlatformRow[] };
}

/** A flat per-(chara, platform) row for the overview. */
interface PlatRow {
  name: string; // chara session name
  platform: string;
  enabled: boolean;
  state: string;
}

/** Reconstruct a minimal MessagingConfig from a chara's platform breakdown, so a
 *  single-platform toggle can re-derive the top-level enabled against the OTHER
 *  platforms' current states. Each platform's `enabled` is stored explicitly (no
 *  legacy inherit needed — the backend already resolved it into the row). */
function cfgFromPlatforms(platforms: GwPlatformRow[]): MessagingConfig {
  const adapters: Record<string, Record<string, unknown>> = {};
  for (const p of platforms) {
    if (p.platform) adapters[p.platform] = { enabled: !!p.enabled };
  }
  return { adapters };
}

export function Gateways() {
  const t = useT();
  const { hub, snapshot } = useHub();
  const [rows, setRows] = useState<GatewayRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  // The gateway-config modal: which chara + platform, and whether the chara binding
  // is locked (manage = locked to the row; new = choosable). null = closed.
  const [modal, setModal] = useState<{ name: string; platform?: string; lock: boolean } | null>(null);

  const sessions = (snapshot?.sessions as BoardSession[] | undefined) || [];
  const byName: Record<string, BoardSession> = {};
  for (const s of sessions) byName[s.name] = s;

  // A new gateway always binds to a chara: no chara → a toast; otherwise open the
  // modal with the chara box choosable (pre-filled when there's only one).
  const newGateway = () => {
    if (!sessions.length) {
      deckToast(t("gw-no-chara"), true);
      return;
    }
    setModal({ name: sessions.length === 1 ? sessions[0].name : "", lock: false });
  };

  const manage = (pr: PlatRow) => setModal({ name: pr.name, platform: pr.platform, lock: true });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await hub.call<{ gateways?: GatewayRow[] }>("gateways.list", {}, 20000);
      setRows((data && data.gateways) || []);
      setErr(null);
    } catch (e) {
      setErr(rpcErrText(t, e as { message?: string }));
    } finally {
      setLoading(false);
    }
  }, [hub, t]);

  useEffect(() => {
    void load();
  }, [load]);

  // Flatten to one row per (chara, platform): each chara's gateway breaks into its
  // configured platforms. A chara with zero configured platforms contributes none.
  const platRows: PlatRow[] = [];
  for (const r of rows) {
    const platforms = (r.gateway && r.gateway.platforms) || [];
    for (const p of platforms) {
      if (!p.platform) continue;
      platRows.push({ name: r.name, platform: p.platform, enabled: !!p.enabled, state: String(p.state || "stopped") });
    }
  }
  const rowKey = (name: string, plat: string) => `${name}/${plat}`;

  // Toggle ONE platform: messaging.save (this platform's enabled + re-derived
  // top-level) then reconcile (gateway.start if any platform lands on, else
  // gateway.stop). Optimistic flip + revert, exactly as the pane does.
  const toggle = async (pr: PlatRow) => {
    const key = rowKey(pr.name, pr.platform);
    if (busy.has(key)) return;
    const turnOn = !pr.enabled;
    // optimistic: flip just this platform's switch
    setRows((prev) =>
      prev.map((x) => {
        if (x.name !== pr.name || !x.gateway) return x;
        const platforms = (x.gateway.platforms || []).map((p) =>
          p.platform === pr.platform ? { ...p, enabled: turnOn } : p,
        );
        return { ...x, gateway: { ...x.gateway, platforms } };
      }),
    );
    setBusy((prev) => new Set(prev).add(key));
    try {
      const cur = rows.find((x) => x.name === pr.name);
      const cfg = cfgFromPlatforms((cur && cur.gateway && cur.gateway.platforms) || []);
      await togglePlatform({ hub, name: pr.name, plat: pr.platform, next: turnOn, cfg });
      await load();
    } catch (e) {
      // revert
      setRows((prev) =>
        prev.map((x) => {
          if (x.name !== pr.name || !x.gateway) return x;
          const platforms = (x.gateway.platforms || []).map((p) =>
            p.platform === pr.platform ? { ...p, enabled: !turnOn } : p,
          );
          return { ...x, gateway: { ...x.gateway, platforms } };
        }),
      );
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  };

  return (
    <div className="view active" id="view-gateways">
      <div className="toolbar">
        <h1>
          <span>{t("nav-gateways")}</span>
          <span className="count">{platRows.length ? String(platRows.length) : ""}</span>
        </h1>
        <div className="grow" />
        <button className="btn soft" disabled={loading} onClick={() => void load()}>
          {loading ? <span className="spin" /> : t("gw-refresh")}
        </button>
        <button className="btn primary" onClick={newGateway}>
          {t("gw-new")}
        </button>
      </div>

      {/* .is-empty flips the container from the card grid (align-content:start —
          which would cram the empty-state into one top-left track) to a flex
          column so .empty-state (flex:1) centers in the pane like Board/Deck. */}
      <div className={"gw-overview" + (snapshot && !err && !platRows.length ? " is-empty" : "")}>
        {!snapshot ? (
          <BrandLoader />
        ) : err ? (
          <div className="gw-error">{err}</div>
        ) : !platRows.length ? (
          <div className="empty-state">
            <div className="empty-title">{t("gw-empty")}</div>
            <div className="acts">
              <button className="btn primary" onClick={newGateway}>
                {t("gw-new")}
              </button>
            </div>
          </div>
        ) : (
          platRows.map((pr) => {
            const bits = gwStatusBits(t, { state: pr.state });
            const sess = byName[pr.name] || ({ char_name: pr.name } as BoardSession);
            const key = rowKey(pr.name, pr.platform);
            return (
              <div className="gw-card" key={key}>
                <div className="gw-card-head">
                  <span className="gw-plat-name">{gwPlatLabel(t, pr.platform)}</span>
                  <span className={"gw-chip " + bits.cls}>{bits.text}</span>
                </div>
                <div className="gw-card-sub">
                  {t("gw-bound")}：{sess.char_name || pr.name}
                </div>
                <div className="gw-card-foot">
                  <button
                    className={"switch" + (pr.enabled ? " on" : "")}
                    disabled={busy.has(key)}
                    onClick={() => void toggle(pr)}
                  />
                  <span className="enable-lbl">{pr.enabled ? t("gw-enabled") : t("gw-disabled")}</span>
                  <div className="grow" />
                  <button className="btn soft" onClick={() => manage(pr)}>
                    {t("gw-manage")}
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>

      {modal && (
        <GatewayModal
          sessions={sessions}
          initialName={modal.name}
          initialPlatform={modal.platform}
          lockChara={modal.lock}
          onClose={() => {
            setModal(null);
            void load(); // reflect any run-state / config change in the overview
          }}
        />
      )}
    </div>
  );
}
