/* Characters market tab — browse & search character-tavern.com's open card catalog and
 * add cards to the local deck. Opens to a real ranking (popular / trending / newest), not
 * a blank search box; filterable by tags / OC / lorebook / NSFW; paged via "load more";
 * each card opens a read-only preview before importing. Pure client over three hub RPCs:
 *   market.search { query, sort, page, nsfw, tags, oc, lorebook } -> { candidates[], totalPages, ... }
 *   market.detail { path } -> the card's persona (for the preview)
 *   market.import { path, nsfw } -> { path, name, image_url }
 * Covers load directly from the storage CDN (resized thumbs). Imported cards land UNLOCKED. */

import { useCallback, useEffect, useRef, useState } from "react";
import { useT, useLang, type TKey } from "../../i18n";
import { FEATURED, tagLabel, suggestTags } from "../../lib/marketTags";
import { useHub } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { fileToB64 } from "../../lib/file";
import { compactNum } from "../../lib/format";
import { deckToast } from "../ui/deckToast";
import { BrandLoader } from "../ui/BrandLoader";
import { MarketCardDetail } from "./MarketCardDetail";

type HubCaller = { call<T = unknown>(m: string, p?: Record<string, unknown>, t?: number): Promise<T> };
const MIME_EXT: Record<string, string> = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp" };

/** Bring a card's cover over CLIENT-SIDE as: the sprite (立绘, the displayed standing art)
 *  AND a persisted reference image (参考图) in the card's asset library. NOT the keyvisual
 *  (主视觉, the AI-generated anchor, left empty) and never the avatar. Best-effort: a CORS
 *  block / offline just leaves the card showing its stored cover URL as the sprite. */
async function bringCoverOver(hub: HubCaller, cardPath: string, imageUrl: string): Promise<void> {
  let blob: Blob;
  try {
    const r = await fetch(imageUrl, { credentials: "omit" });
    if (!r.ok) return;
    blob = await r.blob();
  } catch {
    return;
  }
  const ext = MIME_EXT[blob.type];
  if (!ext) return;
  const b64 = await fileToB64(new File([blob], "cover", { type: blob.type }));
  if (!b64) return;
  try {
    await hub.call("card.asset_save", { path: cardPath, kind: "sprite", data_b64: b64, ext }, 30000);
    await hub.call("card.asset_file_upload", { path: cardPath, name: `reference.${ext}`, data_b64: b64, ext }, 30000);
  } catch {
    /* best-effort */
  }
}

export interface MarketCard {
  path: string;
  name: string;
  tagline: string;
  author: string;
  tags: string[];
  nsfw: boolean;
  hasLorebook: boolean;
  oc: boolean;
  downloads: number;
  likes: number;
  messages: number;
  imageUrl: string;
  pageUrl: string;
  excerpt: string;
}

interface SearchResult {
  query: string;
  sort: string;
  page: number;
  totalPages: number;
  candidates: MarketCard[];
  totalHits: number;
}

const SORTS: ReadonlyArray<readonly [string, TKey]> = [
  ["most_popular", "market-sort-popular"],
  ["trending", "market-sort-trending"],
  ["newest", "market-sort-newest"],
] as const;

const PAGE_SIZE = 24;

export function CharactersTab() {
  const t = useT();
  const { lang } = useLang();
  const { hub, refresh } = useHub();
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState(""); // the committed query (drives the fetch)
  const [sort, setSort] = useState<string>("most_popular");
  const [nsfw, setNsfw] = useState(false);
  const [oc, setOc] = useState(false);
  const [lorebook, setLorebook] = useState(false);
  const [tags, setTags] = useState<string[]>([]);

  const [cards, setCards] = useState<MarketCard[]>([]);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [totalHits, setTotalHits] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");

  const [importing, setImporting] = useState<Set<string>>(new Set());
  const [imported, setImported] = useState<Set<string>>(new Set());
  const [broken, setBroken] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<MarketCard | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const reqSeq = useRef(0);

  const activeCount = tags.length + (oc ? 1 : 0) + (lorebook ? 1 : 0) + (nsfw ? 1 : 0);
  const clearFilters = () => {
    setTags([]);
    setOc(false);
    setLorebook(false);
    setNsfw(false);
  };

  const tagsKey = [...tags].sort().join(",");

  const mark = (set: React.Dispatch<React.SetStateAction<Set<string>>>, path: string, on: boolean) =>
    set((prev) => {
      const next = new Set(prev);
      if (on) next.add(path);
      else next.delete(path);
      return next;
    });

  const fetchPage = useCallback(
    async (pageNum: number, append: boolean) => {
      const seq = append ? reqSeq.current : ++reqSeq.current;
      if (append) setLoadingMore(true);
      else {
        setLoading(true);
        setError("");
      }
      try {
        const res = await hub.call<SearchResult>(
          "market.search",
          { query, sort, page: pageNum, nsfw, tags, oc, lorebook, limit: PAGE_SIZE },
          25000,
        );
        if (seq !== reqSeq.current) return;
        setCards((prev) => (append ? [...prev, ...res.candidates] : res.candidates));
        setPage(res.page);
        setTotalPages(res.totalPages);
        setTotalHits(res.totalHits);
        if (!append) setBroken(new Set());
      } catch (e) {
        if (seq === reqSeq.current && !append) {
          setError(rpcErrText(t, e as { message?: string }));
          setCards([]);
        } else if (seq === reqSeq.current) {
          deckToast(rpcErrText(t, e as { message?: string }), true);
        }
      } finally {
        if (seq === reqSeq.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [hub, query, sort, nsfw, oc, lorebook, tags, t],
  );

  // Re-browse from page 1 whenever the query/sort/filters change (the committed query,
  // not every keystroke). On mount this fires the default browse → opens to content.
  useEffect(() => {
    void fetchPage(1, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, sort, nsfw, oc, lorebook, tagsKey]);

  useEffect(() => {
    if (!filtersOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFiltersOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [filtersOpen]);

  const submitSearch = () => setQuery(queryInput.trim());
  const toggleTag = (tag: string) =>
    setTags((prev) => (prev.includes(tag) ? prev.filter((x) => x !== tag) : [...prev, tag]));
  // Manual tag entry — the chips are a curated subset of the catalog's 700+ tags, so let
  // the user filter by ANY tag by typing it (normalized lowercase; the API filters on it).
  // As they type, `suggestTags` autocompletes from the known vocabulary (matching slug OR
  // Chinese label), but a tag missing from the dict is still accepted verbatim.
  const [tagInput, setTagInput] = useState("");
  const suggestions = suggestTags(tagInput, tags);
  const addTag = (raw: string) => {
    const v = raw.trim().toLowerCase();
    if (v && !tags.includes(v)) setTags((prev) => [...prev, v]);
    setTagInput("");
  };
  // Enter picks the top suggestion when there is one (so typing 猫 + Enter adds "catgirl"),
  // else commits the raw text.
  const commitTyped = () => addTag(suggestions[0]?.slug ?? tagInput);

  const importByPath = useCallback(
    async (path: string, name: string, imageUrl: string) => {
      mark(setImporting, path, true);
      try {
        const res = await hub.call<{ path?: string; name?: string; image_url?: string }>(
          "market.import", { path, nsfw }, 40000);
        if (res?.path) await bringCoverOver(hub, res.path, res.image_url || imageUrl);
        mark(setImported, path, true);
        deckToast(t("market-added", { name: res?.name || name }));
        void refresh().catch(() => {});
      } catch (e) {
        deckToast(rpcErrText(t, e as { message?: string }), true);
      } finally {
        mark(setImporting, path, false);
      }
    },
    [hub, nsfw, refresh, t],
  );

  return (
    <div className="market-body">
      <div className="market-controls">
        <input
          className="searchfield"
          placeholder={t("market-search-ph")}
          value={queryInput}
          onChange={(e) => setQueryInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) submitSearch();
          }}
        />
        <button className="btn primary" disabled={loading} onClick={submitSearch}>
          {loading ? <span className="spin" /> : t("search")}
        </button>
      </div>

      <div className="market-bar">
        <div className="market-sorts">
          {SORTS.map(([key, label]) => (
            <button
              key={key}
              className={"market-sort" + (sort === key ? " on" : "")}
              onClick={() => setSort(key)}
            >
              {t(label)}
            </button>
          ))}
        </div>
        <div className="market-filterwrap">
          <button
            className={"market-filtersbtn" + (activeCount ? " on" : "")}
            onClick={() => setFiltersOpen((v) => !v)}
            aria-expanded={filtersOpen}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>
              <path d="M2 4h12M4 8h8M6 12h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none" />
            </svg>
            {t("market-filters-btn")}
            {activeCount > 0 && <span className="market-filter-badge">{activeCount}</span>}
          </button>
          {filtersOpen && (
            <>
              <div className="market-pop-catch" onClick={() => setFiltersOpen(false)} />
              <div className="market-pop" role="dialog" aria-label={t("market-filters-btn")}>
                <div className="market-pop-group">
                  <div className="market-pop-h">{t("market-filter-tags")}</div>
                  <div className="market-pop-tags">
                    {FEATURED.map((tag) => (
                      <button
                        key={tag}
                        className={"market-tagchip" + (tags.includes(tag) ? " on" : "")}
                        onClick={() => toggleTag(tag)}
                      >
                        {tagLabel(tag, lang)}
                      </button>
                    ))}
                  </div>
                  <div className="market-tag-entry">
                    <input
                      className="market-tag-input"
                      placeholder={t("market-tag-add")}
                      value={tagInput}
                      onChange={(e) => setTagInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                          e.preventDefault();
                          commitTyped();
                        }
                      }}
                    />
                    {suggestions.length > 0 && (
                      <div className="market-tag-sugg">
                        {suggestions.map((s) => (
                          <button
                            key={s.slug}
                            className="market-tag-suggrow"
                            onMouseDown={(e) => {
                              e.preventDefault();
                              addTag(s.slug);
                            }}
                          >
                            <span className="market-tag-suggzh">{lang === "zh" ? s.zh : s.slug}</span>
                            {lang === "zh" && <span className="market-tag-suggen">{s.slug}</span>}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
                <div className="market-pop-group">
                  <div className="market-pop-h">{t("market-filter-type")}</div>
                  <label className="market-check">
                    <input type="checkbox" checked={oc} onChange={(e) => setOc(e.target.checked)} />
                    <span>{t("market-filter-oc")}</span>
                  </label>
                  <label className="market-check">
                    <input type="checkbox" checked={lorebook} onChange={(e) => setLorebook(e.target.checked)} />
                    <span>{t("market-filter-lorebook")}</span>
                  </label>
                </div>
                <div className="market-pop-group">
                  <div className="market-pop-h">{t("market-filter-mature")}</div>
                  <label className="market-check">
                    <input type="checkbox" checked={nsfw} onChange={(e) => setNsfw(e.target.checked)} />
                    <span>{t("market-nsfw")}</span>
                  </label>
                  <div className="market-pop-note">{t("market-nsfw-note")}</div>
                </div>
                {activeCount > 0 && (
                  <button className="market-pop-clear" onClick={clearFilters}>{t("market-clear")}</button>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {activeCount > 0 && (
        <div className="market-applied">
          {tags.map((tag) => (
            <button key={tag} className="market-applied-chip" onClick={() => toggleTag(tag)}>
              {tagLabel(tag, lang)} <span aria-hidden>✕</span>
            </button>
          ))}
          {oc && (
            <button className="market-applied-chip" onClick={() => setOc(false)}>
              {t("market-filter-oc")} <span aria-hidden>✕</span>
            </button>
          )}
          {lorebook && (
            <button className="market-applied-chip" onClick={() => setLorebook(false)}>
              {t("market-filter-lorebook")} <span aria-hidden>✕</span>
            </button>
          )}
          {nsfw && (
            <button className="market-applied-chip mature" onClick={() => setNsfw(false)}>
              {t("market-nsfw")} <span aria-hidden>✕</span>
            </button>
          )}
          <button className="market-applied-clear" onClick={clearFilters}>{t("market-clear")}</button>
        </div>
      )}

      <div className="market-scroll">
        {loading ? (
          <BrandLoader />
        ) : error ? (
          <div className="empty-state market-empty">{error}</div>
        ) : !cards.length ? (
          <div className="empty-state market-empty">{t("market-none")}</div>
        ) : (
          <>
            <div className="market-count">{t("market-results", { n: compactNum(totalHits) })}</div>
            <div className="market-grid">
              {cards.map((c) => {
                const busy = importing.has(c.path);
                const done = imported.has(c.path);
                return (
                  <div
                    className="market-card"
                    key={c.path}
                    onClick={() => setPreview(c)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.target !== e.currentTarget) return; // the import button handles its own keys
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault(); // Space must not scroll the grid
                        setPreview(c);
                      }
                    }}
                  >
                    <div className="market-thumb">
                      <span className="market-thumb-fallback" aria-hidden>
                        {(c.name || "?").trim().charAt(0).toUpperCase()}
                      </span>
                      {!broken.has(c.path) && (
                        <img
                          src={c.imageUrl}
                          alt={c.name}
                          loading="lazy"
                          onError={() => mark(setBroken, c.path, true)}
                        />
                      )}
                      {c.nsfw && <span className="market-badge nsfw">NSFW</span>}
                      {c.hasLorebook && <span className="market-badge lore">{t("market-lorebook")}</span>}
                    </div>
                    <div className="market-meta">
                      <div className="market-name" title={c.name}>{c.name}</div>
                      {c.author && <div className="market-author">{t("market-by", { author: c.author })}</div>}
                      {c.tagline && <div className="market-tagline" title={c.tagline}>{c.tagline}</div>}
                      <div className="market-stats">
                        {c.downloads > 0 && <span title="downloads">⬇ {compactNum(c.downloads)}</span>}
                        {c.likes > 0 && <span title="likes">♥ {compactNum(c.likes)}</span>}
                        {c.oc && <span className="market-oc">OC</span>}
                      </div>
                      <div className="market-acts" onClick={(e) => e.stopPropagation()}>
                        <button
                          className={"btn sm" + (done ? "" : " primary")}
                          disabled={busy || done}
                          onClick={() => void importByPath(c.path, c.name, c.imageUrl)}
                        >
                          {busy ? <span className="spin" /> : done ? t("market-imported") : t("market-import")}
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
            {page < totalPages && (
              <div className="market-more">
                <button className="btn soft" disabled={loadingMore} onClick={() => void fetchPage(page + 1, true)}>
                  {loadingMore ? <span className="spin" /> : t("market-load-more")}
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {preview && (
        <MarketCardDetail
          card={preview}
          onClose={() => setPreview(null)}
          onImport={() => void importByPath(preview.path, preview.name, preview.imageUrl)}
          importing={importing.has(preview.path)}
          imported={imported.has(preview.path)}
        />
      )}
    </div>
  );
}
