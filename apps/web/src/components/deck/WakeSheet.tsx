/* WakeSheet — the wake dialog (single page).
 *
 * Waking and EDITING are separate: a deck card is already a complete persona, so
 * waking only chooses how the chara RUNS — instance name, model (+capability
 * badges), embodiment, isolation, network. To change a card's content you edit
 * the card (the card editor), not the wake dialog.
 *
 * It deliberately sends NO card content. `session.wake` freezes the SOURCE card
 * verbatim, so a chara can NEVER be woken persona-less / greeting-less (the old
 * 2-step "edit on wake" round-tripped the whole card through partial fields and
 * could blank first_mes / the avatar declaration — that class of bug is gone).
 *
 * Binding UI rule: the wake button shows a working state and reverts on failure;
 * the model field live-renders capability badges + the no-tools warning. */

import { useEffect, useState } from "react";
import { useT } from "../../i18n";
import { useHub } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import { rpcErrText } from "../../lib/status";
import { useDirtyGuard } from "../../hooks/useDirtyGuard";
import { Caps } from "./Caps";
import { Avatar } from "./visual";
import { deckToast } from "../ui/deckToast";
import { DeckModal } from "../ui/DeckModal";
import type { DeckCard, ModelInfo } from "./types";

export function WakeSheet({ card, onClose }: { card: DeckCard; onClose: () => void }) {
  const t = useT();
  const { hub, snapshot, refresh } = useHub();
  const nav = useNavigate();
  const defaults = (snapshot?.defaults as { model?: string }) || {};

  const [models, setModels] = useState<ModelInfo[]>([]);
  const [name, setName] = useState(card.name);
  const [model, setModel] = useState(String(defaults.model || ""));
  // New-chara defaults: autonomy / website / network ON, force-roleplay OFF.
  const [wantLive, setWantLive] = useState(true);
  const [personalSite, setPersonalSite] = useState(
    card.website !== false && card.website !== "off",
  );
  const [wantNet, setWantNet] = useState(true);
  const [forceRoleplay, setForceRoleplay] = useState(
    card.force_roleplay === true || card.embodiment === "actor",
  );
  const [iso, setIso] = useState("sandbox");
  const [waking, setWaking] = useState(false);

  useEffect(() => {
    let alive = true;
    hub
      .call<ModelInfo[]>("models.list", {}, 30000)
      .catch(() => [] as ModelInfo[])
      .then((ml) => {
        if (alive) setModels(Array.isArray(ml) ? ml : []);
      });
    return () => {
      alive = false;
    };
  }, [hub]);

  const doWake = async () => {
    setWaking(true);
    try {
      const entry = await hub.call<{ name: string }>(
        "session.wake",
        {
          card: card.path,
          name: name.trim(),
          isolation: iso,
          model: model.trim(),
          toolpack: "sandbox",
          mode: wantLive ? "live" : "chat",
          embodiment: forceRoleplay ? "actor" : "literal",
          website: personalSite ? "on" : "off",
          network: wantNet,
          // No card_data: wake freezes the SOURCE card as-is. Editing the persona
          // is the card editor's job; waking can never blank it.
        },
        60000,
      );
      onClose();
      await refresh();
      nav(`#/chara/${encodeURIComponent(entry.name)}`);
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
      setWaking(false);
    }
  };

  const modelInfo = models.find((m) => m.id === model.trim());
  // Keep the dialog open mid-wake (a click outside must not abandon an in-flight wake).
  const { guardedClose, dirtyProps } = useDirtyGuard(onClose, () => waking);

  return (
    <DeckModal open variant="wide" onClose={guardedClose}>
      <div {...dirtyProps}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 14 }}>
          <Avatar name={card.name} card={card} />
          <div>
            <h2 style={{ margin: 0 }}>{t("wake-title", { name: card.name })}</h2>
            <div className="sub" style={{ marginTop: 2 }}>
              {card.tagline ? card.tagline : t("wake-sub")}
            </div>
          </div>
        </div>

        <div className="wake-settings">
          <div className="field-row">
            <label>{t("wake-name")}</label>
            <div className="input-like">
              <input value={name} onChange={(e) => setName(e.target.value)} />
            </div>
          </div>
          <div className="field-row">
            <label>{t("wake-model")}</label>
            <div className="input-like">
              <input list="model-list" value={model} onChange={(e) => setModel(e.target.value)} />
            </div>
            <datalist id="model-list">
              {models.slice(0, 400).map((m) => (
                <option key={m.id} value={m.id} />
              ))}
            </datalist>
            <Caps
              caps={modelInfo ? { tools: modelInfo.tools, writing: modelInfo.writing, vision: modelInfo.vision } : null}
              style={{ margin: "6px 0 0" }}
            />
            {modelInfo && modelInfo.tools === false && <div className="amber-note">{t("wake-no-tools")}</div>}
          </div>
          {/* Run-time options as one settings list: 自主运行 · 网站 · 网络 · 沙盒 ·
              强化角色扮演. Stacked label+desc (aligned), switch in a right column,
              hairline between rows. First four default on; force-roleplay off. */}
          <div className="field-row toggles">
            <div className="switch-row">
              <div className="tx">
                <b>{t("p-autonomy")}</b>
                <small>{t("p-autonomy-sub")}</small>
              </div>
              <button className={"switch" + (wantLive ? " on" : "")} onClick={() => setWantLive((v) => !v)} />
            </div>
            <div className="switch-row">
              <div className="tx">
                <b>{t("mod-website")}</b>
                <small>{t("mod-website-hint")}</small>
              </div>
              <button className={"switch" + (personalSite ? " on" : "")} onClick={() => setPersonalSite((v) => !v)} />
            </div>
            <div className="switch-row">
              <div className="tx">
                <b>{t("p-net")}</b>
                <small>{t("p-net-sub")}</small>
              </div>
              <button className={"switch" + (wantNet ? " on" : "")} onClick={() => setWantNet((v) => !v)} />
            </div>
            <div className="switch-row">
              <div className="tx">
                <b>{t("p-sandbox")}</b>
                <small>{t("wake-iso-sub")}</small>
              </div>
              <button
                className={"switch" + (iso === "sandbox" ? " on" : "")}
                onClick={() => setIso((v) => (v === "sandbox" ? "admin" : "sandbox"))}
              />
            </div>
            <div className="switch-row">
              <div className="tx">
                <b>{t("mod-roleplay")}</b>
                <small>{t("mod-roleplay-hint")}</small>
              </div>
              <button className={"switch" + (forceRoleplay ? " on" : "")} onClick={() => setForceRoleplay((v) => !v)} />
            </div>
          </div>
        </div>

        <div className="acts" style={{ marginTop: 18 }}>
          <button className="btn text" onClick={guardedClose}>
            {t("cancel")}
          </button>
          <div className="grow" />
          <button className="btn primary big" disabled={waking} onClick={() => void doWake()}>
            {waking ? <span className="spin" /> : t("wake-go")}
          </button>
        </div>
      </div>
    </DeckModal>
  );
}
