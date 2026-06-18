/* The chat stream accumulator — a framework-free, unit-testable port of the
 * chat.js ChatController stream state machine (onEvent ~516, the
 * `this.cur = {kind,node,textNode,raw}` accumulator + closeCurrent, the tool
 * group tally, the super-chat flag, restored-history rendering).
 *
 * chat.js mutated DOM nodes in place; here the "stream" is an immutable-ish array
 * of typed `StreamItem`s. The model keeps a private `cur` cursor describing the
 * item currently being appended to. The contract preserved verbatim:
 *
 *  - Consecutive deltas of the SAME `kind` (say / super / think) append to
 *    ONE item's accumulated text. A `kind` CHANGE first closes the current item
 *    (which markdown-renders say/super text) and opens a fresh one. The `muse`
 *    channel is NOT a display distinction: it only tells the backend messaging
 *    gateway whether to forward a turn externally. On the desktop there is no
 *    gateway, so muse text renders identically to say (it accumulates into a
 *    `say` item — a separate self-work turn is still its own message because the
 *    turn boundary closes the previous item, but with no channel-based styling).
 *  - A run of consecutive tool calls folds into ONE `tool-group` item; its
 *    summary is a hermes-style tally ("read 1 file · ran 2 commands"). Any
 *    non-tool item (text/think/system) breaks the group.
 *  - The `speak` tool ending ok sets a pending-super flag; the NEXT say-channel
 *    text opens as a bright super-chat bubble (the chara reaching out).
 *  - think deltas accumulate into the SINGLE think block for the turn
 *    (turnThink), re-used if the cursor briefly left it (e.g. interleaved tool
 *    calls). finalize() collapses any streaming think blocks.
 *
 * Subtle behaviors interpreted from chat.js, documented here:
 *  - super-chat read/unread is a CSS class applied later by the view from its own
 *    read watermark (Chat.tsx superReadTs); the model only records the super's `ts`.
 *  - closeCurrent markdown-renders say|super; think stays plain text. Muse text
 *    is folded into say, so it is markdown-rendered like any other message.
 *  - tool_end with no matching tool_start (orphan) still counts in the tally and
 *    appends a chip (chat.js showToolEnd's `!rec` branch).
 */

import { durationText } from "../../lib/format";

export type ItemKind =
  | "user"
  | "say"
  | "super"
  | "think"
  | "tool-group"
  | "system"
  | "permission"
  | "clarify";

export interface ToolChip {
  /** tool_start index:name key, used to match the tool_end. */
  key: string;
  name: string;
  preview: string;
  running: boolean;
  ok: boolean;
  duration: number;
  summary: string;
}

export interface ToolGroupItem {
  id: string;
  kind: "tool-group";
  chips: ToolChip[];
  /** tool-name -> count, for the compact summary line. */
  tally: Record<string, number>;
  fails: number;
}

export interface TextItem {
  id: string;
  kind: "say" | "super";
  raw: string;
  /** super-chat only: the speak timestamp (epoch s). Read/unread is derived in the
   *  view from its own watermark (Chat.tsx superReadTs) — NOT stored on the item. */
  ts?: number;
}

export interface ThinkItem {
  id: string;
  kind: "think";
  raw: string;
  tokens: number;
  streaming: boolean;
}

export interface UserItem {
  id: string;
  kind: "user";
  text: string;
  /** [{name,mime,size,isImage,url}] — local preview attachments. */
  atts: UserAttachment[];
  queued?: boolean;
  /** an incoming peer (gateway) message — shows a "via X" tag. */
  via?: string;
}

export interface UserAttachment {
  name: string;
  mime: string;
  size: number;
  isImage: boolean;
  url: string;
}

export interface SystemItem {
  id: string;
  kind: "system";
  text: string;
  cls?: string;
}

export interface PermissionItem {
  id: string;
  kind: "permission";
  askId: string;
  reason: string;
  title: string;
}

export interface ClarifyItem {
  id: string;
  kind: "clarify";
  askId: string;
  question: string;
  choices: string[];
}

export type StreamItem =
  | UserItem
  | TextItem
  | ThinkItem
  | ToolGroupItem
  | SystemItem
  | PermissionItem
  | ClarifyItem;

/* Bucketed tool verbs for the compact summary — chat.js _toolBucket. Returns the
   i18n key for the bucket, or null (→ "used <name> N×"). */
export function toolBucket(name: string): string | null {
  if (name === "read_file" || name === "list_files") return "tools-read";
  if (name === "write_file" || name === "patch" || name === "edit_file" || name === "send_file")
    return "tools-edited";
  if (name === "search_files" || name === "search" || name === "grep") return "tools-searched";
  if (name === "terminal" || name === "execute_code" || name === "process") return "tools-ran";
  if (name.startsWith("browser") || name === "browse") return "tools-browsed";
  if (name.startsWith("web") || name === "fetch") return "tools-web";
  return null;
}

/** A translator-shaped function (the i18n `t`). */
export type Translate = (key: string, vars?: Record<string, string | number>) => string;

/** Compact summary for a tool tally — chat.js summarizeToolTally. */
export function summarizeToolTally(
  t: Translate,
  tally: Record<string, number>,
  fails: number,
): string {
  const byKey: Record<string, number> = {};
  const byName: Record<string, number> = {};
  for (const name of Object.keys(tally)) {
    const n = tally[name];
    const key = toolBucket(name);
    if (key) byKey[key] = (byKey[key] || 0) + n;
    else byName[name] = (byName[name] || 0) + n;
  }
  const parts: string[] = [];
  for (const key of Object.keys(byKey)) parts.push(t(key, { n: byKey[key] }));
  for (const name of Object.keys(byName)) parts.push(t("tools-used", { name, n: byName[name] }));
  let s = parts.join(" · ");
  if (fails) s += ` · ${t("tools-failed", { n: fails })}`;
  return s || t("st-creating");
}

/** Rough token estimate (mirror of format.estimateTokens — duplicated tiny so the
 *  model has no React/lib import beyond durationText; kept consistent). */
function estimateTokens(text: string): number {
  let cjk = 0;
  for (const ch of text) if (ch >= "一" && ch <= "鿿") cjk++;
  const other = Math.max(0, text.length - cjk);
  return cjk + Math.floor(other / 4);
}

interface Cursor {
  kind: ItemKind | null;
  item: TextItem | ThinkItem | null;
}

/* The stream model: feed protocol events (already decoded), read `items`. It
   intentionally mirrors chat.js's imperative cursor rather than a pure reducer,
   because the in-place text accumulation is the load-bearing behavior. */
export class StreamModel {
  items: StreamItem[] = [];
  private cur: Cursor = { kind: null, item: null };
  private toolGroup: ToolGroupItem | null = null;
  private turnThink: ThinkItem | null = null;
  private activeTools = new Map<string, ToolChip>();
  private pendingSuper = false;
  // Item-id counter, INSTANCE-scoped (not module-global): each model numbers from
  // 0, so replaying identical restored history yields identical ids — stable React
  // keys keep a restored ThinkBlock/ToolGroup's local open/scroll state across
  // re-renders, and tests don't leak ids into each other.
  private seq = 0;
  private nextId(): string {
    return `i${++this.seq}`;
  }

  reset(): void {
    this.items = [];
    this.cur = { kind: null, item: null };
    this.toolGroup = null;
    this.turnThink = null;
    this.activeTools.clear();
    this.pendingSuper = false;
    this.seq = 0;
  }

  /* ---- the live event dispatch (chat.js onEvent) ---- */
  // Returns whether `speak` just ok'd (so the caller can flag a notification);
  // the work-state phase the caller surfaces is derived separately.
  // The `channel` (say|muse) is a backend-gateway forwarding hint, NOT a display
  // distinction — on the desktop both render identically. Muse text accumulates
  // into a normal `say` item; a self-work turn is still its own message because
  // the turn boundary closes the previous item. (A pending super-chat only
  // applies to the say channel — a muse turn never opens a super bubble.)
  pushText(text: string, channel: "say" | "muse"): void {
    const isSuper = channel === "say" && this.pendingSuper;
    this.pendingSuper = false;
    this.appendSay(text, isSuper);
  }

  pushThink(text: string): void {
    this.appendThink(text);
  }

  pushToolStart(name: string, preview: string, index: number): void {
    this.closeCurrent();
    const group = this.ensureToolGroup();
    this.tally(name);
    const key = `${index}:${name}`;
    const chip: ToolChip = { key, name, preview, running: true, ok: true, duration: 0, summary: "" };
    group.chips.push(chip);
    this.activeTools.set(key, chip);
  }

  pushToolEnd(name: string, ok: boolean, duration: number, summary: string, index: number): void {
    const key = `${index}:${name}`;
    let chip = this.activeTools.get(key);
    if (!chip) {
      // orphan end (no matching start) — still counts + gets a chip
      const group = this.ensureToolGroup();
      this.tally(name);
      chip = { key, name, preview: "", running: true, ok: true, duration: 0, summary: "" };
      group.chips.push(chip);
    }
    chip.running = false;
    chip.ok = ok;
    chip.duration = duration;
    chip.summary = summary;
    if (!ok && this.toolGroup) this.toolGroup.fails += 1;
    this.activeTools.delete(key);
    if (name === "speak" && ok) this.pendingSuper = true;
  }

  pushNotice(text: string): void {
    this.systemLine(text);
  }

  /* ---- user / peer / queued bubbles ---- */
  pushUser(text: string, atts: UserAttachment[], opts?: { queued?: boolean; via?: string }): string {
    this.closeCurrent();
    const id = this.nextId();
    this.items.push({ id, kind: "user", text, atts, queued: opts?.queued, via: opts?.via });
    return id;
  }

  removeItem(id: string): void {
    this.items = this.items.filter((it) => it.id !== id);
  }

  /* ---- inline permission / clarify boxes ---- */
  pushPermission(askId: string, title: string, reason: string): void {
    this.items.push({ id: this.nextId(), kind: "permission", askId, title, reason });
  }
  pushClarify(askId: string, question: string, choices: string[]): void {
    this.items.push({ id: this.nextId(), kind: "clarify", askId, question, choices });
  }
  resolveAsk(askId: string): void {
    this.items = this.items.filter(
      (it) =>
        !((it.kind === "permission" || it.kind === "clarify") && (it as PermissionItem | ClarifyItem).askId === askId),
    );
  }

  /* ---- system note line ---- */
  systemLine(text: string, cls?: string): void {
    if (!text) return;
    this.closeCurrent();
    this.breakToolGroup();
    this.items.push({ id: this.nextId(), kind: "system", text: String(text).slice(0, 240), cls });
  }

  /* ---- finalize a turn (chat.js finalize) ---- */
  finalize(): void {
    this.closeCurrent();
    for (const it of this.items) if (it.kind === "think") it.streaming = false;
    this.activeTools.clear();
    this.breakToolGroup();
    this.pendingSuper = false;
    this.turnThink = null;
  }

  /* ---- the accumulator internals (chat.js appendCharText/…) ---- */
  private appendSay(text: string, isSuper: boolean, tsOverride?: number): void {
    const kind: "say" | "super" = isSuper ? "super" : "say";
    if (this.cur.kind !== kind) {
      this.closeCurrent();
      this.breakToolGroup();
      // Restore uses the message's recorded ts (chat.js `m.ts || now`) so an
      // already-read historical super-chat doesn't re-render as unread.
      const ts = tsOverride ?? Date.now() / 1000;
      const item: TextItem =
        kind === "super"
          ? { id: this.nextId(), kind, raw: "", ts }
          : { id: this.nextId(), kind, raw: "" };
      this.items.push(item);
      this.cur = { kind, item };
    }
    const item = this.cur.item as TextItem;
    item.raw += text;
  }

  private appendThink(text: string): void {
    if (this.cur.kind !== "think") {
      this.closeCurrent();
      this.breakToolGroup();
      if (this.turnThink && this.items.includes(this.turnThink)) {
        this.cur = { kind: "think", item: this.turnThink };
      } else {
        const item: ThinkItem = { id: this.nextId(), kind: "think", raw: "", tokens: 0, streaming: true };
        this.items.push(item);
        this.turnThink = item;
        this.cur = { kind: "think", item };
      }
    }
    const item = this.cur.item as ThinkItem;
    item.raw += text;
    item.tokens = item.raw ? Math.max(1, estimateTokens(item.raw)) : 0;
    item.streaming = true;
  }

  /** Close the current text/think item. say|super get their raw kept as-is; the
   *  view markdown-renders say|super (chat.js closeCurrent's mdRender). */
  closeCurrent(): void {
    this.cur = { kind: null, item: null };
  }

  private ensureToolGroup(): ToolGroupItem {
    if (this.toolGroup && this.items.includes(this.toolGroup)) return this.toolGroup;
    this.closeCurrent();
    const group: ToolGroupItem = { id: this.nextId(), kind: "tool-group", chips: [], tally: {}, fails: 0 };
    this.items.push(group);
    this.toolGroup = group;
    return group;
  }

  private breakToolGroup(): void {
    this.toolGroup = null;
  }

  private tally(name: string): void {
    if (!this.toolGroup) return;
    this.toolGroup.tally[name] = (this.toolGroup.tally[name] || 0) + 1;
  }

  /* ---- restored-history rendering (chat.js renderRestored) ---- */
  // A faithful-but-compact subset: user bubbles, system lines, assistant text
  // (say + super from speak tool_calls), think blocks, and tool-call chips.
  renderRestored(messages: RestoredMessage[]): void {
    const restoreChips = new Map<string, ToolChip>();
    for (const m of messages.slice(-80)) {
      if (!m) continue;
      const content = typeof m.content === "string" ? m.content : "";
      const hasText = content.trim().length > 0;
      if (m.role === "user") {
        if (!hasText) continue;
        this.pushUser(content, []);
      } else if (m.role === "system") {
        if (hasText && m.kind !== "summary") this.systemLine(content);
      } else if (m.role === "tool") {
        if (hasText) this.restoreToolResult(m, restoreChips);
      } else if (m.role === "assistant") {
        const reasoning =
          typeof m.reasoning_content === "string" ? m.reasoning_content.trim() : "";
        if (reasoning) {
          this.closeCurrent();
          this.breakToolGroup();
          this.items.push({
            id: this.nextId(),
            kind: "think",
            raw: reasoning,
            tokens: Math.max(1, estimateTokens(reasoning)),
            streaming: false,
          });
        }
        if (hasText) {
          this.appendSay(content, false);
          this.closeCurrent();
        }
        const calls = Array.isArray(m.tool_calls) ? m.tool_calls : [];
        for (const tc of calls) {
          const fn = tc && tc.function;
          if (!fn) continue;
          if (fn.name === "speak") continue;
          this.restoreToolCall(fn, tc.id, restoreChips);
        }
        for (const speak of speakTextsFromMessage(m)) {
          this.pendingSuper = true;
          this.appendSay(speak, true, m.ts);
          this.closeCurrent();
        }
      }
    }
    this.closeCurrent();
    this.breakToolGroup();
  }

  private restoreToolCall(
    fn: { name?: string; arguments?: unknown },
    callId: string | undefined,
    chips: Map<string, ToolChip>,
  ): void {
    const name = fn.name || "?";
    const group = this.ensureToolGroup();
    this.tally(name);
    const chip: ToolChip = {
      key: callId || this.nextId(),
      name,
      preview: "",
      running: false,
      ok: true,
      duration: 0,
      summary: "",
    };
    group.chips.push(chip);
    if (callId) chips.set(callId, chip);
  }

  private restoreToolResult(m: RestoredMessage, chips: Map<string, ToolChip>): void {
    const text = String(m.content || "").slice(0, 600);
    const rec = m.tool_call_id ? chips.get(m.tool_call_id) : undefined;
    if (rec) {
      rec.summary = rec.summary ? `${rec.summary}\n\n→ ${text}` : `→ ${text}`;
      return;
    }
    const group = this.ensureToolGroup();
    group.chips.push({
      key: this.nextId(),
      name: "result",
      preview: "",
      running: false,
      ok: true,
      duration: 0,
      summary: text,
    });
  }
}

export interface RestoredMessage {
  role?: string;
  content?: unknown;
  kind?: string;
  reasoning_content?: string;
  tool_calls?: { id?: string; function?: { name?: string; arguments?: unknown } }[];
  tool_call_id?: string;
  ts?: number;
}

/* speak tool-call argument extraction — chat.js speakTextsFromMessage (app.js
   defines it globally). The speak tool's text arg surfaces as a super-chat. */
export function speakTextsFromMessage(m: RestoredMessage): string[] {
  const out: string[] = [];
  const calls = Array.isArray(m.tool_calls) ? m.tool_calls : [];
  for (const tc of calls) {
    const fn = tc && tc.function;
    if (!fn || fn.name !== "speak") continue;
    let args: unknown = fn.arguments;
    if (typeof args === "string") {
      try {
        args = JSON.parse(args);
      } catch {
        continue;
      }
    }
    const text = args && typeof args === "object" ? (args as { text?: unknown }).text : undefined;
    if (typeof text === "string" && text.trim()) out.push(text);
  }
  return out;
}

/** A chip's display label (chat.js showToolEnd / restoreToolCall). */
export function chipLabel(chip: ToolChip): string {
  if (chip.running) return `⚙ ${chip.name}`;
  if (chip.name === "result") return "⚙ result";
  return `⚙ ${chip.name} ${chip.ok ? "✓" : "✗"} · ${durationText(chip.duration)}`;
}
