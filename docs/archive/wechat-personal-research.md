# Personal WeChat (个人微信) — why our adapter fails, and how AstrBot actually connects

Research doc. Investigates why LunaMoth's personal-WeChat adapter
(`src/lunamoth/messaging/weixin.py`) produces a QR that "scans to nothing"
(扫描啥都不是), how AstrBot connects personal WeChat with one QR that "just
works," and a concrete reimplementation path. **No product code was changed.**

Date: 2026-06-13. Today's WeChat reality moves fast; treat version numbers and
endpoint paths as snapshots, re-verify before building.

---

## TL;DR (decision-ready)

- **Our adapter and AstrBot's *"Connect Personal WeChat"* adapter target the
  SAME backend: Tencent's official `openclaw-weixin` / ClawBot (iLink) API.**
  AstrBot did not find a magic one-QR path — its `weixin_oc` adapter is the
  same surface we built (QR + long-poll, no webhook). [astrbot-oc][wiki-oc]
- **Why our QR is "nothing when scanned":** the ClawBot QR is *not* a
  WeChat-login QR. It binds your account to the **WeChat ClawBot plugin
  (微信 ClawBot / OpenClaw bridge)**, which is in **grayscale rollout (灰度)** —
  "部分用户可能暂时无法看到，请耐心等待." If the owner's WeChat app has **not yet
  received the ClawBot grayscale** (or is below iOS 8.0.70 / Android 8.0.69),
  the app has no ClawBot handler registered, so scanning that QR resolves to
  nothing. This is an account-eligibility gate, not a bug in our code.
  [zhihu-claw][drivers][cnblogs-claw]
- **The adapter the owner saw "just work" is a DIFFERENT one: AstrBot +
  WeChatPadPro.** That path does NOT use ClawBot at all. It runs a **self-hosted
  Go gateway (WeChatPadPro) speaking the WeChat *Pad/iPad protocol*** which
  generates a real personal-account login QR, holds the session, pushes inbound
  over a **WebSocket** (`/ws/GetSyncMsg?key=...`) and sends via **HTTP REST**.
  No grayscale eligibility needed — works for any account. [astrbot-pad][padpro][ccino]
- **Recommendation: keep the iLink adapter as-is (it's correct for users who
  HAVE ClawBot), and add a second adapter `weixinpad` targeting a user-run
  WeChatPadPro docker container.** It fits our sync `messaging.Adapter` seam
  cleanly (one background WS reader thread + stdlib HTTP for send), is
  say-channel-only, and gives the "one QR and it works" experience. Effort:
  ~1–1.5 days for text-only. **Ban-risk is real** (unofficial iPad protocol,
  Tencent risk-control) and must be surfaced honestly to the user.

---

## 1. Our current adapter (the seam we must fit)

`src/lunamoth/messaging/weixin.py` — `WeixinAdapter(Adapter)`:

- Base URL `https://ilinkai.weixin.qq.com`; calls `ilink/bot/get_bot_qrcode`
  (bot_type=3), polls `ilink/bot/get_qrcode_status`, then long-polls
  `ilink/bot/getupdates` and sends via `ilink/bot/sendmessage`.
- Auth: `AuthorizationType: ilink_bot_token`, `Bearer <bot_token>`.
- Constraint baked in: **the bot can only `send` after the human messages first**
  (needs a per-user `context_token`), surfaced as `DeliveryDeferred`.
- It is a clean fit to the `messaging.Adapter` ABC (`messaging/base.py`):
  `run(inbox)` owns I/O on a daemon thread, `send(text)` emits, `name`,
  `set_reply_target`/`clear_reply_target`, `close()`. The gateway
  (`messaging/gateway.py`) supervises it: one thread per adapter, say-channel
  text only, dedup on `message_id`, one-retry-then-drop on send, allowlist.

Nothing about the seam is wrong. The problem is entirely the *backend the QR
points at*.

---

## 2. What AstrBot actually ships for personal WeChat (two different adapters)

AstrBot ships **two** distinct personal-WeChat platform adapters. People
conflate them; they are not the same path.

### 2a. `weixin_oc` — "Connect Personal WeChat" = the SAME thing we built

AstrBot's docs: *"built on Tencent's official `openclaw-weixin` interface, uses
QR-code login plus long polling, and does not require a Webhook callback URL."*
[astrbot-oc] Prereqs (from the zh wiki): **iOS ≥ 8.0.70 / Android ≥ 8.0.69, and
"ensure WeChat includes the ClawBot plugin."** [wiki-oc] Same QR-then-long-poll
shape, same `openclaw-weixin`/iLink endpoints, same "human must speak first"
behavior. So if the owner tried AstrBot's *this* adapter, it would fail the
**exact same way** ours does — because it's the same ClawBot surface.

There is even a community drop-in (`SiverKing/weixin-ClawBot-API`) that
re-implements the same `tencent-weixin/openclaw-weixin` bot endpoints — further
confirming this is one shared backend, not AstrBot magic. [siverking]

### 2b. WeChatPadPro adapter — the one that "just works with one QR"

This is the path that matches the owner's experience. It does **not** touch
ClawBot. Setup [astrbot-pad][ccino][linuxdo][80aj]:

1. User runs a **WeChatPadPro docker container** (Go service; deps **MySQL 5.7+**
   and **Redis**). Default API port in the common compose is **38849** (upstream
   default 8080/8848 depending on build); an `adminKey` is set in
   `setting.json`. [ccino][padpro]
2. AstrBot's WeChatPadPro adapter is configured with **host + port + admin_key**.
   On first connect with no saved credential, the adapter calls WeChatPadPro to
   **generate an auth key from the admin_key**, then asks WeChatPadPro for a
   **login QR**, which AstrBot prints to its console log as an image URL
   (`https://api.pwmqr.com/qrcode/create/?url=...`). [ccino]
3. **That QR is a genuine WeChat personal-account login QR** (Pad protocol). The
   user scans it with their phone WeChat and confirms "log in on another device."
   WeChatPadPro now holds the session; the `auth_key` per `wxid` is persisted to
   `data/wechatpadpro_credentials.json`. [ccino]
4. **Receive:** AstrBot opens a **WebSocket** to WeChatPadPro
   (`ws://host:port/ws/GetSyncMsg?key=<auth_key>`) and gets pushed inbound
   messages in real time. [issue-1909]
5. **Send:** AstrBot calls WeChatPadPro's **HTTP REST** message endpoints
   (send-text/send-file under `/api/.../message/...`). [padpro]

Because the QR is a real device-login QR (not a plugin-binding QR), it works for
*any* WeChat account with no grayscale gate. That is precisely why the owner saw
"one QR and it just works."

---

## 3. WHY our QR "is nothing when scanned" (concrete)

Root cause: **the iLink/ClawBot QR is a ClawBot-plugin binding QR, and the
ClawBot plugin is a grayscale-gated WeChat feature the owner's app likely
hasn't received.**

- ClawBot (微信 ClawBot, the WeChat↔OpenClaw bridge) launched ~2026-03-22 and
  rolled out **by grayscale**: "微信安卓以及 iOS 用户将 App 更新至最新版后，可以
  等待微信 ClawBot 灰度，已灰度的用户可以开启使用…部分用户可能暂时无法看到，请
  耐心等待." [zhihu-claw][drivers]
- A WeChat QR code is resolved *client-side* by the app: the app inspects the
  encoded URL/scheme and dispatches to whatever handler is registered for it.
  The ClawBot bind-QR encodes a ClawBot/OpenClaw scheme. **If the WeChat app has
  no ClawBot capability registered (not in the grayscale, or app < 8.0.70/8.0.69),
  there is no handler — the scan resolves to nothing** ("扫描啥都不是"). It is not
  a login QR, so it will never present a "log in?" prompt the way a device-login
  QR does.
- Contributing/adjacent failure modes that produce the same silent symptom even
  for eligible accounts: QR already expired (our code refreshes 3× over 5 min,
  but a stale render still scans to nothing) [astrbot-oc]; app below the version
  floor; ClawBot plugin present but not enabled; or an endpoint/`bot_type`
  drift on Tencent's side. But the dominant cause given "完全没反应" is **no
  ClawBot grayscale on that account.**

So our code is doing the right protocol against a backend the owner is **not
eligible to use yet.** No code change makes that QR scannable; it needs the
account to receive the ClawBot grayscale (out of our control) — OR a different
backend (WeChatPadPro).

---

## 4. Recommendation: add a `weixinpad` adapter against user-run WeChatPadPro

Keep `weixin.py` (iLink) as-is — it is the *officially sanctioned, lowest-ban-risk*
path and will work the moment a user has ClawBot. Add a **second, independent
adapter** for the "works today, any account" experience. This is a self-hosted
gateway answer, stated plainly: **the realistic one-QR-and-it-works path requires
the user to run a WeChatPadPro docker container.** There is no pure-stdlib,
zero-infra way to do personal-account login; the device-login session must be
held by a Pad-protocol gateway.

### What the user sets up (document this in setup, don't hide it)

- `docker compose up` a WeChatPadPro stack: the WeChatPadPro container + MySQL +
  Redis (a 3-service compose; upstream ships one). [ccino][padpro]
- Pick an `adminKey`; note the host + API port (e.g. 38849).
- Provide LunaMoth's `messaging.json` adapter config:
  `{"weixinpad": {"host": "...", "port": 38849, "admin_key": "..."}}`.

### Minimal API surface the adapter calls (verify exact paths against the
pinned WeChatPadPro release before coding — paths have drifted between versions,
e.g. the `authKeys` response-shape change [issue-2041])

1. **Get/refresh auth key** from `admin_key` (admin → per-account `auth_key`).
   WeChatPadPro: a `GenAuthKey`-style admin endpoint. [deepwiki-pad]
2. **Get login QR** (`/login/...QrCode...`) → render to terminal ASCII (reuse our
   existing `qrcode` + `qr_fallback_url` helpers from `weixin.py`).
3. **Poll login status** until confirmed → persist `auth_key` per `wxid` to a
   `weixinpad_state.json` next to our existing `weixin_state.json`
   (`default_state_path()` pattern is already there to copy).
4. **Receive:** open `ws://host:port/ws/GetSyncMsg?key=<auth_key>` on the
   adapter's `run()` thread; decode pushed messages → `InboundMessage`
   (sender `wxid`, text). [issue-1909] WS means we add one dependency OR
   implement a minimal RFC6455 client; given our "stdlib-friendly" rule, prefer
   a tiny `websockets`/`websocket-client` dep behind the existing optional
   `messaging` extra (we already optionally import `qrcode`). A long-poll HTTP
   fallback exists in some builds if we want to stay pure-stdlib.
5. **Send:** HTTP POST send-text to a target `wxid`
   (`/api/.../message/sendText` or `sendFile` for text). Pure stdlib `urllib`,
   exactly like `WeixinAPI` today. [padpro]

### Fit to our architecture

- Subclass `Adapter` (`messaging/base.py`): `name="weixinpad"`, `run(inbox)`
  owns the WS reader loop, `send(text)` does the REST POST, `set_reply_target`
  maps to the inbound `wxid`. Register it in `make_adapters()` in
  `gateway.py` next to `weixin`/`qq`/`telegram`.
- Say-channel-only, supervised by the existing `MessagingGateway` /
  lunamothd — no new supervision needed; it's one more thread.
- Unlike iLink, WeChatPadPro can usually **initiate** to any friend `wxid`
  (no "human must speak first" `context_token` gate), so `DeliveryDeferred`
  largely goes away — nicer for unattended `speak`. (Subject to risk-control;
  see below.)

### Effort estimate

- **Text-only adapter:** ~1–1.5 days incl. tests (mirror `tests` for `weixin`:
  fake opener for REST, fake WS frames; `poll_once`-style seam for the WS
  decode). The login dance + state persistence is the bulk; send is trivial.
- **Media (image/voice):** out of scope for v1, same as our iLink adapter.
- **Risk:** the moving target is WeChatPadPro's API versioning — pin a release,
  document it, and gate exact endpoint paths behind config so a version bump
  doesn't require a code change.

### Ban-risk & legality (surface this to the user, no fallbacks/hiding)

- WeChatPadPro uses the **unofficial WeChat Pad/iPad protocol — NOT sanctioned
  by Tencent.** Real account-suspension risk; upstream itself warns: new
  accounts should "stabilize 3 days" before high-risk ops, new logins can be
  force-disconnected within 24h, and friend-add / bulk ops trigger 7-day to
  30-day bans. [padpro] Recommend: a secondary/burner WeChat, keep phone logged
  in same region, low message rate.
- By contrast, **iLink/ClawBot is Tencent-official → effectively no ban risk**;
  that's why we keep it. The two adapters are a deliberate trade:
  *official-but-gated* (iLink) vs *works-now-but-risky* (WeChatPadPro). Let the
  user choose per-account in `messaging.json`.
- License: WeChatPadPro's README does not state a clear license — flag this; we
  only *talk to* the container over HTTP/WS, we don't vendor it, so LunaMoth's
  license is unaffected, but the user is responsible for running it.

---

## 5. One-paragraph answer for the owner

Your QR scans to nothing because our adapter (correctly) talks to Tencent's
official ClawBot/iLink API, and that QR binds your account to WeChat's **ClawBot
plugin**, which is a **grayscale feature your WeChat app hasn't been granted yet**
(needs iOS ≥ 8.0.70 / Android ≥ 8.0.69 *and* the ClawBot rollout to reach your
account). It's an eligibility gate, not a bug — nothing we change makes that QR
work until ClawBot reaches you. The "one QR and it just works" you saw in AstrBot
is a **different** path: AstrBot + **WeChatPadPro**, a self-hosted gateway you run
in Docker that speaks WeChat's iPad protocol and produces a *real device-login QR*
(works on any account, no grayscale). We can add a second LunaMoth adapter
(`weixinpad`) against a user-run WeChatPadPro container — ~1–1.5 days, text-only,
say-channel — but it carries genuine account-ban risk (unofficial protocol), so
it's the "works today, use a spare account" option alongside the official iLink
one.

---

## Sources

- [astrbot-oc] AstrBot — Connect Personal WeChat (weixin_oc):
  https://docs.astrbot.app/en/platform/weixin_oc.html — and zh:
  https://docs.astrbot.app/platform/weixin_oc.html
- [wiki-oc] AstrBot wiki zh-platform-weixin_oc (ClawBot plugin + version floor
  iOS 8.0.70 / Android 8.0.69):
  https://github.com/AstrBotDevs/AstrBot/wiki/zh-platform-weixin_oc
- [zhihu-claw] 微信 ClawBot 插件支持个人微信 / 灰度说明:
  https://zhuanlan.zhihu.com/p/2019077006235554955
- [drivers] 快科技 — 微信官方 ClawBot 插件:
  https://news.mydrivers.com/1/1111/1111107.htm
- [cnblogs-claw] 微信 ClawBot 完整安装教程:
  https://www.cnblogs.com/weixinjiqiren/p/19850902
- [siverking] weixin-ClawBot-API (same openclaw-weixin backend, drop-in):
  https://github.com/SiverKing/weixin-ClawBot-API
- [astrbot-pad] AstrBot — WeChatPadPro setup:
  https://docs.astrbot.app/en/deploy/platform/wechat/wechatpadpro_legacy.html
- [padpro] WeChatPadPro repo (Go, Pad protocol, MySQL+Redis, risk-control notes):
  https://github.com/WeChatPadPro/WeChatPadPro
- [deepwiki-pad] WeChatPadPro DeepWiki (REST + WebSocket, admin_key, GenAuthKey,
  Redis sessions): https://deepwiki.com/WeChatPadPro/WeChatPadPro
- [ccino] 通过 Docker 搭建 WeChatPadPro 微信机器人 (compose, port 38849, adminKey,
  QR in AstrBot logs, credentials.json):
  https://blog.ccino.org/p/building-a-wechat-pad-pro-robot-using-docker/
- [linuxdo] AstrBot + WeChatPadPro 搭建微信机器人:
  https://linux.do/t/topic/1341292
- [80aj] AstrBot + WeChatPadPro setup guide (EN):
  https://www.80aj.com/2025/12/19/new-wechat-bot-solution-astrbot-wechatpadpro-setup-guide-en/
- [issue-1909] AstrBot issue — WS `ws://.../ws/GetSyncMsg?key=***`:
  https://github.com/AstrBotDevs/AstrBot/issues/1909
- [issue-2041] AstrBot issue — new WeChatPadPro auth-code (`authKeys`) shape change:
  https://github.com/AstrBotDevs/AstrBot/issues/2041
