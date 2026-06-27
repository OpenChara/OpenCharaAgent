/* Market — a top-level section for browsing community content and pulling it into
 * the local install. Today it has ONE tab, Characters (browse open-source角色卡 from
 * character-tavern.com → add to the deck); the tab bar is built to grow (Skills etc.
 * land beside it). Decoupled: the section owns its own views/components and talks to
 * the hub only through `market.*` RPCs, touching no other subsystem.
 *
 * Binding UI rule: search shows a working state; import flips its own button to a
 * spinner and surfaces success/errors via the deck toast (no silent waits). */

import { useState } from "react";
import { useT } from "../i18n";
import { CharactersTab } from "../components/market/CharactersTab";

type Tab = "characters" | "skills";

export function Market() {
  const t = useT();
  const [tab, setTab] = useState<Tab>("characters");

  return (
    <div className="view active" id="view-market">
      <div className="toolbar">
        <h1>
          <span>{t("nav-market")}</span>
        </h1>
        <div className="grow" />
        <div className="market-tabs" role="tablist">
          <button
            role="tab"
            className={`market-tab${tab === "characters" ? " active" : ""}`}
            aria-selected={tab === "characters"}
            onClick={() => setTab("characters")}
          >
            {t("market-characters")}
          </button>
          {/* Future market kinds slot in here; shown disabled to signal the section grows. */}
          <button role="tab" className="market-tab" disabled title={t("market-soon")}>
            {t("market-skills")} · {t("market-soon")}
          </button>
        </div>
      </div>

      {tab === "characters" && <CharactersTab />}
    </div>
  );
}
