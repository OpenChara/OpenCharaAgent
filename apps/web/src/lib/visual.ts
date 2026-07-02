/* Per-chara chat visual prefs — ported from front/web/app.js
 * (VISUAL_DEFAULTS:335, visualKey:342, readVisualPrefs:346). Operator
 * presentation prefs kept in localStorage, scoped per open chat. Read +
 * write live here; the DOM-applying side (CSS vars) stays in the view.
 * The chat settings pane writes via writeVisualPrefs, which fires
 * VISUAL_PREFS_EVENT so the open chat re-reads and applies live.
 *
 * The JS read a global `state.chat.name` for the scope; here the chara name is an
 * explicit argument (null = the bare unscoped key, the original's no-chat case). */

export type SpritePos = "off" | "left" | "center" | "right";

export interface VisualPrefs {
  bgOn: boolean;
  veilOpacity: number;
  spriteOpacity: number;
  spritePos: SpritePos;
}

/** app.js:335 VISUAL_DEFAULTS. */
export const VISUAL_DEFAULTS: VisualPrefs = {
  bgOn: true,
  veilOpacity: 80,
  spriteOpacity: 16,
  spritePos: "right",
};

const SPRITE_POSITIONS: readonly SpritePos[] = ["off", "left", "center", "right"];

/** Scope a storage key by the open chat's name. app.js:342 visualKey. */
export function visualKey(base: string, charaName?: string | null): string {
  return charaName ? `${base}:${charaName}` : base;
}

/** Read the per-chara visual prefs from localStorage, falling back to the
 *  defaults for missing/invalid values. app.js:346 readVisualPrefs. */
export function readVisualPrefs(charaName?: string | null): VisualPrefs {
  const get = (k: string): string | null => {
    try {
      return localStorage.getItem(visualKey(k, charaName));
    } catch {
      return null;
    }
  };
  const num = (k: string, d: number): number => {
    // An ABSENT key must fall back to the default — NOT 0. `Number(null)===0`
    // (and `Number("")===0`) would otherwise pass the 0..100 range check and
    // silently read as 0, so e.g. the chat veil defaulted to fully transparent
    // instead of VISUAL_DEFAULTS.veilOpacity. Null-guard before coercing.
    const raw = get(k);
    if (raw === null || raw === "") return d;
    const v = Number(raw);
    return Number.isFinite(v) && v >= 0 && v <= 100 ? v : d;
  };
  let pos = (get("lm-sprite-pos") || VISUAL_DEFAULTS.spritePos) as SpritePos;
  if (!SPRITE_POSITIONS.includes(pos)) pos = VISUAL_DEFAULTS.spritePos;
  const bgRaw = get("lm-chat-bg-on");
  return {
    bgOn: bgRaw === null ? VISUAL_DEFAULTS.bgOn : bgRaw === "1",
    veilOpacity: num("lm-chat-veil-opacity", VISUAL_DEFAULTS.veilOpacity),
    spriteOpacity: num("lm-sprite-opacity", VISUAL_DEFAULTS.spriteOpacity),
    spritePos: pos,
  };
}

/** Fired on window after writeVisualPrefs so an open chat re-reads and applies. */
export const VISUAL_PREFS_EVENT = "lm-visual-prefs";

/** Write a partial set of per-chara visual prefs and notify listeners. The keys
 *  mirror readVisualPrefs exactly (same names, same per-chara scoping). */
export function writeVisualPrefs(charaName: string | null | undefined, patch: Partial<VisualPrefs>): void {
  const set = (k: string, v: string) => {
    try {
      localStorage.setItem(visualKey(k, charaName), v);
    } catch {
      /* private mode — the in-memory state the caller keeps still applies */
    }
  };
  if (patch.bgOn !== undefined) set("lm-chat-bg-on", patch.bgOn ? "1" : "0");
  if (patch.veilOpacity !== undefined) set("lm-chat-veil-opacity", String(patch.veilOpacity));
  if (patch.spriteOpacity !== undefined) set("lm-sprite-opacity", String(patch.spriteOpacity));
  if (patch.spritePos !== undefined) set("lm-sprite-pos", patch.spritePos);
  try {
    window.dispatchEvent(new Event(VISUAL_PREFS_EVENT));
  } catch {
    /* non-browser environment */
  }
}
