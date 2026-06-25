/* Framework-agnostic formatters — ported verbatim from front/web/app.js
 * (timeAgo / fmtClock / fmtSize / estimateTokens / durationText / modeLabel /
 * paletteClass / glyphOf). No DOM, no el(). Functions that surface UI copy take
 * the translator `t` as their first argument (the JS originals reached a global
 * `t`); everything else is pure math/string work. */

import type { TFn } from "../i18n";

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
