/* The wire protocol — a TS mirror of src/lunamoth/protocol/{events,codec}.py.
 *
 * Six frozen event types keyed on a `type` discriminant (codec.py `_TYPES`).
 * PROTOCOL_VERSION is 1 and the format is additive-compatible: a decoder must
 * ignore unknown fields and tolerate unknown types (forward-compat with a newer
 * backend). There is no generated client; this hand-written union is the model. */

export const PROTOCOL_VERSION = 1;

export const SAY = "say";
export const MUSE = "muse";
export type Channel = "say" | "muse";

export interface TextDelta {
  type: "text";
  text: string;
  channel: Channel;
}
export interface ThinkDelta {
  type: "think";
  text: string;
}
export interface ToolStart {
  type: "tool_start";
  name: string;
  preview: string;
  index: number;
}
export interface ToolEnd {
  type: "tool_end";
  name: string;
  ok: boolean;
  duration: number;
  summary: string;
  index: number;
}
export interface Notice {
  type: "notice";
  kind: string;
  text: string;
}
export type ProtocolEvent =
  | TextDelta
  | ThinkDelta
  | ToolStart
  | ToolEnd
  | Notice;

/** Known event type tags (codec.py `_TYPES` keys). */
export const KNOWN_EVENT_TYPES = [
  "text",
  "think",
  "tool_start",
  "tool_end",
  "notice",
] as const;

/** Defaults mirror the frozen dataclass field defaults in events.py, so a
 *  sparse wire dict decodes to a fully-populated event. */
export function decodeEvent(data: Record<string, unknown>): ProtocolEvent | null {
  const type = data?.type;
  switch (type) {
    case "text":
      return { type, text: String(data.text ?? ""), channel: chan(data.channel) };
    case "think":
      return { type, text: String(data.text ?? "") };
    case "tool_start":
      return {
        type,
        name: String(data.name ?? ""),
        preview: String(data.preview ?? ""),
        index: num(data.index),
      };
    case "tool_end":
      return {
        type,
        name: String(data.name ?? ""),
        ok: data.ok === undefined ? true : Boolean(data.ok),
        duration: num(data.duration),
        summary: String(data.summary ?? ""),
        index: num(data.index),
      };
    case "notice":
      return { type, kind: String(data.kind ?? ""), text: String(data.text ?? "") };
    default:
      // Unknown type — tolerate (forward-compat). Caller drops a null.
      return null;
  }
}

function chan(v: unknown): Channel {
  return v === "muse" ? "muse" : "say";
}
function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}
