/* Characters market tab — search character-tavern.com's open card catalog and add a
 * card to the local deck in one click. Pure client over two hub RPCs:
 *   market.search { query, nsfw } -> { candidates[], totalHits }
 *   market.import { path, nsfw }  -> { path, name, cover }
 * The cover thumbnails load directly in the browser from character-tavern's CDN; the
 * hub only proxies the JSON. Imported cards land UNLOCKED in the deck (editable, then
 * wakeable like any card). */

import { useCallback, useRef, useState } from "react";
import { useT } from "../../i18n";
import { useHub } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { deckToast } from "../ui/deckToast";
import { BrandLoader } from "../ui/BrandLoader";

interface MarketCard {
  path: string;
  name: string;
  tagline: string;
  author: string;
  tags: string[];
  nsfw: boolean;
  hasLorebook: boolean;
  imageUrl: string;
  pageUrl: string;
  excerpt: string;
}

interface SearchResult {
  query: string;
  candidates: MarketCard[];
  totalHits: number;
}

export function CharactersTab() {
  const t = useT();
  const { hub, refresh } = useHub();
  const [query, setQuery] = useState("");
  const [nsfw, setNsfw] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [importing, setImporting] = useState<Set<string>>(new Set());
  const [imported, setImported] = useState<Set<string>>(new Set());
  const reqSeq = useRef(0); // drop stale responses if the user searches again fast

  const mark = (set: React.Dispatch<React.SetStateAction<Set<string>>>, path: string, on: boolean) =>
    set((prev) => {
      const next = new Set(prev);
      if (on) next.add(path);
      else next.delete(path);
      return next;
    });

  const runSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
    const seq = ++reqSeq.current;
    setLoading(true);
    setError("");
    try {
      const res = await hub.call<SearchResult>("market.search", { query: q, nsfw }, 25000);
      if (seq === reqSeq.current) setResult(res);
    } catch (e) {
      if (seq === reqSeq.current) {
        setError(rpcErrText(t, e as { message?: string }));
        setResult(null);
      }
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [query, nsfw, hub, t]);

  const importCard = useCallback(
    async (c: MarketCard) => {
      mark(setImporting, c.path, true);
      try {
        const res = await hub.call<{ name?: string }>("market.import", { path: c.path, nsfw }, 40000);
        mark(setImported, c.path, true);
        deckToast(t("market-added", { name: res?.name || c.name }));
        await refresh(); // so the deck shows it immediately when the user switches over
      } catch (e) {
        deckToast(rpcErrText(t, e as { message?: string }), true);
      } finally {
        mark(setImporting, c.path, false);
      }
    },
    [hub, nsfw, refresh, t],
  );

  const cards = result?.candidates ?? [];

  return (
    <div className="market-body">
      <div className="market-controls">
        <input
          className="searchfield"
          placeholder={t("market-search-ph")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) void runSearch();
          }}
        />
        <button className="btn primary" disabled={loading || !query.trim()} onClick={() => void runSearch()}>
          {loading ? <span className="spin" /> : t("search")}
        </button>
        <label className="market-nsfw">
          <input type="checkbox" checked={nsfw} onChange={(e) => setNsfw(e.target.checked)} />
          <span>{t("market-nsfw")}</span>
        </label>
      </div>
      <div className="market-source-note">{t("market-source")}</div>

      <div className="market-scroll">
        {loading && !cards.length ? (
          <BrandLoader />
        ) : error ? (
          <div className="empty-state market-empty">{error}</div>
        ) : !result ? (
          <div className="empty-state market-empty">{t("market-empty")}</div>
        ) : !cards.length ? (
          <div className="empty-state market-empty">{t("market-none")}</div>
        ) : (
          <div className="market-grid">
            {cards.map((c) => {
              const busy = importing.has(c.path);
              const done = imported.has(c.path);
              return (
                <div className="market-card" key={c.path}>
                  <div className="market-thumb">
                    {/* loads straight from character-tavern's CDN; hide on error */}
                    <img
                      src={c.imageUrl}
                      alt={c.name}
                      loading="lazy"
                      onError={(e) => {
                        (e.currentTarget as HTMLImageElement).style.visibility = "hidden";
                      }}
                    />
                    {c.nsfw && <span className="market-badge nsfw">NSFW</span>}
                    {c.hasLorebook && <span className="market-badge lore">{t("market-lorebook")}</span>}
                  </div>
                  <div className="market-meta">
                    <div className="market-name" title={c.name}>{c.name}</div>
                    {c.author && <div className="market-author">{t("market-by", { author: c.author })}</div>}
                    {c.tagline && <div className="market-tagline" title={c.tagline}>{c.tagline}</div>}
                    {!!c.tags.length && (
                      <div className="market-tags">
                        {c.tags.slice(0, 4).map((tag) => (
                          <span className="market-tag" key={tag}>{tag}</span>
                        ))}
                      </div>
                    )}
                    <div className="market-acts">
                      <button
                        className={"btn sm" + (done ? "" : " primary")}
                        disabled={busy || done}
                        onClick={() => void importCard(c)}
                      >
                        {busy ? <span className="spin" /> : done ? t("market-imported") : t("market-import")}
                      </button>
                      <a className="market-link" href={c.pageUrl} target="_blank" rel="noreferrer noopener" title={c.pageUrl}>↗</a>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
