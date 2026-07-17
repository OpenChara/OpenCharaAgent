/* GatewayPane — the chat right-panel「网关」tab. A React port of chat.js
 * renderGatewayPane (1402): the chara's gateway state + adapter config, the WeChat
 * QR login flow (weixin.qr → poll weixin.qr_status), enable/disable
 * (gateway.start/stop), and field-level config save (messaging.get / messaging.save).
 *
 * RPCs (all hub-level — cross-checked against src/chara/server/hub.py):
 *   messaging.get / messaging.save / gateway.status / gateway.start / gateway.stop /
 *   weixin.qr / weixin.qr_status.
 *
 * Binding UI rule: the enable switch flips immediately (optimistic) and reverts +
 * surfaces the error on failure; config fields auto-save on blur (saved / error
 * toast); the QR section shows a working state while fetching and polling.
 *
 * The QR payload is encoded locally with the bundled `qrcode` dep — the login
 * payload (scan_content) is never shipped to a third party; `qr.qrcode` is only the
 * polling token, not what the phone scans (chat.js note). */

import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { useT, type TKey } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";
import {
  GW_PLATFORMS,
  GW_MASK,
  buildSaveConfig,
  platformEnabled,
  togglePlatform,
  requiredFilled,
  allowedToString,
  type GwField,
  type MessagingConfig,
  type GatewayStatus,
} from "./gatewayModel";

// `platform` is CONTROLLED by the parent (the gateway modal's 网关 selector), so a
// chara + platform pair is chosen at the top of the modal, consistent with the
// model pane's provider + model boxes. GatewayPane renders only the config body for
// that pair.
export function GatewayPane({ name, platform: plat }: { name: string; platform: string }) {
  const t = useT();
  const { hub } = useHubApi();

  const [cfg, setCfg] = useState<MessagingConfig | null>(null);
  const [status, setStatus] = useState<GatewayStatus>({ state: "stopped", platform: "", detail: "" });
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [enabled, setEnabled] = useState(false);
  const [switching, setSwitching] = useState(false);

  // Live field inputs + their persisted-value initials, for the field-level merge
  // save. Seeded once per field (seedField) and reset SYNCHRONOUSLY on a platform
  // switch — the old post-render wipe effect emptied initialRef right after render
  // had seeded it, so clearing a field compared "" against "" ("unchanged") and the
  // explicit-null delete was never sent.
  const inputsRef = useRef<Record<string, string>>({});
  const initialRef = useRef<Record<string, string>>({});
  const allowedRef = useRef<string>("");
  const platRef = useRef(plat);
  if (platRef.current !== plat) {
    platRef.current = plat;
    inputsRef.current = {};
    initialRef.current = {};
  }

  const load = useCallback(async () => {
    setLoading(true);
    let nextCfg: MessagingConfig;
    try {
      const r = await hub.call<{ config?: MessagingConfig }>("messaging.get", { name }, 15000);
      nextCfg = (r && r.config) || {};
      setErr(null);
    } catch (e) {
      setErr(rpcErrText(t, e as { message?: string }));
      setLoading(false);
      return;
    }
    let nextStatus: GatewayStatus = { state: "stopped", platform: "", detail: "" };
    try {
      nextStatus = (await hub.call<GatewayStatus>("gateway.status", { name }, 15000)) || nextStatus;
    } catch {
      /* keep the stopped default; messaging.get already succeeded */
    }
    setCfg(nextCfg);
    setStatus(nextStatus);
    setEnabled(platformEnabled(nextCfg, plat)); // THIS platform's effective on-state
    setLoading(false);
  }, [hub, name, t, plat]);

  useEffect(() => {
    void load();
  }, [load]);

  // Keep the allowed-senders ref in step with the loaded/saved config (its input is
  // uncontrolled; a live edit overwrites this via onChange before any save reads it).
  useEffect(() => {
    allowedRef.current = cfg ? allowedToString(cfg) : "";
  }, [cfg]);

  // Field auto-save: persists edited fields and re-asserts THIS platform's current
  // enabled flag (so a blur never clobbers the on/off the switch set), with the
  // top-level enabled re-derived from the live cfg.
  const saveConfig = useCallback(async () => {
    const config = buildSaveConfig({
      plat,
      enabled,
      allowedText: allowedRef.current,
      current: inputsRef.current,
      initial: initialRef.current,
      cfg: cfg || {},
    });
    const r = await hub.call<{ config?: MessagingConfig }>("messaging.save", { name, config }, 20000);
    if (r && r.config) setCfg(r.config);
    // The edits are persisted now: adopt them as the new initials so the next blur
    // doesn't resend the same diffs (a cleared field then compares "" === "").
    initialRef.current = { ...inputsRef.current };
  }, [hub, name, plat, enabled, cfg]);

  const saveOnBlur = useCallback(() => {
    saveConfig()
      .then(() => deckToast(t("saved")))
      .catch((e) => deckToast(rpcErrText(t, e as { message?: string }), true));
  }, [saveConfig, t]);

  // The switch flips THIS platform: messaging.save (this platform's enabled + the
  // re-derived top-level) then reconcile (gateway.start if any platform lands on,
  // else gateway.stop). In-flight field edits ride along via togglePlatform.
  const toggleEnable = useCallback(async () => {
    if (switching) return;
    const turnOn = !enabled;
    setEnabled(turnOn); // optimistic
    setSwitching(true);
    try {
      const r = await togglePlatform({
        hub,
        name,
        plat,
        next: turnOn,
        cfg: cfg || {},
        allowedText: allowedRef.current,
        current: inputsRef.current,
        initial: initialRef.current,
      });
      if (r.config) setCfg(r.config);
      initialRef.current = { ...inputsRef.current }; // in-flight edits rode along and are persisted
      setStatus(r.status || { state: turnOn ? "needs_login" : "stopped" });
    } catch (e) {
      setEnabled(!turnOn); // revert
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setSwitching(false);
    }
  }, [enabled, switching, hub, name, plat, cfg, t]);

  if (loading && !cfg) return <div className="placeholder-pane">…</div>;
  if (err && !cfg) return <div className="gw-error">{err}</div>;

  const conf = cfg || {};
  const spec = GW_PLATFORMS[plat];
  // Run-state for THIS platform: read its row from status.platforms, falling back
  // to the aggregate gateway state when the per-platform breakdown isn't present.
  const platRow = (status.platforms || []).find((p) => p.platform === plat);
  const st = (platRow && platRow.state) || status.state || "stopped";
  const runText = st === "running" ? t("gw-running") : st === "needs_login" ? t("gw-needs-login") : t("gw-stopped");
  const runCls = st === "running" ? "ok" : st === "needs_login" ? "warn" : "";
  const filled = requiredFilled(conf, plat);

  const seedField = (fd: GwField, value: string) => {
    // Seed ONCE per field: a later re-render (e.g. after a save updates cfg) must
    // neither clobber a live uncontrolled edit nor reset the persisted-value
    // initial — that reset is what made a cleared field look "unchanged".
    if (fd.key in initialRef.current) return;
    inputsRef.current[fd.key] = value;
    initialRef.current[fd.key] = value;
  };

  return (
    <div>
      <div className="sub" style={{ marginBottom: 12 }}>
        {t("gw-sub")}
      </div>

      <div className="gw-chips">
        <span className={"gw-chip " + (enabled ? "ok" : "")}>{enabled ? t("gw-enabled") : t("gw-disabled")}</span>
        <span className={"gw-chip " + (filled ? "ok" : "warn")}>{filled ? t("gw-configured") : t("gw-needs-setup")}</span>
        <span className={"gw-chip " + runCls}>{runText}</span>
      </div>

      <div className="gw-blurb">{t(spec.blurb)}</div>
      {/* Setup note for non-QR platforms (the QR ones show it in the QR section
          below). Carries the must-know hint, e.g. Discord's Message Content intent. */}
      {spec.note && !spec.qr && <div className="gw-blurb gw-note">{t(spec.note)}</div>}
      {spec.pending && <div className="gw-banner draft-note">{t(spec.pending)}</div>}

      {/* QR is the login path; once running there's nothing to scan. */}
      {spec.qr && st !== "running" && (
        <>
          <QrSection name={name} onConfirmed={load} />
          {spec.note && <div className="gw-blurb">{t(spec.note)}</div>}
        </>
      )}

      {spec.required.length > 0 && (
        <div className="gw-sec">
          <h4>{t("gw-required")}</h4>
          {spec.required.map((fd) => (
            <FieldRow key={`${plat}:${fd.key}`} fd={fd} conf={conf} plat={plat} onSeed={seedField} onSave={saveOnBlur} inputs={inputsRef} />
          ))}
        </div>
      )}

      <div className="gw-sec">
        <h4>{t("gw-recommended")}</h4>
        {spec.recommended.map((fd) => (
          <FieldRow key={`${plat}:${fd.key}`} fd={fd} conf={conf} plat={plat} onSeed={seedField} onSave={saveOnBlur} inputs={inputsRef} />
        ))}
        {/* allowed_senders — a shared top-level field, with the security reason. */}
        <div className="gw-field">
          <label>{t("gw-f-allowed")}</label>
          <div className="why">{t("gw-allowed-why")}</div>
          <input
            defaultValue={allowedToString(conf)}
            onChange={(e) => {
              allowedRef.current = e.target.value;
            }}
            onBlur={saveOnBlur}
          />
        </div>
      </div>

      {spec.advanced.length > 0 && (
        <details className="gw-adv">
          <summary>
            {t("gw-advanced")} ({spec.advanced.length})
          </summary>
          {spec.advanced.map((fd) => (
            <FieldRow key={`${plat}:${fd.key}`} fd={fd} conf={conf} plat={plat} onSeed={seedField} onSave={saveOnBlur} inputs={inputsRef} />
          ))}
        </details>
      )}

      <div className="gw-foot">
        <button
          className={"switch" + (enabled ? " on" : "")}
          disabled={switching || !!spec.pending}
          onClick={() => void toggleEnable()}
        />
        <span className="enable-lbl">{enabled ? t("gw-enabled") : t("gw-disabled")}</span>
      </div>
    </div>
  );
}

/* One adapter config field. Uncontrolled (defaultValue) so live edits land in the
   shared inputs map; the value is captured at render time as the merge-save initial.
   A secret field shows the mask as placeholder and never echoes the stored secret. */
function FieldRow({
  fd,
  conf,
  plat,
  onSeed,
  onSave,
  inputs,
}: {
  fd: GwField;
  conf: MessagingConfig;
  plat: string;
  onSeed: (fd: GwField, value: string) => void;
  onSave: () => void;
  inputs: React.MutableRefObject<Record<string, string>>;
}) {
  const t = useT();
  const a = (conf.adapters || {})[plat] || {};
  const raw = (a as Record<string, unknown>)[fd.key];
  const value = raw !== undefined && raw !== null ? String(raw) : "";
  // Seed the live + initial maps for this render (must run before any blur).
  onSeed(fd, value);
  return (
    <div className="gw-field">
      <label>{fd.label ? t(fd.label as TKey) : fd.key}</label>
      {fd.help && <div className="why">{t(fd.help)}</div>}
      <input
        type={fd.secret ? "password" : "text"}
        defaultValue={value}
        placeholder={fd.ph ? t(fd.ph as TKey) : fd.secret ? GW_MASK : ""}
        onChange={(e) => {
          inputs.current[fd.key] = e.target.value;
        }}
        onBlur={onSave}
      />
      {fd.secret && <div className="why">{t("gw-secret-keep")}</div>}
    </div>
  );
}

/* WeChat QR login: weixin.qr → render the scannable payload locally, then poll
   weixin.qr_status (~2.5s, capped at 96 polls = 4min). A confirmed login is saved
   backend-side; we then (re)start the gateway and refresh the pane. The poll stops
   on unmount / when this section leaves the tree. */
type QrState =
  | { phase: "idle"; refetch: boolean; msg?: string; err?: boolean }
  | { phase: "fetching" }
  | { phase: "waiting"; dataUrl: string; fallbackUrl?: string }
  | { phase: "starting"; accountId: string }
  | { phase: "done" };

function QrSection({ name, onConfirmed }: { name: string; onConfirmed: () => void }) {
  const t = useT();
  const { hub } = useHubApi();
  const [s, setS] = useState<QrState>({ phase: "idle", refetch: false });
  const aliveRef = useRef(true);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const busyRef = useRef(false);

  const stopPoll = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
    busyRef.current = false;
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      stopPoll();
    };
  }, [stopPoll]);

  const fetchQr = useCallback(async () => {
    stopPoll();
    setS({ phase: "fetching" });
    let qr: { qrcode?: string; scan_content?: string; img?: string; fallback_url?: string };
    try {
      qr = await hub.call("weixin.qr", { name }, 30000);
    } catch (e) {
      if (aliveRef.current) setS({ phase: "idle", refetch: true, err: true, msg: rpcErrText(t, e as { message?: string }) });
      return;
    }
    if (!aliveRef.current) return;
    const scan = qr.scan_content || qr.img || "";
    let dataUrl = "";
    if (scan) {
      try {
        dataUrl = await QRCode.toDataURL(String(scan), { errorCorrectionLevel: "M", margin: 2, width: 220 });
      } catch {
        dataUrl = "";
      }
    }
    if (!aliveRef.current) return;
    setS({ phase: "waiting", dataUrl, fallbackUrl: qr.fallback_url });
    // Poll the login state until confirmed / expired / timeout.
    const token = String(qr.qrcode || "");
    let polls = 0;
    timerRef.current = setInterval(async () => {
      if (!aliveRef.current) {
        stopPoll();
        return;
      }
      if (busyRef.current) return;
      if (++polls > 96) {
        stopPoll();
        setS({ phase: "idle", refetch: true, err: true, msg: t("gw-qr-timeout") });
        return;
      }
      busyRef.current = true;
      let r: { status?: string; account_id?: string };
      try {
        r = await hub.call("weixin.qr_status", { name, qrcode: token }, 10000);
      } catch (e) {
        stopPoll();
        if (aliveRef.current) setS({ phase: "idle", refetch: true, err: true, msg: rpcErrText(t, e as { message?: string }) });
        return;
      } finally {
        busyRef.current = false;
      }
      if (r.status === "confirmed") {
        stopPoll();
        setS({ phase: "starting", accountId: r.account_id || "" });
        // Scanning saved the login + ensured the weixin adapter; start the gateway
        // so it actually connects, then refresh the pane to show running state.
        try {
          await hub.call("gateway.stop", { name }, 15000).catch(() => {});
          await hub.call("gateway.start", { name }, 30000);
        } catch {
          /* the pane refresh surfaces the resulting state */
        }
        if (aliveRef.current) {
          setS({ phase: "done" });
          onConfirmed();
        }
      } else if (r.status === "expired") {
        stopPoll();
        setS({ phase: "idle", refetch: true, err: true, msg: t("gw-qr-expired") });
      }
    }, 2500);
  }, [hub, name, t, stopPoll, onConfirmed]);

  return (
    <div className="gw-qr-box">
      {s.phase === "idle" && (
        <>
          {s.msg && <div className={s.err ? "gw-qr-err" : "gw-blurb"}>{s.msg}</div>}
          <button className="btn soft" onClick={() => void fetchQr()}>
            {t(s.refetch ? "gw-qr-refetch" : "gw-qr-get")}
          </button>
        </>
      )}
      {s.phase === "fetching" && <div className="gw-blurb">{t("gw-qr-fetching")}</div>}
      {s.phase === "waiting" && (
        <>
          {s.dataUrl ? (
            <img className="gw-qr-img" src={s.dataUrl} alt="QR" />
          ) : s.fallbackUrl ? (
            <img className="gw-qr-img" src={s.fallbackUrl} alt="QR" />
          ) : null}
          <div className="gw-blurb" style={{ margin: "4px 0 0" }}>
            {t("gw-qr-waiting")}
          </div>
        </>
      )}
      {s.phase === "starting" && (
        <>
          <div className="gw-qr-ok">{t("gw-qr-confirmed", { id: s.accountId })}</div>
          <div className="gw-blurb" style={{ marginTop: 6 }}>
            {t("gw-qr-starting")}
          </div>
        </>
      )}
      {s.phase === "done" && <div className="gw-qr-ok">{t("gw-qr-confirmed", { id: "" })}</div>}
    </div>
  );
}
