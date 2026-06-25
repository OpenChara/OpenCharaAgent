/* FirstRun — the no-charas first-run welcome, a React port of app.js openFirstRun
 * (1673) / frShowWelcome / frShowPicker. The wordmark + a lang toggle, then two
 * paths: create your character (CreateFlow), or pick a recommended one (the builtin
 * carousel embedded here). Card import is deferred — to start from a card you have
 * elsewhere (e.g. a SillyTavern card), paste its JSON into the create box.
 *
 * In the vanilla app a missing model reused this overlay's setup column; the SPA
 * routes the model gate to Settings (useEnsureModel), so this overlay only carries
 * welcome + the embedded picker. Binding UI rule: the lang toggle flips instantly;
 * each path opens its own overlay. */

import { useState } from "react";
import { useT, useLang } from "../../i18n";
import { useOverlay } from "../../state/overlay";
import { useEnsureModel } from "./useEnsureModel";
import { BuiltinPickerBody } from "./BuiltinPicker";
import { Segmented } from "../ui/Segmented";
import type { DeckCard } from "../deck/types";

type Pane = "welcome" | "picker";

export function FirstRun({ onClose }: { onClose: () => void }) {
  const t = useT();
  const { lang, setLang } = useLang();
  const overlay = useOverlay();
  const ensureModel = useEnsureModel();
  const [pane, setPane] = useState<Pane>("welcome");

  const onPick = (card: DeckCard) => {
    onClose();
    ensureModel(() => overlay.open({ kind: "wake", card }));
  };

  return (
    <div className="overlay open" id="overlay-firstrun">
      {/* A way out: dismiss to the board (always re-openable via the board's
          "meet your character" CTA), so the welcome never traps a new user. */}
      <button className="fr-close" onClick={onClose} title={t("cancel")} aria-label={t("cancel")}>
        ×
      </button>
      <div className="fr-dots" id="fr-dots">
        <i className={pane === "welcome" ? "on" : "done"} />
        <i className={pane === "picker" ? "on" : ""} />
      </div>

      {pane === "welcome" ? (
        <div id="fr-welcome" style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
          <div className="fr-word">LunaMoth</div>
          <Segmented
            className="fr-langseg"
            ariaLabel="Language / 语言"
            value={lang}
            options={[{ value: "zh", label: "中文" }, { value: "en", label: "English" }]}
            onChange={setLang}
          />
          {/* tagline carries a <br> in the dict — render it as HTML (trusted copy). */}
          <div className="fr-tagline" dangerouslySetInnerHTML={{ __html: t("tagline") }} />
          <button
            className="btn primary big stacked"
            id="fr-create"
            onClick={() => {
              onClose();
              ensureModel(() => overlay.open({ kind: "create" }));
            }}
          >
            <span>{t("fr-create-title")}</span>
            <small>{t("fr-create-sub")}</small>
          </button>
          <button className="btn soft stacked fr-second" id="fr-try" onClick={() => setPane("picker")}>
            <span>{t("btn-try")}</span>
            <small>{t("btn-try-sub")}</small>
          </button>
        </div>
      ) : (
        <div id="fr-picker" className="bp-host" style={{ display: "block" }}>
          <BuiltinPickerBody onSelect={onPick} />
          <div className="acts" style={{ justifyContent: "center", marginTop: 8 }}>
            <button className="btn text" onClick={() => setPane("welcome")}>{t("back")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
