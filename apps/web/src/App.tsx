import { useEffect, useState } from "react";
import { I18nProvider } from "./i18n";
import { HubProvider, useHubState } from "./state/hub";
import { OverlayProvider } from "./state/overlay";
import { useHashRoute } from "./hooks/useHashRoute";
import { Sidebar } from "./components/Sidebar";
import { OverlayHost } from "./components/overlays/OverlayHost";
import { Login } from "./components/overlays/Login";
import { BOOT, authInfo } from "./rpc";
import { Board } from "./views/Board";
import { Deck } from "./views/Deck";
import { Gateways } from "./views/Gateways";
import { Settings } from "./views/Settings";
import { Chat } from "./views/Chat";

/* The shell — providers + sidebar + the routed view. Hash routing (useHashRoute)
   switches the main pane; the chara page is a full-bleed view without the board
   chrome. Views land per Track C against this contract (see Board.tsx). */

/* The connection dot is the ONLY part of the shell that reads live hub state, so
   it's its own leaf: that keeps every ~1/sec life.state push from re-rendering
   Shell (and with it Sidebar + the whole routed view tree). */
function ConnDot() {
  const { connected } = useHubState();
  return <i id="conn-dot" className={connected ? "ok" : ""} />;
}

function Shell() {
  const route = useHashRoute();

  return (
    <div id="app">
      <Sidebar view={route.view} />
      <div className="main">
        {route.view === "board" && <Board />}
        {route.view === "deck" && <Deck />}
        {route.view === "gateways" && <Gateways />}
        {route.view === "settings" && <Settings />}
        {route.view === "chat" && route.name && <Chat name={route.name} sub={route.sub} />}
      </div>
      <div id="statusbar">
        <span className="grow" />
        <ConnDot />
      </div>
      <OverlayHost />
    </div>
  );
}

/* The auth gate. With a BOOT.token (local app, SSH tunnel, token URL) we behave
   EXACTLY as before — the hub connects straight away, no login screen, no probe.
   ONLY when there is no token (the proxied https://host/ bookmark) do we ask the
   server whether the OPTIONAL password login is offered; if so, show <Login/>;
   otherwise fall through unchanged (the token-cookie / no-auth path decides). */
function Gate() {
  // hasToken is constant for the page lifetime; if present, skip the probe.
  const hasToken = Boolean(BOOT.token);
  const [needLogin, setNeedLogin] = useState<boolean | null>(hasToken ? false : null);

  useEffect(() => {
    if (hasToken) return; // local/tunnel/token path — unchanged, no probe
    let live = true;
    void authInfo().then((info) => {
      if (live) setNeedLogin(info.login);
    });
    return () => {
      live = false;
    };
  }, [hasToken]);

  if (needLogin === null) return null; // brief probe — no flash of the app shell
  if (needLogin) return <Login />;
  return (
    <HubProvider>
      <OverlayProvider>
        <Shell />
      </OverlayProvider>
    </HubProvider>
  );
}

export function App() {
  return (
    <I18nProvider>
      <Gate />
    </I18nProvider>
  );
}
