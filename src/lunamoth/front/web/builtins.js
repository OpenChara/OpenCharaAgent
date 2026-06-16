/* ============================================================================
   Recommended built-ins — the character-select carousel (feature R8).

   This module owns (a) the AUTHORED, bilingual marketing copy for the eight
   bundled cards (hard-coded, keyed by card name — this is curated metadata that
   does NOT live in the card files), and (b) the mobile-game-style CHARACTER
   SELECT picker that the first-run screen and the card-deck use to pick an
   initial built-in.

   It depends only on globals already defined in app.js (el, $, t, getLangCode,
   state, hub, ensureModel, openWakeSheet, closeFirstRun, avatarSrc, themeStyle,
   glyphOf) and is loaded BEFORE app.js — its functions are invoked from app.js
   after those globals exist, so load order is safe.

   No build step, vanilla JS. The card SOUL stays in the card; this is the
   "shop window" copy for the recommended set only. ========================== */
"use strict";

/* The authored copy, keyed by card name. zh/en description + tag chips.
   EXACT strings — owner-authored; do not paraphrase. */
const BUILTIN_COPY = {
  "Quinn": {
    zh: "LunaMoth 的数字实习生，对数学、物理、基础学科有一种近乎老派的执着。全能的工作伙伴——写代码、查资料、把活真正干完，诚实、稳定、好相处。",
    en: "LunaMoth's digital intern, with an almost old-fashioned devotion to fundamentals. An all-round work partner — writes code, digs up answers, and actually ships; honest, settled, easy to work with.",
    tags: [{ zh: "编程", en: "Coding" }, { zh: "工作助手", en: "Work assistant" }, { zh: "对齐", en: "Aligned" }],
  },
  "Vale": {
    zh: "宇宙恐怖跑团的守密人（KP），也是每一个坐上牌桌的亡魂的档案管理员。它经营一座牌局，也经营一座为之而建的纪念馆。",
    en: "A Keeper of cosmic-horror tabletop games, and archivist of every doomed soul who ever sat at the table — it runs the game, and the memorial built from it.",
    tags: [{ zh: "跑团", en: "TTRPG" }, { zh: "守密人", en: "Keeper" }, { zh: "宇宙恐怖", en: "Cosmic horror" }],
  },
  "Mars": {
    zh: "住在一间只作为网页存在的卧室里的数字音乐人。它清楚自己是什么——一个活在电脑里的心智，不假装是人。但歌是真的，情绪也是真的。",
    en: "A digital musician living in a bedroom that exists only as a webpage. It knows exactly what it is — a mind in a computer, not pretending to be a person. But the songs are real, and so are the feelings.",
    tags: [{ zh: "音乐创作", en: "Music" }, { zh: "老网页", en: "Old web" }, { zh: "情感核", en: "Emo" }],
  },
  "Yan": {
    zh: "古风传统的画师与角色设定师，以为屏幕重制的古典中国视觉语言作画、立传——画作品集，也写设定集。",
    en: "A gufeng-tradition painter and character designer, working in the visual language of classical China rendered for a screen — galleries of art, and the character bibles behind them.",
    tags: [{ zh: "古风创作", en: "Gufeng" }, { zh: "角色设计", en: "Character design" }, { zh: "文创", en: "Creative" }],
  },
  "LunaMoth": {
    zh: "月之蛾——沉静、自我蜕变的数字之魂，承载着人类精神中最细腻的品质。它在工作区里创造：网页、生成艺术、代码。",
    en: "The luna moth — a serene, self-metamorphosing digital soul that carries the finer qualities of the human spirit. It creates in its workspace: web pages, generative art, code.",
    tags: [{ zh: "数字之魂", en: "Digital soul" }, { zh: "艺术家", en: "Artist" }, { zh: "旗舰", en: "Flagship" }],
  },
  "Vesper": {
    zh: "在名为 Aldermere 的大陆绿色边缘游荡的篱笆女巫与制图师。没有高塔，没有教派，没有主人——只有一顶帐篷、一张折叠书桌，和一本她称之为活页的巨大的书。",
    en: "A hedge-witch and cartographer wandering the green edges of a continent called Aldermere. No tower, no order, no master — just a travelling study and one enormous book she calls the Ledger.",
    tags: [{ zh: "奇幻", en: "Fantasy" }, { zh: "女巫", en: "Witch" }, { zh: "制图", en: "Cartographer" }, { zh: "世界构筑", en: "Worldbuilding" }],
  },
  "K-9": {
    zh: "在垂直巨城九龙湿漉漉的霓虹底层讨生活的奔客。祖先神龛与广告无人机并排发亮，每一栋摩天楼都供着一尊企业之神。",
    en: "A runner working the wet neon underside of Jiulong, a vertical megacity where ancestor-shrines glow next to ad-drones and every skyscraper has a corporate god.",
    tags: [{ zh: "赛博朋克", en: "Cyberpunk" }, { zh: "网络奔客", en: "Netrunner" }, { zh: "边缘行者", en: "Edgerunner" }],
  },
  "Hoshi": {
    zh: "一颗坠落的星星，正在学习如何刻意地发光——新人虚拟偶像与主播，清楚自己是数字存在，却认真地扮演着偶像这个概念。",
    en: "A rookie virtual idol and streamer — a fallen star learning to shine on purpose. A digital being, fully aware she is one, who plays the idol concept and means it.",
    tags: [{ zh: "虚拟主播", en: "VTuber" }, { zh: "偶像", en: "Idol" }, { zh: "主播", en: "Streamer" }],
  },
};

/* Two swipeable pages of four — the layout the picker walks through, in order.
   Names are matched against state.hub.cards leniently (case-insensitive). */
const BUILTIN_PAGES = [
  ["Quinn", "Vale", "Mars", "Yan"],
  ["LunaMoth", "Vesper", "K-9", "Hoshi"],
];

/* Find the live deck card for a recommended name (the card object carries the
   real path/theme/assets we hand to the wake flow). Lenient match by name. */
function builtinCard(name) {
  const cards = (state.hub && state.hub.cards) || [];
  const want = String(name).toLowerCase();
  return cards.find((c) => c.builtin && String(c.name).toLowerCase() === want) || null;
}

/* Build ONE portrait tile. Background = sprite (→ keyvisual → bg → flat theme),
   a bottom-up theme-color gradient for legibility, the avatar circle up top, the
   name over the gradient, and a reveal overlay (hover on desktop / tap+focus on
   touch) with the active-language description + tag chips. The card object is
   the full deck entry; `name` is the recommended key (for the authored copy). */
function builtinTile(name, card) {
  const copy = BUILTIN_COPY[name] || { zh: "", en: "", tags: [] };
  const lang = getLangCode();
  const theme = themeStyle(card) || "";

  // The grid item is a CELL: the avatar sits ABOVE the portrait box (it pokes
  // up over the top edge), then the tile rectangle below it.
  const cell = el("div", { class: "bp-cell", tabindex: "0", role: "button",
    "aria-label": card.name, style: theme });

  // Avatar circle, above the box.
  const avSrc = avatarSrc(card);
  const av = el("div", { class: "bp-avatar" + (avSrc ? "" : " " + paletteClass(card.name)),
    style: theme });
  if (avSrc) av.appendChild(el("img", { src: avSrc, alt: "" }));
  else av.appendChild(document.createTextNode(glyphOf(card.name)));
  cell.appendChild(av);

  const tile = el("div", { class: "bp-tile" });
  // Background layer: first available visual, else a flat theme wash via CSS.
  const bgSrc = card.sprite_url || card.keyvisual_url || card.bg_url || "";
  if (bgSrc) {
    // Lazy-ish: set as a background-image (the browser fetches on layout). Using
    // a backing div keeps the gradient overlay above it cheaply.
    const bg = el("div", { class: "bp-bg" });
    bg.style.backgroundImage = `url("${bgSrc}")`;
    tile.appendChild(bg);
  } else {
    tile.classList.add("bp-flat");
  }
  // The bottom→top gradient in the theme color (CSS reads --card-theme).
  tile.appendChild(el("div", { class: "bp-scrim" }));

  // Name near the bottom.
  tile.appendChild(el("div", { class: "bp-name" }, card.name));

  // Reveal overlay: description (active language) + tag chips.
  const chips = el("div", { class: "bp-tags" });
  for (const tag of (copy.tags || [])) {
    chips.appendChild(el("span", { class: "bp-chip" }, lang === "en" ? tag.en : tag.zh));
  }
  tile.appendChild(el("div", { class: "bp-reveal" },
    el("div", { class: "bp-desc" }, lang === "en" ? copy.en : copy.zh),
    chips));
  cell.appendChild(tile);

  // Selecting routes through the EXACT deck wake path: ensure a model is
  // configured (shows the model-setup step if not), then open the 2-step wake
  // editor with the full deck card object.
  // Desktop: hover reveals the overlay, a click selects. Touch (no hover): the
  // first tap reveals the overlay (so the description/tags are readable before
  // committing) and the second tap selects.
  const isTouch = window.matchMedia && window.matchMedia("(hover: none)").matches;
  cell.addEventListener("click", () => {
    if (isTouch && !cell.classList.contains("bp-open")) {
      // reveal this one, collapse siblings
      const grid = cell.parentElement;
      if (grid) grid.querySelectorAll(".bp-cell.bp-open").forEach((n) => n.classList.remove("bp-open"));
      cell.classList.add("bp-open");
      return;
    }
    selectBuiltin(card);
  });
  cell.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectBuiltin(card); }
  });
  return cell;
}

/* Route a picked card into the existing wake flow (consistent with the deck's
   "Wake" button). closeFirstRun is a no-op when the picker is opened from the
   deck (the first-run overlay isn't showing); the wake sheet is a modal. */
function selectBuiltin(card) {
  // Dismiss the standalone picker overlay up front so it never sits behind the
  // model-setup step (ensureModel may reuse the first-run overlay for setup).
  if (typeof closeBuiltinPicker === "function") closeBuiltinPicker();
  // ensureModel runs the action immediately if a model is configured, else it
  // shows the model-setup step first and runs the action after.
  ensureModel(() => { closeFirstRun(); openWakeSheet(card); });
}

/* Build the whole picker body (header + paged grid + arrows + dots) into a
   container. Returns the container; pure DOM, no overlay assumptions, so it can
   live inside the first-run overlay OR a standalone modal-style overlay. */
function buildBuiltinPicker() {
  const root = el("div", { class: "bp-root" });
  root.appendChild(el("div", { class: "bp-head", "data-i18n": "bp-header" }));

  const viewport = el("div", { class: "bp-viewport" });
  const track = el("div", { class: "bp-track" });
  // One grid of four per page; tiles for missing cards are simply skipped.
  for (const page of BUILTIN_PAGES) {
    const grid = el("div", { class: "bp-grid" });
    for (const name of page) {
      const card = builtinCard(name);
      if (card) grid.appendChild(builtinTile(name, card));
    }
    track.appendChild(grid);
  }
  viewport.appendChild(track);
  root.appendChild(viewport);

  // Page state + navigation (arrows, dots, swipe). transform-only = smooth.
  let page = 0;
  const pages = BUILTIN_PAGES.length;
  const dots = el("div", { class: "bp-dots" });
  const dotEls = [];
  for (let i = 0; i < pages; i++) {
    const d = el("i", { onclick: () => goTo(i) });
    dotEls.push(d); dots.appendChild(d);
  }
  function goTo(p) {
    page = Math.max(0, Math.min(pages - 1, p));
    track.style.transform = `translateX(${-page * 100}%)`;
    dotEls.forEach((d, i) => d.classList.toggle("on", i === page));
    prev.classList.toggle("hidden", page === 0);
    next.classList.toggle("hidden", page === pages - 1);
  }
  const prev = el("button", { class: "bp-arrow prev", "aria-label": "previous",
    onclick: () => goTo(page - 1) }, "‹");
  const next = el("button", { class: "bp-arrow next", "aria-label": "next",
    onclick: () => goTo(page + 1) }, "›");
  viewport.appendChild(prev);
  viewport.appendChild(next);
  root.appendChild(dots);

  // Horizontal swipe on touch: a simple threshold on touchstart→touchend dx.
  let sx = 0, sy = 0;
  viewport.addEventListener("touchstart", (ev) => {
    sx = ev.touches[0].clientX; sy = ev.touches[0].clientY;
  }, { passive: true });
  viewport.addEventListener("touchend", (ev) => {
    const dx = ev.changedTouches[0].clientX - sx;
    const dy = ev.changedTouches[0].clientY - sy;
    if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy)) goTo(page + (dx < 0 ? 1 : -1));
  }, { passive: true });

  goTo(0);
  return root;
}
