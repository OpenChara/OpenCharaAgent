/* Shared core for the visual-set editor — the pure (no-JSX) types, constants and
   helpers imported by BOTH VisualEditor and VisualSlot, so the two can't drift. */
import { assetUrl } from "../../rpc";
import type { TKey } from "../../i18n";
import { avatarSrc } from "./visual";
import type { DeckCard } from "./types";

export const ART_EXTS = ["png", "jpg", "jpeg", "webp"];
export const ART_UPLOAD_MAX = 16 * 1024 * 1024;
export const DEFAULT_KINDS = ["keyvisual", "avatar", "sprite", "stickers", "background"] as const;
export const REF_MAX = 3; // user reference images per generation

export type VisKind = "avatar" | "sprite" | "background" | "keyvisual" | "stickers";

export interface Ref {
  data_b64: string;
  mime: string;
  /** The card asset-library path (`assets/<name>`) when this reference is PERSISTED
   *  there — so removing it can delete the saved file. Absent for a not-yet-saved ref. */
  rel?: string;
}

export interface Brief {
  appearance?: string;
  style?: string;
  palette?: string;
  world?: string;
  theme?: string;
  [k: string]: unknown;
}

export const BRIEF_FIELDS: { k: string; labelKey: TKey; long?: boolean }[] = [
  { k: "appearance", labelKey: "vis-brief-appearance", long: true },
  { k: "style", labelKey: "vis-brief-style", long: true },
  { k: "palette", labelKey: "vis-brief-palette" },
  { k: "world", labelKey: "vis-brief-world", long: true },
  { k: "theme", labelKey: "vis-brief-theme" },
];

/* card.visual_job's ready payload — the backend already saved the result, so this
   carries the saved location, not raw bytes. */
export interface GenResult {
  status?: string;
  saved?: boolean;
  kind?: string;
  url?: string;        // sprite/background/keyvisual/avatar asset url
  options?: string[];  // the kind's full candidate gallery (after this generation)
  urls?: string[];     // stickers (the saved set)
  sheet_urls?: string[]; // stickers: the kept raw sheets
  data_uri?: string;   // avatar (inlined, for a fast preview)
  note?: string;
  matted?: boolean;    // false ⇒ a cut was wanted but skipped (engine not ready)
}

export type HubCall = <T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>;

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/* Kick card.visual_generate (returns a job_id) then poll card.visual_job until ready.
   A real failure surfaces as a rejected hubCall; "unknown" means the job expired. */
export async function runVisualJob(
  hubCall: HubCall,
  params: Record<string, unknown>,
  onTick?: (sec: number) => void,
): Promise<GenResult> {
  const sub = await hubCall<{ job_id?: string; status?: string }>("card.visual_generate", params, 30000);
  const jobId = sub?.job_id;
  if (!jobId) throw new Error("visual_generate did not return a job id");
  const t0 = Date.now();
  for (let i = 0; i < 400; i++) {
    await sleep(1500);
    onTick?.(Math.round((Date.now() - t0) / 1000));
    const st = await hubCall<GenResult>("card.visual_job", { job_id: jobId }, 30000);
    if (st.status === "ready") return st;
    if (st.status === "unknown") throw new Error("the generation job expired — please try again");
  }
  throw new Error("generation timed out");
}

export function initUrlFor(kind: VisKind, card: DeckCard): string {
  if (kind === "avatar") return avatarSrc(card);
  if (kind === "sprite") return card.sprite_url ? assetUrl(String(card.sprite_url)) : "";
  if (kind === "keyvisual") return card.keyvisual_url ? assetUrl(String(card.keyvisual_url)) : "";
  return card.bg_url ? assetUrl(String(card.bg_url)) : "";
}

// The candidate gallery urls for a kind (sprite/keyvisual/background keep a gallery;
// avatar + stickers don't use the single-image strip).
export function optionsFor(kind: VisKind, card: DeckCard): string[] {
  const raw =
    kind === "sprite" ? card.sprite_options
    : kind === "keyvisual" ? card.keyvisual_options
    : kind === "background" ? card.bg_options
    : kind === "avatar" ? card.avatar_options
    : undefined;
  return Array.isArray(raw) ? raw.map((u) => assetUrl(String(u))) : [];
}

/* The candidate FILENAME from an /asset?p=<abspath> url — what asset_select/remove want. */
export function assetName(url: string): string {
  try {
    const p = new URLSearchParams(url.split("?")[1] || "").get("p") || "";
    return decodeURIComponent(p).split("/").pop() || "";
  } catch {
    return "";
  }
}

/* The display name of a sticker = the slug in `<stem>.sticker.<slug>.png`. */
export function stickerSlug(url: string): string {
  const m = assetName(url).match(/\.sticker\.(.+)\.(png|jpe?g|webp)$/i);
  return m ? m[1] : "";
}
