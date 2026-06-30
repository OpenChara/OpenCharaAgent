/* Market — a top-level section for browsing community content and pulling it into the
 * local install. Today it IS the characters catalog (browse open-source 角色卡 from
 * character-tavern.com → add to the deck). When a second kind (Skills, …) actually ships,
 * a tab strip returns here — until then we show no placeholder for unbuilt things.
 * Decoupled: the section owns its own views/components and talks to the hub only through
 * `market.*` RPCs, touching no other subsystem.
 *
 * Binding UI rule: search shows a working state; import flips its own button to a spinner
 * and surfaces success/errors via the deck toast (no silent waits). */

import { useT } from "../i18n";
import { CharactersTab } from "../components/market/CharactersTab";

export function Market() {
  const t = useT();
  return (
    <div className="view active" id="view-market">
      <div className="toolbar">
        <h1>
          <span>{t("nav-market")}</span>
        </h1>
      </div>
      <CharactersTab />
    </div>
  );
}
