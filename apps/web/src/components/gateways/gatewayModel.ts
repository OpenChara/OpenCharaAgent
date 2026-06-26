/* Gateway pane — pure data + the field-level save-merge logic, split out so it can
 * be unit-tested without React. Ported from chat.js GW_PLATFORMS (97) + the
 * renderGatewayPane saveConfig() field merge (1601).
 *
 * The save contract (mirrors hub.py _merge_messaging): the form sends ONLY the
 * platform on screen and OMITS unchanged fields (including unchanged secret masks),
 * a cleared field becomes an explicit null (delete), and allowed_senders is a
 * top-level shared field. */

import type { TKey } from "../../i18n";

/** Backend secret-echo mask (hub.py _SECRET_MASK). */
export const GW_MASK = "••••••••";

export interface GwField {
  key: string;
  /** i18n key OR a literal label (e.g. "base_url"); rendered through t(). */
  label: TKey;
  secret: boolean;
  /** i18n key for the one-line "why / where to get it" help under the label. */
  help?: TKey;
  /** placeholder: i18n key when it's a key, literal otherwise. */
  ph?: string;
}

export interface GwPlatform {
  label: TKey;
  blurb: TKey;
  qr?: boolean;
  note?: TKey;
  /** amber banner i18n key when the backend adapter isn't shipped (disables enable). */
  pending?: TKey;
  required: GwField[];
  recommended: GwField[];
  advanced: GwField[];
}

/* The gateway pane shows the surfaced platforms here. weixinpad exists in
   messaging/ but isn't surfaced; re-add its entry here to bring it back.
   Platform key = the backend adapter name. */
export const GW_PLATFORMS: Record<string, GwPlatform> = {
  weixin: {
    label: "gw-weixin-label",
    blurb: "gw-weixin-blurb",
    qr: true,
    note: "gw-weixin-note",
    required: [],
    recommended: [],
    advanced: [
      { key: "base_url", label: "base_url", secret: false, help: "gw-h-wx-base", ph: "https://ilinkai.weixin.qq.com" },
      { key: "bot_type", label: "bot_type", secret: false, help: "gw-h-wx-bot-type", ph: "3" },
      { key: "long_poll_timeout_ms", label: "long_poll_timeout_ms", secret: false, help: "gw-h-wx-poll", ph: "35000" },
      { key: "api_timeout_ms", label: "api_timeout_ms", secret: false, help: "gw-h-wx-api-timeout", ph: "15000" },
    ],
  },
  qq: {
    label: "gw-qq-label",
    blurb: "gw-qq-blurb",
    note: "gw-qq-note",
    required: [
      { key: "url", label: "gw-f-qq-url", secret: false, help: "gw-h-qq-url", ph: "ws://127.0.0.1:3001" },
    ],
    recommended: [
      { key: "access_token", label: "gw-f-access-token", secret: true, help: "gw-h-qq-token" },
      { key: "peer_id", label: "gw-f-peer-id", secret: false, help: "gw-h-peer-id", ph: "10001" },
    ],
    advanced: [],
  },
  telegram: {
    label: "gw-telegram-label",
    blurb: "gw-tg-blurb",
    note: "gw-tg-note",
    required: [
      { key: "bot_token", label: "gw-f-tg-token", secret: true, help: "gw-h-tg-token", ph: "gw-ph-tg-token" },
    ],
    recommended: [],
    advanced: [
      { key: "api_base", label: "gw-f-tg-api-base", secret: false, help: "gw-h-tg-api-base", ph: "https://api.telegram.org" },
    ],
  },
  discord: {
    label: "gw-discord-label",
    blurb: "gw-discord-blurb",
    note: "gw-discord-note",
    required: [
      { key: "bot_token", label: "gw-f-discord-token", secret: true, help: "gw-h-discord-token" },
    ],
    recommended: [
      { key: "owner_id", label: "gw-f-owner-id", secret: false, help: "gw-h-owner-id" },
      { key: "channel_id", label: "gw-f-channel-id", secret: false, help: "gw-h-channel-id" },
    ],
    advanced: [],
  },
  slack: {
    label: "gw-slack-label",
    blurb: "gw-slack-blurb",
    note: "gw-slack-note",
    required: [
      { key: "bot_token", label: "gw-f-slack-bot-token", secret: true, help: "gw-h-slack-bot-token" },
      { key: "app_token", label: "gw-f-slack-app-token", secret: true, help: "gw-h-slack-app-token" },
    ],
    recommended: [
      { key: "owner_id", label: "gw-f-owner-id", secret: false, help: "gw-h-owner-id" },
      { key: "channel_id", label: "gw-f-channel-id", secret: false, help: "gw-h-channel-id" },
    ],
    advanced: [],
  },
};

export interface MessagingConfig {
  enabled?: boolean;
  allowed_senders?: unknown;
  adapters?: Record<string, Record<string, unknown> | undefined>;
  [k: string]: unknown;
}

export interface GwPlatformRow {
  platform?: string;
  enabled?: boolean;
  state?: string;
}

export interface GatewayStatus {
  state?: string;
  platform?: string;
  detail?: string;
  error_message?: string;
  /** one entry per CONFIGURED platform: effective enabled + live state. */
  platforms?: GwPlatformRow[];
}

/** The slice of HubClient togglePlatform needs (so the model file stays React-free). */
export interface RpcCaller {
  call<T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number): Promise<T>;
}

/** Flip ONE platform on/off and reconcile the live host. The single algorithm both
 *  the gateway pane's switch and the overview's per-row switch use:
 *   1. messaging.save with this platform's new `enabled` folded into its adapter
 *      block, plus the re-derived top-level `enabled` (= anyEnabled after the change);
 *   2. reconcile the host: gateway.start if the top-level lands ON, else gateway.stop.
 *  `cfg` is the platform's current config (so the top-level enabled is derived from
 *  the OTHER platforms' real states); `changed`/`initial` carry any in-flight field
 *  edits to persist alongside the toggle (empty for the overview's bare switch). */
export async function togglePlatform(args: {
  hub: RpcCaller;
  name: string;
  plat: string;
  next: boolean;
  cfg: MessagingConfig;
  allowedText?: string;
  current?: Record<string, string>;
  initial?: Record<string, string>;
}): Promise<{ config?: MessagingConfig; status?: GatewayStatus }> {
  const config = buildSaveConfig({
    plat: args.plat,
    enabled: args.next,
    allowedText: args.allowedText ?? allowedToString(args.cfg),
    current: args.current ?? {},
    initial: args.initial ?? {},
    cfg: args.cfg,
  });
  const saved = await args.hub.call<{ config?: MessagingConfig }>(
    "messaging.save",
    { name: args.name, config },
    20000,
  );
  // The BACKEND re-derives the authoritative top-level enabled (= any platform
  // effectively on) and returns it; steer the reconcile off THAT, not our own
  // recompute, so the start/stop decision can't drift from what was persisted.
  const topOn = !!(saved?.config?.enabled ?? config.enabled);
  const status = await args.hub.call<GatewayStatus>(
    topOn ? "gateway.start" : "gateway.stop",
    { name: args.name },
    30000,
  );
  return { config: saved && saved.config, status };
}

/** Has the platform's required fields been filled? weixin (login lives in
 *  weixin_state.json) is "configured" once its adapter block exists. */
export function requiredFilled(cfg: MessagingConfig, plat: string): boolean {
  const spec = GW_PLATFORMS[plat];
  if (!spec) return false;
  const a = (cfg.adapters || {})[plat] || {};
  if (spec.required.length === 0) return Object.keys(a).length > 0 || plat === "weixin";
  return spec.required.every((fd) => String((a as Record<string, unknown>)[fd.key] ?? "").length > 0);
}

/** Is this platform effectively on? A platform runs iff its own `enabled` flag is
 *  truthy; when that flag is ABSENT it inherits the legacy top-level `enabled`
 *  (so pre-per-platform configs keep working). Mirrors the backend's
 *  `adapters[plat].enabled ?? cfg.enabled`. */
export function platformEnabled(cfg: MessagingConfig, plat: string): boolean {
  const a = (cfg.adapters || {})[plat];
  const own = a ? (a as Record<string, unknown>).enabled : undefined;
  return own === undefined || own === null ? !!cfg.enabled : !!own;
}

/** Is ANY configured platform effectively on? This is what the top-level `enabled`
 *  (= "is the gateway host on at all") must equal after any change. */
export function anyEnabled(cfg: MessagingConfig): boolean {
  return Object.keys(cfg.adapters || {}).some((plat) => platformEnabled(cfg, plat));
}

/** allowed_senders as a comma-joined string for the input. */
export function allowedToString(cfg: MessagingConfig): string {
  return Array.isArray(cfg.allowed_senders) ? cfg.allowed_senders.map(String).join(", ") : "";
}

/** Parse the allowed-senders input back into a trimmed, de-blanked list (zh/en commas). */
export function parseAllowed(text: string): string[] {
  return text
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Build the messaging.save payload for ONE platform (`plat`).
 *
 *  Field merge (unchanged): only fields whose value differs from their render-time
 *  initial (incl. unchanged masks) are sent; a cleared field → explicit null.
 *  Mirrors chat.js saveConfig() and hub.py _merge_messaging's field-level contract.
 *
 *  Per-platform on/off: `enabled` is THIS platform's new enabled state — it is
 *  folded into the adapter block (`adapters[plat].enabled`), so the save flips just
 *  this platform. The TOP-LEVEL `enabled` (= "is the gateway host on at all") is
 *  DERIVED: it's anyEnabled computed over `cfg` with this platform's new value
 *  applied, so it stays equal to "any platform effectively on". Pass the current
 *  full `cfg` so the other platforms' effective states are visible. */
export function buildSaveConfig(args: {
  plat: string;
  /** THIS platform's new enabled state (folded into adapters[plat].enabled). */
  enabled: boolean;
  allowedText: string;
  /** field key -> current input value */
  current: Record<string, string>;
  /** field key -> render-time initial value (what was shown, incl. masks) */
  initial: Record<string, string>;
  /** the current full config, for deriving the top-level enabled (anyEnabled). */
  cfg?: MessagingConfig;
}): MessagingConfig {
  const spec = GW_PLATFORMS[args.plat];
  const fields: Record<string, string | boolean | null> = {};
  if (spec) {
    for (const fd of [...spec.required, ...spec.recommended, ...spec.advanced]) {
      const f = fd.key;
      if (!(f in args.current)) continue;
      const v = (args.current[f] ?? "").trim();
      const init = args.initial[f] ?? "";
      if (v === init) continue; // omit unchanged (keeps stored value, incl. masks)
      fields[f] = v === "" ? null : v; // cleared → explicit delete
    }
  }
  fields.enabled = args.enabled; // this platform's on/off, folded into its block
  // Top-level enabled = anyEnabled AFTER applying this platform's new value: clone
  // the current adapters, set this platform's effective enabled, then re-derive.
  const cfg = args.cfg || {};
  const adapters: Record<string, Record<string, unknown>> = {};
  for (const [k, v] of Object.entries(cfg.adapters || {})) adapters[k] = { ...(v || {}) };
  adapters[args.plat] = { ...(adapters[args.plat] || {}), enabled: args.enabled };
  const topEnabled = anyEnabled({ enabled: cfg.enabled, adapters });
  return {
    enabled: topEnabled,
    allowed_senders: parseAllowed(args.allowedText),
    adapters: { [args.plat]: fields },
  };
}
