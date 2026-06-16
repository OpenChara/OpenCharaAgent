/* Card visual helpers — React ports of app.js themeOf/themeStyle/avatarSrc/
 * dataUriSvg/avatarNode/cardVisual (app.js:221-275). Presentation only; degrades
 * to a palette+glyph fallback when a card carries no art. (Deck-track owned —
 * the lib/* DOM-free helpers stay untouched.) */

import type { CSSProperties } from "react";
import { assetUrl } from "../../rpc";
import { glyphOf, paletteClass } from "../../lib/format";
import type { DeckCard } from "./types";

interface ThemeLike {
  theme?: { primary?: string; secondary?: string } | null;
  theme_color?: string;
  avatar_uri?: string;
  avatar_svg?: string;
}

/** app.js:227 themeOf. */
export function themeOf(card: ThemeLike | null | undefined): { primary: string; secondary: string } {
  const th = card && card.theme && typeof card.theme === "object" ? card.theme : {};
  return {
    primary: String((th && th.primary) || (card && card.theme_color) || ""),
    secondary: String((th && th.secondary) || ""),
  };
}

/** app.js:233 themeStyle → a style object for the --card-theme CSS vars. */
export function themeStyle(card: ThemeLike | null | undefined): CSSProperties {
  const { primary, secondary } = themeOf(card);
  if (!primary) return {};
  const style: Record<string, string> = { "--card-theme": primary };
  if (secondary) style["--card-theme-2"] = secondary;
  return style as CSSProperties;
}

/** app.js:221 dataUriSvg. */
export function dataUriSvg(svg: string): string {
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

/** app.js:239 avatarSrc — sidecar data-URI first, then a portable inline SVG. */
export function avatarSrc(card: ThemeLike | null | undefined): string {
  if (card && card.avatar_uri) return String(card.avatar_uri);
  if (card && card.avatar_svg) return dataUriSvg(String(card.avatar_svg));
  return "";
}

/** Shared avatar — image when the card has one, palette+letter glyph otherwise.
 *  app.js:246 avatarNode. */
export function Avatar({
  name,
  card,
  cls = "avatar-s",
  onClick,
  title,
}: {
  name: string;
  card: ThemeLike | null | undefined;
  cls?: string;
  onClick?: () => void;
  title?: string;
}) {
  const style = themeStyle(card);
  const src = avatarSrc(card);
  const hasTheme = Object.keys(style).length > 0;
  const className = cls + (hasTheme || src ? "" : " " + paletteClass(name));
  return (
    <div
      className={className}
      style={onClick ? { ...style, cursor: "pointer" } : style}
      onClick={onClick}
      title={title}
    >
      {src ? <img src={src} alt="" /> : glyphOf(name)}
    </div>
  );
}

/** The deck-card face — sprite under the avatar, palette+glyph fallback.
 *  app.js:257 cardVisual. `children` lets the caller drop in badges/acts. */
export function CardFace({
  card,
  cls = "face",
  children,
}: {
  card: DeckCard;
  cls?: string;
  children?: React.ReactNode;
}) {
  const style = themeStyle(card);
  const src = avatarSrc(card);
  const spriteUrl = assetUrl(card.sprite_url || card.keyvisual_url || "");
  return (
    <div className={cls} style={style}>
      {spriteUrl && (
        <>
          <div
            className="face-sprite"
            style={{ backgroundImage: `url("${String(spriteUrl).replace(/"/g, "%22")}")` }}
          />
          <div className="face-sprite-scrim" />
        </>
      )}
      {src ? (
        <img className="avatar-svg" src={src} alt="" />
      ) : (
        <div className="glyph">{glyphOf(card.name)}</div>
      )}
      {children}
    </div>
  );
}
