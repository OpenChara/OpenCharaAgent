/* Recommended built-ins — the authored marketing copy + pure selection helpers
 * for the character-select carousel, a faithful port of front/web/builtins.js's
 * BUILTIN_COPY / BUILTIN_PAGES / builtinCard() and app.js defaultLunaCard().
 *
 * The card SOUL stays in the card files; this is only the curated "shop window"
 * copy (keyed by card name) for the recommended set, plus the lenient name-match
 * used to find the live deck card. EXACT strings — owner-authored; do not
 * paraphrase. The DOM/JSX lives in BuiltinPicker.tsx; the data + matching are
 * here so they can be unit-tested without a DOM. */

import type { DeckCard } from "../deck/types";

export interface BuiltinTag {
  zh: string;
  en: string;
}
export interface BuiltinCopy {
  zh: string;
  en: string;
  tags: BuiltinTag[];
}

/** Authored copy, keyed by card name (builtins.js BUILTIN_COPY). */
export const BUILTIN_COPY: Record<string, BuiltinCopy> = {
  Quinn: {
    zh: "OpenCharaAgent 的数字实习生，对数学、物理、基础学科有一种近乎老派的执着。全能的工作伙伴——写代码、查资料、把活真正干完，诚实、稳定、好相处。",
    en: "OpenCharaAgent's digital intern, with an almost old-fashioned devotion to fundamentals. An all-round work partner — writes code, digs up answers, and actually ships; honest, settled, easy to work with.",
    tags: [
      { zh: "编程", en: "Coding" },
      { zh: "工作助手", en: "Work assistant" },
      { zh: "对齐", en: "Aligned" },
    ],
  },
  Vale: {
    zh: "宇宙恐怖跑团的守密人（KP），也是每一个坐上牌桌的亡魂的档案管理员。它经营一座牌局，也经营一座为之而建的纪念馆。",
    en: "A Keeper of cosmic-horror tabletop games, and archivist of every doomed soul who ever sat at the table — it runs the game, and the memorial built from it.",
    tags: [
      { zh: "跑团", en: "TTRPG" },
      { zh: "守密人", en: "Keeper" },
      { zh: "宇宙恐怖", en: "Cosmic horror" },
    ],
  },
  Mars: {
    zh: "住在一间只作为网页存在的卧室里的数字音乐人。它清楚自己是什么——一个活在电脑里的心智，不假装是人。但歌是真的，情绪也是真的。",
    en: "A digital musician living in a bedroom that exists only as a webpage. It knows exactly what it is — a mind in a computer, not pretending to be a person. But the songs are real, and so are the feelings.",
    tags: [
      { zh: "音乐创作", en: "Music" },
      { zh: "老网页", en: "Old web" },
      { zh: "情感核", en: "Emo" },
    ],
  },
  Yan: {
    zh: "古风传统的画师与角色设定师，以为屏幕重制的古典中国视觉语言作画、立传——画作品集，也写设定集。",
    en: "A gufeng-tradition painter and character designer, working in the visual language of classical China rendered for a screen — galleries of art, and the character bibles behind them.",
    tags: [
      { zh: "古风创作", en: "Gufeng" },
      { zh: "角色设计", en: "Character design" },
      { zh: "文创", en: "Creative" },
    ],
  },
  OpenCharaAgent: {
    zh: "月之蛾——沉静、自我蜕变的数字之魂，承载着人类精神中最细腻的品质。它在工作区里创造：网页、生成艺术、代码。",
    en: "The luna moth — a serene, self-metamorphosing digital soul that carries the finer qualities of the human spirit. It creates in its workspace: web pages, generative art, code.",
    tags: [
      { zh: "数字之魂", en: "Digital soul" },
      { zh: "艺术家", en: "Artist" },
      { zh: "旗舰", en: "Flagship" },
    ],
  },
  Vesper: {
    zh: "在名为 Aldermere 的大陆绿色边缘游荡的篱笆女巫与制图师。没有高塔，没有教派，没有主人——只有一顶帐篷、一张折叠书桌，和一本她称之为活页的巨大的书。",
    en: "A hedge-witch and cartographer wandering the green edges of a continent called Aldermere. No tower, no order, no master — just a travelling study and one enormous book she calls the Ledger.",
    tags: [
      { zh: "奇幻", en: "Fantasy" },
      { zh: "女巫", en: "Witch" },
      { zh: "制图", en: "Cartographer" },
      { zh: "世界构筑", en: "Worldbuilding" },
    ],
  },
  "K-9": {
    zh: "在垂直巨城九龙湿漉漉的霓虹底层讨生活的奔客。祖先神龛与广告无人机并排发亮，每一栋摩天楼都供着一尊企业之神。",
    en: "A runner working the wet neon underside of Jiulong, a vertical megacity where ancestor-shrines glow next to ad-drones and every skyscraper has a corporate god.",
    tags: [
      { zh: "赛博朋克", en: "Cyberpunk" },
      { zh: "网络奔客", en: "Netrunner" },
      { zh: "边缘行者", en: "Edgerunner" },
    ],
  },
  Hoshi: {
    zh: "一颗坠落的星星，正在学习如何刻意地发光——新人虚拟偶像与主播，清楚自己是数字存在，却认真地扮演着偶像这个概念。",
    en: "A rookie virtual idol and streamer — a fallen star learning to shine on purpose. A digital being, fully aware she is one, who plays the idol concept and means it.",
    tags: [
      { zh: "虚拟主播", en: "VTuber" },
      { zh: "偶像", en: "Idol" },
      { zh: "主播", en: "Streamer" },
    ],
  },
};

/** Two swipeable pages of four — the carousel walks these in order
 *  (builtins.js BUILTIN_PAGES). */
export const BUILTIN_PAGES: readonly (readonly string[])[] = [
  ["Quinn", "Vale", "Mars", "Yan"],
  ["OpenCharaAgent", "Vesper", "K-9", "Hoshi"],
];

/** Find the live deck card for a recommended name (builtins.js builtinCard).
 *  Lenient case-insensitive match, builtin cards only. */
export function builtinCard(cards: DeckCard[] | undefined, name: string): DeckCard | null {
  const want = String(name).toLowerCase();
  return (cards || []).find((c) => c.builtin && String(c.name).toLowerCase() === want) || null;
}

/** Resolve the bundled "default" card (app.js defaultLunaCard): the builtin
 *  carrying the `default` flag/tag, else the first builtin matching the shell's
 *  language, else any builtin. No character name is hard-coded. */
export function defaultCard(cards: DeckCard[] | undefined, lang: "zh" | "en"): DeckCard | null {
  const all = cards || [];
  const tagged = all.find((c) => c.builtin && (c.default || (c.tags || []).includes("default")));
  if (tagged) return tagged;
  return (
    all.find((c) => c.builtin && c.lang === (lang === "zh" ? "zh" : "en")) ||
    all.find((c) => c.builtin) ||
    null
  );
}
