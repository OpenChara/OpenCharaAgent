/* Card / draft serialization — the PURE half of front/web/app.js's draft
 * pipeline: normalizeDraft (1010/2056), sectionText/putSection (2115/2123) and
 * the field-serialization logic from collectCardData (1950). The DOM-reading
 * shell of collectCardData stays in Track C; serializeCardFields here is its pure
 * core, taking plain string field values instead of contenteditable nodes. */

/** A world-book entry as the draft carries it (loose, pre-normalize input). */
export interface RawWorldEntry {
  keys?: string[];
  key?: string;
  content?: string;
  desc?: string;
  constant?: boolean;
}

/** A normalized world-book entry. */
export interface WorldEntry {
  keys: string[];
  content: string;
  constant: boolean;
}

/** A world-book entry for the structured editor / save path: the edited fields
 *  plus any passthrough (secondary_keys, selective, comment…) preserved as-is, so
 *  round-tripping through the editor never silently drops a power-user field. */
export interface WorldEntryFull {
  keys: string[];
  content: string;
  constant: boolean;
  enabled?: boolean;
  insertion_order?: number;
  [k: string]: unknown;
}

/** The theme dual-color. */
export interface Theme {
  primary: string;
  secondary: string;
}

/** A loose draft as it arrives from cards.draft / hub before normalization. */
export interface RawDraft {
  name?: string;
  user_name?: string;
  user_persona?: string;
  description?: string;
  appearance?: string;
  first_mes?: string;
  world_entries?: RawWorldEntry[];
  world?: RawWorldEntry[];
  seed_goals?: string[];
  goals?: string[];
  tagline?: string;
  theme?: { primary?: string; secondary?: string };
  theme_color?: string;
  avatar_svg?: string;
  pending_avatar?: unknown;
  force_roleplay?: boolean | string;
  /** Legacy stance string ("actor"|"literal"); bridged into force_roleplay. */
  embodiment?: string;
  website?: boolean | string;
  [k: string]: unknown;
}

/** A fully-normalized draft (every field present, theme as {primary,secondary}). */
export interface NormalizedDraft {
  name: string;
  user_name: string;
  user_persona: string;
  description: string;
  first_mes: string;
  world_entries: WorldEntry[];
  seed_goals: string[];
  tagline: string;
  theme: Theme;
  avatar_svg: string;
  pending_avatar: unknown;
  force_roleplay: boolean;
  website: boolean;
  [k: string]: unknown;
}

/** Normalize raw character_book entries (the card.read / draft shape) into the
 *  structured-editor type: keys/content/constant coerced, all other fields
 *  (secondary_keys, selective, comment, enabled, insertion_order…) preserved. */
export function toWorldEntries(raw: ReadonlyArray<unknown> | undefined): WorldEntryFull[] {
  const out: WorldEntryFull[] = [];
  for (const item of raw || []) {
    if (!item || typeof item !== "object") continue;
    const e = item as Record<string, unknown>;
    const keysRaw = e.keys ?? (e.key ? [e.key] : []);
    const keys = Array.isArray(keysRaw) ? keysRaw.map((k) => String(k)).filter(Boolean) : [];
    out.push({
      ...e,
      keys,
      content: String(e.content ?? e.desc ?? ""),
      constant: !!e.constant,
    });
  }
  return out;
}

const HEX6 = /^#[0-9a-fA-F]{6}$/;
const isHex = (v: unknown): boolean => HEX6.test(String(v || ""));

/** The deck's signature blue — the fallback primary when a card has no usable
 * theme color (mirrors the backend `_DEFAULT_THEME_PRIMARY`). */
export const DEFAULT_THEME_PRIMARY = "#5B9FD4";

/** Fill every field, fold legacy aliases, coerce the theme + force_roleplay.
 *  app.js:2056 normalizeDraft (verbatim semantics). */
export function normalizeDraft(d: RawDraft | null | undefined): NormalizedDraft {
  const draft: Record<string, unknown> = Object.assign({}, d || {});
  draft.name = String(draft.name || "");
  draft.user_name = String(draft.user_name || "");
  draft.user_persona = String(draft.user_persona || "");
  draft.description = String(draft.description || draft.appearance || "");
  draft.first_mes = String(draft.first_mes || "");
  if (!Array.isArray(draft.world_entries)) {
    const world = (draft.world as RawWorldEntry[] | undefined) || [];
    draft.world_entries = world.map((w) => ({
      keys: w.keys || (w.key ? [w.key] : []),
      content: w.content || w.desc || "",
      constant: !!w.constant,
    }));
  }
  if (!Array.isArray(draft.seed_goals))
    draft.seed_goals = Array.isArray(draft.goals) ? draft.goals : [];
  draft.tagline = String(draft.tagline || "");
  const th = (draft.theme && typeof draft.theme === "object" ? draft.theme : {}) as {
    primary?: string;
    secondary?: string;
  };
  const primary = isHex(th.primary)
    ? String(th.primary).toUpperCase()
    : isHex(draft.theme_color)
      ? String(draft.theme_color).toUpperCase()
      : DEFAULT_THEME_PRIMARY;
  const secondary = isHex(th.secondary) ? String(th.secondary).toUpperCase() : "";
  draft.theme = { primary, secondary };
  delete draft.theme_color;
  draft.avatar_svg = String(draft.avatar_svg || "");
  draft.pending_avatar = draft.pending_avatar || null;
  // The card FIELD is a boolean; accept a legacy `embodiment: "actor"` string.
  draft.force_roleplay =
    draft.force_roleplay === true ||
    draft.force_roleplay === "actor" ||
    draft.force_roleplay === "true" ||
    draft.embodiment === "actor";
  delete draft.embodiment;
  draft.website = draft.website === true || draft.website === "on";
  return draft as NormalizedDraft;
}

/** Serialize a draft section back to the editable plain-text form.
 *  app.js:2115 sectionText. */
export function sectionText(draft: NormalizedDraft, key: string): string {
  if (key === "world_entries") {
    return (draft.world_entries || [])
      .map((w) => `${(w.keys || []).join(", ")} — ${w.content || ""}${w.constant ? " [constant]" : ""}`)
      .join("\n");
  }
  if (key === "seed_goals") return (draft.seed_goals || []).join("\n");
  const v = draft[key];
  return v == null ? "" : String(v);
}

/** Parse a draft section's edited plain text back into the draft (mutates).
 *  app.js:2123 putSection. */
export function putSection(draft: Partial<NormalizedDraft>, key: string, text: string): void {
  if (key === "world_entries") {
    draft.world_entries = text
      .split("\n")
      .map((line): WorldEntry | null => {
        const constant = /\[(constant|常驻)\]/i.test(line);
        const clean = line.replace(/\[(constant|常驻)\]/gi, "").trim();
        const m = clean.split("—");
        return m.length > 1
          ? {
              keys: m[0]
                .split(/[,，]/)
                .map((s) => s.trim())
                .filter(Boolean),
              content: m.slice(1).join("—").trim(),
              constant,
            }
          : null;
      })
      .filter((w): w is WorldEntry => w !== null);
  } else if (key === "seed_goals") {
    draft.seed_goals = text
      .split(/\n|·/)
      .map((s) => s.trim())
      .filter(Boolean);
  } else {
    (draft as Record<string, unknown>)[key] = text;
    if (key === "name") draft.name = String(draft.name || "").trim();
  }
}

/** The plain field values that the wake/edit content step collects (the strings
 *  the contenteditable nodes hold), used to build the saved card payload.
 *
 *  Fields a surface ALWAYS edits are required strings ("" deletes the chara
 *  key). Fields a surface may NOT edit are optional: `undefined` means "leave the
 *  card's existing value alone" — the card editor preserves user_name/user_persona
 *  it never shows. This is the data-safety contract that lets BOTH save paths share
 *  this one serializer without clobbering fields they don't render. */
export interface CardFields {
  // Every field follows the same contract: undefined = NOT edited by this surface
  // (preserve the card's current value); "" = clear/delete; a value = set. This is
  // what makes it safe to save from a tab whose editors aren't mounted — an absent
  // editor sends undefined, never a blank that would wipe the soul.
  name?: string;
  description?: string;
  personality?: string;
  scenario?: string;
  first_mes?: string;
  tagline?: string;
  /** Polaris / north-star text (textContent of the field). */
  goals?: string;
  /** World book, in the "keys — content [constant]" line form (legacy text path). */
  world?: string;
  /** World book as structured entries (preferred — lossless, multi-line content).
   *  Takes precedence over `world` when present; undefined = world not edited. */
  worldEntries?: WorldEntryFull[];
  user_name?: string;
  user_persona?: string;
  /** data.creator_notes (NOT under chara). */
  creator_notes?: string;
}

/** A SillyTavern-ish card we serialize fields back into (the `data` block). */
export interface CardData {
  name?: string;
  description?: string;
  personality?: string;
  scenario?: string;
  first_mes?: string;
  extensions?: { chara?: Record<string, unknown>; [k: string]: unknown };
  character_book?: { name?: string; entries?: unknown[] };
  [k: string]: unknown;
}

/** The pure core of collectCardData (app.js:1950): fold edited field strings into
 *  a card `data` block. The DOM-reading wrapper (which pulls textContent off the
 *  contenteditable nodes) lives in the view; this takes the already-read strings,
 *  so the serialization rules (chara extensions, wishes split, world-book
 *  assembly, legacy-key migration) are testable. Mutates and returns `data`. */
export function serializeCardFields(
  data: CardData,
  fields: CardFields,
  charName: string,
): CardData {
  if (fields.name !== undefined) data.name = fields.name.trim() || charName;
  if (fields.description !== undefined) data.description = fields.description;
  if (fields.personality !== undefined) data.personality = fields.personality;
  if (fields.scenario !== undefined) data.scenario = fields.scenario;
  if (fields.first_mes !== undefined) data.first_mes = fields.first_mes;
  if (fields.creator_notes !== undefined) data.creator_notes = fields.creator_notes;
  data.extensions = data.extensions || {};
  const lm = (data.extensions.chara = data.extensions.chara || {});
  // undefined → leave the card's value alone (surface doesn't edit it); "" → delete.
  const setOrDel = (k: string, raw: string | undefined): void => {
    if (raw === undefined) return;
    const v = raw.trim();
    if (v) lm[k] = v;
    else delete lm[k];
  };
  setOrDel("user_name", fields.user_name);
  setOrDel("user_persona", fields.user_persona);
  setOrDel("tagline", fields.tagline);
  delete lm.wishes; // the old chara-mutable lists are gone
  delete lm.goals;
  // Polaris: a single north-star string (the field may span lines; stored as one).
  if (fields.goals !== undefined) {
    const polaris = fields.goals.trim();
    if (polaris) lm.polaris = polaris;
    else delete lm.polaris;
  }
  // World book — structured path (preferred): lossless per-entry save. Empty entries
  // (no keys or no content) are dropped; passthrough fields (secondary_keys, selective,
  // comment…) are preserved; insertion_order is re-indexed to the editor's order.
  if (fields.worldEntries !== undefined) {
    const entries = fields.worldEntries
      .map((w) => ({
        ...w,
        keys: (w.keys || []).map((k) => k.trim()).filter(Boolean),
        content: String(w.content ?? ""),
        constant: !!w.constant,
        enabled: w.enabled !== false,
      }))
      .filter((w) => w.keys.length > 0 && w.content.trim() !== "")
      .map((w, i) => ({ ...w, insertion_order: i }));
    // Always write an explicit book (even entries:[]) on the structured path. This save
    // goes through card.patch, where a MISSING key is PRESERVED, not cleared — so a
    // `delete` would silently keep stale entries on disk after the user removed them all.
    // Writing entries:[] makes a "clear everything" actually persist.
    data.character_book = {
      name: (data.character_book && data.character_book.name) || data.name,
      entries,
    };
    return data;
  }
  // World book — legacy text path: only rebuilt when the world editor was present.
  // undefined (its tab wasn't open) preserves the card's existing character_book.
  if (fields.world !== undefined) {
    const tmp: Partial<NormalizedDraft> = {};
    putSection(tmp, "world_entries", fields.world);
    const entries = (tmp.world_entries || []).map((w, i) => ({
      keys: w.keys,
      content: w.content,
      constant: w.constant,
      enabled: true,
      insertion_order: i,
    }));
    if (entries.length || (data.character_book && data.character_book.name)) {
      data.character_book = {
        name: (data.character_book && data.character_book.name) || data.name,
        entries,
      };
    } else {
      delete data.character_book;
    }
  }
  return data;
}

/** Does a string hold a real character-card JSON (vs free-text inspiration)? A cheap
 *  client check so the create box can offer a faithful as-is import instead of the AI
 *  rewrite — the backend (`cards.import_foreign`) validates authoritatively. Recognizes
 *  the SillyTavern V2/V3 `data` block, a V1 flat card, and character-tavern's flat
 *  `definition_*` API shape. Tolerant of half-typed text: anything that doesn't parse as
 *  a JSON object with a name + some persona is treated as plain inspiration. */
export function looksLikeCardJson(text: string): boolean {
  const s = text.trim();
  if (s.length < 2 || s[0] !== "{") return false;
  let o: Record<string, unknown>;
  try {
    o = JSON.parse(s) as Record<string, unknown>;
  } catch {
    return false;
  }
  if (!o || typeof o !== "object") return false;
  const d = (o.data && typeof o.data === "object" ? o.data : o) as Record<string, unknown>;
  const str = (k: string) => typeof d[k] === "string" && (d[k] as string).trim().length > 0;
  const hasName =
    str("name") || str("inChatName") || (typeof o.name === "string" && o.name.trim().length > 0);
  const hasPersona =
    str("description") ||
    str("personality") ||
    str("first_mes") ||
    str("scenario") ||
    str("definition_character_description") ||
    str("definition_first_message");
  return hasName && hasPersona;
}
