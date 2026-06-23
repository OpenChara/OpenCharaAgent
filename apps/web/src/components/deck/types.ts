/* Deck card shapes — the subset of the Python `_card_entry` (hub.py:978) the
 * deck reads, plus the `card.read` full-card response. Unknown fields tolerated.
 * (Owned by the Deck track; lib/* stays untouched.) */

/** A deck-list card entry (hub.state `cards[]` / cards.list). */
export interface DeckCard {
  path: string;
  name: string;
  lang: string;
  tags?: string[];
  default?: boolean;
  world?: string;
  builtin?: boolean;
  draft?: boolean;
  frozen?: boolean;
  used_by?: string[];
  locked?: boolean;
  owner?: string;
  creator_notes?: string;
  tagline?: string;
  theme_color?: string;
  theme?: { primary?: string; secondary?: string };
  avatar_svg?: string;
  avatar_uri?: string;
  sprite_url?: string;
  bg_url?: string;
  keyvisual_url?: string;
  stickers_urls?: string[];
  sticker_sheets_urls?: string[];
  // The non-destructive candidate gallery per kind (selected = *_url above).
  sprite_options?: string[];
  bg_options?: string[];
  keyvisual_options?: string[];
  avatar_options?: string[];
  force_roleplay?: boolean;
  /** Legacy stance string ("actor"); read only as a back-compat fallback. */
  embodiment?: string;
  website?: boolean | string;
  [k: string]: unknown;
}

/** A lunamoth extensions block as `card.read` surfaces it. */
export interface CardExtLunamoth {
  tagline?: string;
  force_roleplay?: boolean;
  /** Legacy stance string ("actor"); read only as a back-compat fallback. */
  embodiment?: string;
  website?: boolean | string;
  user_name?: string;
  user_persona?: string;
  polaris?: string;
  theme?: { primary?: string; secondary?: string };
  theme_color?: string;
  avatar_svg?: string;
  origin?: string;
  [k: string]: unknown;
}

/** A world-book entry as `character_book.entries[]` carries it. */
export interface WorldBookEntry {
  keys?: string[];
  content?: string;
  constant?: boolean;
  enabled?: boolean;
  insertion_order?: number;
  [k: string]: unknown;
}

/** The `card.read` response (full card). */
export interface FullCard {
  name?: string;
  language?: string;
  description?: string;
  personality?: string;
  scenario?: string;
  first_mes?: string;
  creator_notes?: string;
  extensions?: { lunamoth?: CardExtLunamoth; [k: string]: unknown };
  character_book?: { name?: string; entries?: WorldBookEntry[] };
  /** The raw SillyTavern card object (present for editable JSON cards). */
  raw?: { name?: string; data?: Record<string, unknown>; [k: string]: unknown };
  [k: string]: unknown;
}

/** A model entry from models.list. */
export interface ModelInfo {
  id: string;
  tools?: boolean;
  writing?: boolean;
  vision?: boolean;
  [k: string]: unknown;
}

/** A toolpack entry from toolpacks.list. */
export interface ToolpackInfo {
  name: string;
  description?: string;
  tools?: string[];
  [k: string]: unknown;
}
