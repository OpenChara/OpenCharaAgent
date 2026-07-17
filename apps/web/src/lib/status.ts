/* Status / life-state derivers — ported from front/web/app.js
 * (statusOf / lifeText / boardErrText / shortErr / errText / rpcErrText) plus the
 * chat.js lifeWord extension. These turn a backend session/life snapshot into the
 * one-line factual status the board + chat header show. No DOM. Status words are
 * factual statements only — the platform never roleplays.
 *
 * The `now` epoch (ms) is injectable so the time-relative branches are testable. */

import type { TFn } from "../i18n";
import { fmtClock } from "./format";

/** A chara's live-state snapshot (supervisor life.state). Only the fields these
 *  derivers read are typed; unknown fields are tolerated. */
export interface LifeSnapshot {
  state?: string;
  next_cycle_at?: number;
  rest_until?: number;
  engaged_until?: number;
  detail?: string;
}

/** A board session row — the subset statusOf/boardErrText read. */
export interface SessionSnapshot {
  status?: string;
  error?: string;
  error_kind?: string;
  paused?: boolean;
  last_active?: number;
  life?: LifeSnapshot;
  /** the newest conversation message (user turn, chara reply, or speak text) —
   *  the board's WeChat-style preview line. Absent/null until a real exchange. */
  last_message?: { text: string; ts: number; role: "user" | "chara" } | null;
}

/** The one-line status descriptor a board card renders. */
export interface StatusLine {
  dot: "live" | "off" | "err";
  line: string;
  cls: "" | "err" | "msg";
}

/** Raw error → message string (the un-translated variant; for system lines etc.). */
export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Error kind → human reason. app.js:207 errText. */
export function errText(t: TFn, err: { kind?: string } | null | undefined): string {
  const kind = err && err.kind ? err.kind : "provider";
  const map: Record<string, string> = {
    auth: "err-auth",
    credit: "err-credit",
    network: "err-network",
    model: "err-model",
    ratelimit: "err-ratelimit",
    draft_json: "err-draft-json",
    draft_schema: "err-draft-schema",
  };
  return t(map[kind] || "err-provider");
}

/** An RPC error (with optional `.data.kind`/`.detail`) → human text.
 *  app.js:215 rpcErrText. */
export function rpcErrText(
  t: TFn,
  e: { message?: string; data?: { kind?: string; detail?: string } | null } | null | undefined,
): string {
  const data = e && e.data;
  if (data && data.kind) return errText(t, data) + (data.detail ? ` · ${data.detail}` : "");
  return e && e.message ? e.message : t("err-provider");
}

/** Heuristic mapping of a raw error line to a friendly reason. app.js:764 shortErr. */
export function shortErr(t: TFn, line: string): string {
  const low = String(line).toLowerCase();
  if (low.includes("credit") || low.includes("balance") || low.includes("402")) return t("err-credit");
  if (low.includes("401") || low.includes("403") || low.includes("auth")) return t("err-auth");
  if (
    low.includes("timeout") ||
    low.includes("connect") ||
    low.includes("network") ||
    low.includes("unreachable")
  )
    return t("err-network");
  return t("err-provider");
}

/** Board-card error text from a session's error_kind/error. app.js:772 boardErrText. */
export function boardErrText(t: TFn, s: SessionSnapshot | null | undefined): string {
  const kind = s && s.error_kind ? s.error_kind : "";
  if (kind === "auth") return t("board-key-invalid");
  if (kind === "credit") return t("err-credit");
  if (kind === "model") return t("err-model");
  if (kind === "ratelimit") return t("err-ratelimit");
  if (kind === "network") return t("err-network");
  return s && s.error ? shortErr(t, s.error) : t("st-error");
}

/** A life snapshot → one factual line. app.js:613 lifeText.
 *  `working` = an LLM turn in flight; `idle_countdown` = the gap between cycles. */
export function lifeText(t: TFn, life: LifeSnapshot | null | undefined, now: number = Date.now()): string {
  if (!life) return "";
  if (life.state === "working") return t("life-working");
  if (life.state === "idle_countdown") {
    const n = life.next_cycle_at
      ? Math.max(0, Math.round((life.next_cycle_at - now / 1000) / 60))
      : null;
    return n && n >= 1 ? t("life-idle-next", { n }) : t("life-idle");
  }
  if (life.state === "waiting") return t("life-waiting");
  if (life.state === "resting" && life.rest_until) return t("life-resting-until", { time: fmtClock(life.rest_until) });
  if (life.state === "resting") return t("st-resting");
  if (life.state === "backoff") return `${t("life-backoff")}${life.detail ? " · " + life.detail : ""}`;
  return t("life-idle");
}

/** The chat-header life line — like lifeText but, while waiting, says how many
 *  minutes until it returns to its own work. chat.js:998 lifeWord. */
export function lifeWord(t: TFn, life: LifeSnapshot, now: number = Date.now()): string {
  if (life.state === "waiting" && life.engaged_until) {
    const leftMin = Math.ceil((life.engaged_until - now / 1000) / 60);
    if (leftMin >= 1) return t("life-waiting-back", { n: leftMin });
  }
  return lifeText(t, life, now);
}

/** A session row → its single status line. app.js:594 statusOf. */
export function statusOf(t: TFn, s: SessionSnapshot, now: number = Date.now()): StatusLine {
  if (s.status === "new") return { dot: "off", line: t("st-new"), cls: "" };
  if (s.status === "crashed") return { dot: "err", line: s.error || "crashed", cls: "err" };
  if (s.error && (s.error_kind === "auth" || (s.status !== "attached" && s.status !== "running")))
    return { dot: "err", line: t("st-error"), cls: "err" };
  // The board headlines the LAST CONVERSATION MESSAGE (WeChat semantics): the
  // newest user turn, chara reply, or speak text — the backend's last_message.
  // The backend already excludes the card's opening `first_mes` on a chara that
  // has only greeted, so the opener never reads as a message line here. With no
  // conversation yet, fall through to the factual life/idle status. The dot
  // still reflects autonomy: off = paused (mode chat), else live.
  const dot = s.paused ? "off" : "live";
  const lm = s.last_message;
  if (lm && lm.text) return { dot, line: lm.text, cls: "msg" };
  // OFF = autonomy off (mode chat). That is the ONLY "offline" the board shows:
  // the chara's on/off state IS its autonomy, decoupled from any process/PID.
  if (s.paused) return { dot: "off", line: t("st-paused"), cls: "" };
  if (s.life && s.life.state) return { dot: "live", line: lifeText(t, s.life, now), cls: "" };
  return { dot: "live", line: t("st-idle-live"), cls: "" };
}
