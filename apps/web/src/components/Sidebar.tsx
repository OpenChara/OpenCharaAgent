import type { ReactNode } from "react";
import { useT } from "../i18n";
import { useNavigate, type ViewName } from "../hooks/useHashRoute";

/* Left nav — mirrors index.html .sidebar. Chat counts as "board" for the active
   highlight (app.js:393), since a chara page is reached from the board. */

const ICONS: Record<Exclude<ViewName, "chat">, ReactNode> = {
  board: (
    <svg viewBox="0 0 16 16" fill="currentColor">
      <circle cx="8" cy="5" r="3" />
      <path d="M2 14c0-3 2.7-5 6-5s6 2 6 5" opacity=".6" />
    </svg>
  ),
  deck: (
    <svg viewBox="0 0 16 16" fill="currentColor">
      <rect x="2" y="2" width="9" height="12" rx="1.5" opacity=".55" />
      <rect x="5" y="2" width="9" height="12" rx="1.5" />
    </svg>
  ),
  market: (
    <svg viewBox="0 0 16 16" fill="currentColor">
      <path d="M2.5 3h11l-.7 3.2a1.4 1.4 0 0 1-1.37 1.1H4.57A1.4 1.4 0 0 1 3.2 6.2L2.5 3z" opacity=".55" />
      <path d="M3.4 7.5h9.2V13a1 1 0 0 1-1 1H4.4a1 1 0 0 1-1-1V7.5z" />
      <circle cx="8" cy="10.5" r="1.1" fill="var(--panel, #fff)" />
    </svg>
  ),
  gateways: (
    <svg viewBox="0 0 16 16" fill="currentColor">
      <path d="M4 4h8v3H4z" opacity=".55" />
      <path d="M4 9h8v3H4z" />
      <circle cx="6" cy="5.5" r=".9" />
      <circle cx="6" cy="10.5" r=".9" />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 16 16" fill="currentColor">
      <circle cx="8" cy="8" r="2.4" />
      <path
        d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  ),
};

const ITEMS: Array<{ view: Exclude<ViewName, "chat">; key: string; hash: string }> = [
  { view: "board", key: "nav-charas", hash: "#/" },
  { view: "deck", key: "nav-deck", hash: "#/deck" },
  { view: "market", key: "nav-market", hash: "#/market" },
  { view: "gateways", key: "nav-gateways", hash: "#/gateways" },
  { view: "settings", key: "nav-settings", hash: "#/settings" },
];

export function Sidebar({ view }: { view: ViewName }) {
  const t = useT();
  const nav = useNavigate();
  const active = view === "chat" ? "board" : view;
  return (
    <aside className="sidebar" id="sidebar">
      {ITEMS.map((it) => (
        <button
          key={it.view}
          className={`nav-item${active === it.view ? " active" : ""}`}
          data-view={it.view}
          onClick={() => nav(it.hash)}
        >
          {ICONS[it.view]}
          <span>{t(it.key)}</span>
        </button>
      ))}
      <div className="spacer" />
    </aside>
  );
}
