/* ModelGate — the in-flow model-key step. A brand-new user's first click (create /
 * pick / wake) needs a text key; instead of EJECTING them to Settings and dropping
 * what they wanted (the old useEnsureModel behaviour), this asks for the key right
 * here and, on save, RESUMES their intent via onReady. One recommended path
 * (OpenRouter); "more options" still routes to the full Settings pane.
 *
 * Binding UI rule: the Continue button shows a working state and surfaces errors;
 * the key never leaves this computer (defaults.set persists it locally). */

import { useState } from "react";
import { useT, useLang } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { rpcErrText } from "../../lib/status";
import { DeckModal } from "../ui/DeckModal";
import { deckToast } from "../ui/deckToast";

export function ModelGate({ onClose, onReady }: { onClose: () => void; onReady: () => void }) {
  const t = useT();
  const { lang } = useLang();
  const { hub, refresh } = useHubApi();
  const nav = useNavigate();
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);

  const connect = async () => {
    const key = apiKey.trim();
    if (!key || busy) return;
    setBusy(true);
    try {
      await hub.call(
        "defaults.set",
        { provider: "openrouter", base_url: "https://openrouter.ai/api/v1", api_key: key, ui_lang: lang },
        20000,
      );
      await refresh();
      onClose();
      onReady(); // resume exactly what they set out to do (create / wake)
    } catch (e) {
      setBusy(false);
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  };

  return (
    <DeckModal open variant="sheet" onClose={busy ? () => {} : onClose}>
      <h2>{t("gate-title")}</h2>
      <div className="sub">{t("gate-sub")}</div>
      <div className="gate-field">
        <input
          type="password"
          autoFocus
          placeholder={t("gate-key-ph")}
          value={apiKey}
          disabled={busy}
          onChange={(e) => setApiKey(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void connect();
          }}
        />
      </div>
      <div className="sub gate-note">{t("gate-openrouter-note")}</div>
      <div className="acts" style={{ marginTop: 18 }}>
        <button
          className="btn text"
          disabled={busy}
          onClick={() => {
            onClose();
            nav("#/settings");
          }}
        >
          {t("gate-advanced")}
        </button>
        <div className="grow" />
        <button className="btn primary big" disabled={busy || !apiKey.trim()} onClick={() => void connect()}>
          {busy ? <span className="spin" /> : t("gate-continue")}
        </button>
      </div>
    </DeckModal>
  );
}
