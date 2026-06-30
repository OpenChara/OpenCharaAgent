/* Framework-agnostic formatters — ported verbatim from front/web/app.js
 * (timeAgo / fmtClock / fmtSize / estimateTokens / durationText / modeLabel /
 * paletteClass / glyphOf). No DOM, no el(). Functions that surface UI copy take
 * the translator `t` as their first argument (the JS originals reached a global
 * `t`); everything else is pure math/string work. */

import type { Lang, TFn } from "../i18n";

/** YYYY-MM-DD for an epoch (ms) AS SEEN in a timezone — the calendar-day key used
 *  to decide today/yesterday/older. `tz` "" → the viewer's browser timezone. */
function dayKey(ms: number, tz: string): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: tz || undefined,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(ms);
  } catch {
    // An invalid tz string → fall back to the browser timezone rather than throw.
    return new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit" }).format(ms);
  }
}

function hhmm(ms: number, tz: string): string {
  try {
    return new Intl.DateTimeFormat([], {
      timeZone: tz || undefined,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(ms);
  } catch {
    return new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", hour12: false }).format(ms);
  }
}

/** A WeChat-style chat time label, rendered in the chosen timezone:
 *   today    → "14:05"
 *   yesterday→ "昨天 14:05" / "Yesterday 14:05"
 *   this year→ "6月28日 14:05" / "Jun 28 14:05"
 *   older    → "2025-12-31 14:05"
 *  `ts` is epoch SECONDS; `tz` "" follows the browser. `now` is injectable for tests. */
export function chatTimeLabel(
  t: TFn,
  lang: Lang,
  ts: number,
  tz: string = "",
  now: number = Date.now(),
): string {
  const ms = ts * 1000;
  const clock = hhmm(ms, tz);
  const dMsg = dayKey(ms, tz);
  const dNow = dayKey(now, tz);
  if (dMsg === dNow) return clock;
  if (dMsg === dayKey(now - 86400000, tz)) return `${t("yesterday")} ${clock}`;
  const sameYear = dMsg.slice(0, 4) === dNow.slice(0, 4);
  let date: string;
  try {
    date = new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en", {
      timeZone: tz || undefined,
      year: sameYear ? undefined : "numeric",
      month: sameYear ? "long" : "2-digit",
      day: sameYear ? "numeric" : "2-digit",
    }).format(ms);
  } catch {
    date = dMsg; // YYYY-MM-DD fallback
  }
  return `${date} ${clock}`;
}

/** The IANA timezones offered in Settings; "" = follow the browser. The chat stores
 *  absolute time and renders in the chosen zone, so this is a pure display choice. */
export const TIMEZONES = [
  "",
  "Asia/Shanghai",
  "Asia/Hong_Kong",
  "Asia/Taipei",
  "Asia/Tokyo",
  "Asia/Singapore",
  "Asia/Kolkata",
  "Asia/Dubai",
  "Europe/London",
  "Europe/Paris",
  "Europe/Moscow",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
  "UTC",
] as const;

/** A timezone's current UTC offset as "UTC+8" / "UTC-5:30" / "" (browser/auto). */
export function tzOffsetLabel(tz: string, now: number = Date.now()): string {
  if (!tz) return "";
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      timeZoneName: "shortOffset",
    }).formatToParts(now);
    const name = parts.find((p) => p.type === "timeZoneName")?.value || "";
    return name.replace(/^GMT/, "UTC") || "";
  } catch {
    return "";
  }
}

/** The localStorage key for the chat timezone display choice. */
export const TZ_STORAGE_KEY = "lm-timezone";

/** Read the saved chat timezone ("" = follow the browser). Safe in non-DOM envs. */
export function currentTimezone(): string {
  try {
    return localStorage.getItem(TZ_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

/** Relative "x min ago" text. app.js:140 timeAgo. `now` is injectable for tests. */
export function timeAgo(t: TFn, ts: number | null | undefined, now: number = Date.now()): string {
  if (!ts) return "";
  const s = Math.max(0, now / 1000 - ts);
  if (s < 90) return t("ago-just");
  if (s < 3600) return `${Math.round(s / 60)} ${t("ago-min")}`;
  if (s < 86400) return `${Math.round(s / 3600)} ${t("ago-hour")}`;
  return `${Math.round(s / 86400)} ${t("ago-day")}`;
}

/** HH:MM (24h) from a unix epoch (seconds). app.js:149 fmtClock. */
export function fmtClock(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Human byte size. app.js:153 fmtSize. */
export function fmtSize(bytes: number | string | null | undefined): string {
  const n = Number(bytes) || 0;
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return Math.round(n / 1024) + " KB";
  return n + " B";
}

/** Rough token estimate: CJK chars count 1, other text ~4 chars/token.
 *  app.js:160 estimateTokens. */
export function estimateTokens(text: string | null | undefined): number {
  const s = String(text || "");
  let cjk = 0;
  for (const ch of s) if (ch >= "一" && ch <= "鿿") cjk++;
  const other = Math.max(0, s.length - cjk);
  return cjk + Math.floor(other / 4);
}

/** Compact duration ("1m20s" / "5s" / "0.3s" / "<1s"). app.js:168 durationText. */
export function durationText(seconds: number | null | undefined): string {
  const s = Math.max(0, Number(seconds) || 0);
  if (s >= 60) return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
  if (s >= 10) return `${Math.round(s)}s`;
  if (s >= 1) return `${s.toFixed(1)}s`;
  return "<1s";
}

/** "Always-on" / "Chat mode" label for a mode. app.js:176 modeLabel. */
export function modeLabel(t: TFn, mode: string): string {
  return t(mode === "chat" ? "mode-chat" : "mode-live");
}

/** Deterministic palette bucket "p-0".."p-5" from a name. app.js:199 paletteClass. */
export function paletteClass(name: string): string {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return "p-" + (h % 6);
}

/** First letter glyph for an avatar fallback. app.js:204 glyphOf. */
export function glyphOf(name: string | null | undefined): string {
  return (name || "?").trim().slice(0, 1).toUpperCase();
}

/** The provider segment of a `provider/model` id (e.g. "openai/gpt-4o" → "openai"),
 *  with the ~/@ prefix of a self-registered Local/Custom endpoint stripped; "other"
 *  when there's no provider segment. Shared by the model pickers so their grouping of
 *  the same id agrees (ModelPane + the chat panel). */
export function providerOf(id: string): string {
  return (id.includes("/") ? id.split("/")[0] : "other").replace(/^[~@]/, "");
}

/** Compact count: 5473 → "5.5k", 1_200_000 → "1.2M". For market download/like badges. */
export function compactNum(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}
