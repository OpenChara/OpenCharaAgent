/* MarketCardDetail — a read-only preview of a market card before importing, styled like
 * browsing one of your own cards (the same labelled CardField sections) but trimmed: no
 * edit, no wake, no visual/asset tabs. Fetches the full persona on open (market.detail)
 * and offers one action — add to deck. The persona is read-only; {{char}}/{{user}} macros
 * are shown as-is (they resolve at play time). */

import { useEffect, useState } from "react";
import { useT, type TKey } from "../../i18n";
import { useHub } from "../../state/hub";
import { rpcErrText } from "../../lib/status";
import { DeckModal } from "../ui/DeckModal";
import { BrandLoader } from "../ui/BrandLoader";
import { CardField } from "../deck/CardField";
import type { MarketCard } from "./CharactersTab";

interface MarketDetail {
  path: string;
  name: string;
  author: string;
  tagline: string;
  description: string;
  personality: string;
  scenario: string;
  first_mes: string;
  mes_example: string;
  tags: string[];
  nsfw: boolean;
  hasLorebook: boolean;
  oc: boolean;
  downloads: number;
  views: number;
  messages: number;
  imageUrl: string;
  pageUrl: string;
}

const SECTIONS: ReadonlyArray<readonly [keyof MarketDetail, TKey]> = [
  ["description", "sec-description"],
  ["personality", "cve-personality"],
  ["scenario", "cve-scenario"],
  ["first_mes", "sec-first"],
  ["mes_example", "market-sec-example"],
] as const;

function compactNum(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}

export function MarketCardDetail({
  card,
  onClose,
  onImport,
  importing,
  imported,
}: {
  card: MarketCard;
  onClose: () => void;
  onImport: () => void;
  importing: boolean;
  imported: boolean;
}) {
  const t = useT();
  const { hub } = useHub();
  const [detail, setDetail] = useState<MarketDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    hub
      .call<MarketDetail>("market.detail", { path: card.path }, 25000)
      .then((d) => {
        if (alive) setDetail(d);
      })
      .catch((e) => {
        if (alive) setError(rpcErrText(t, e as { message?: string }));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [card.path, hub, t]);

  // Fall back to the grid row's fields while the full persona loads, so the header
  // (cover / name / author / tagline) shows instantly — no blank flash.
  const d = detail;
  const cover = d?.imageUrl || card.imageUrl;

  return (
    <DeckModal open variant="wide" onClose={onClose}>
      <div className="mkd">
        <div className="mkd-head">
          <div className="mkd-cover">
            <span className="market-thumb-fallback" aria-hidden>
              {(card.name || "?").trim().charAt(0).toUpperCase()}
            </span>
            <img src={cover} alt={card.name} onError={(e) => (e.currentTarget.style.display = "none")} />
          </div>
          <div className="mkd-headmeta">
            <h2 className="mkd-name">{card.name}</h2>
            {card.author && <div className="mkd-author">{t("market-by", { author: card.author })}</div>}
            {(d?.tagline || card.tagline) && <div className="mkd-tagline">{d?.tagline || card.tagline}</div>}
            <div className="mkd-stats">
              {(d?.downloads ?? card.downloads) > 0 && <span>⬇ {compactNum(d?.downloads ?? card.downloads)}</span>}
              {(d?.views ?? 0) > 0 && <span>👁 {compactNum(d!.views)}</span>}
              {(d?.messages ?? card.messages) > 0 && <span>💬 {compactNum(d?.messages ?? card.messages)}</span>}
            </div>
            <div className="mkd-badges">
              {(d?.nsfw ?? card.nsfw) && <span className="market-badge nsfw">NSFW</span>}
              {(d?.hasLorebook ?? card.hasLorebook) && <span className="market-badge lore">{t("market-lorebook")}</span>}
              {(d?.oc ?? card.oc) && <span className="market-badge oc">OC</span>}
            </div>
            {!!(d?.tags || card.tags)?.length && (
              <div className="mkd-tags">
                {(d?.tags || card.tags).slice(0, 12).map((tag) => (
                  <span className="market-tag" key={tag}>{tag}</span>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="mkd-sections">
          {loading ? (
            <BrandLoader />
          ) : error ? (
            <div className="empty-state market-empty">{error}</div>
          ) : d ? (
            SECTIONS.map(([key, label]) => {
              const text = String(d[key] || "");
              if (!text) return null;
              return (
                <div className="sec" key={key}>
                  <h3>{t(label)}</h3>
                  <CardField initial={text} editable={false} />
                </div>
              );
            })
          ) : null}
        </div>

        <div className="mkd-bar">
          <a className="btn text" href={card.pageUrl} target="_blank" rel="noreferrer noopener">
            {t("market-open-source")} ↗
          </a>
          <div className="grow" />
          <button className="btn text" onClick={onClose}>{t("cancel")}</button>
          <button className="btn primary" disabled={importing || imported} onClick={onImport}>
            {importing ? <span className="spin" /> : imported ? t("market-imported") : t("market-import")}
          </button>
        </div>
      </div>
    </DeckModal>
  );
}
