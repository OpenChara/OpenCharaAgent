# Security

OpenCharaAgent runs an AI agent that executes shell commands, reads/writes files, and
(optionally) drives a browser. The agent and anything it generates are treated as
**untrusted**. This document states what the trust model protects, what it does
*not*, and how to report a vulnerability.

## Threat model

The adversary is the model (or content it ingests) attempting to:
- escape the per-session jail to read or write outside its workspace,
- read host secrets — the OpenCharaAgent API key and login hash in `~/.chara`, other
  charas' sessions, the transcript database, process environments,
- reach the privileged JSON-RPC gateway from agent-generated content,
- exfiltrate data over the network.

## What the trust model protects

**Filesystem confinement (two layers).** Under the default `sandbox` isolation the
`terminal`/`execute_code`/`search` tools run behind an OS jail — `sandbox-exec`
(macOS), `bubblewrap` → `Landlock` (Linux) — built by one shared
`build_jail_command` so the foreground, background and PTY paths cannot drift.
Writes are confined to the workspace; `~/.chara` (the global key, the login
hash, every other chara's session) is unreadable from inside. On top of the OS
jail, the file tools resolve symlinks and `..` against the workspace and refuse
paths that escape it (`tools/builtin/_pathsec.py`). **No silent degradation:** if
no jail is available, the tool *refuses* rather than running unconfined — only an
explicit `admin` isolation opts out of the jail.

**Credential hygiene.** Provider keys are stripped from every child environment;
the per-turn request log and compaction summaries are run through a credential
redactor before they touch disk; the global key is never copied into a session
config (only resolved at load from the keyring).

**Web surface (the JSON-RPC/WebSocket gateway).** Host/Origin allow-listing
(anti-DNS-rebinding / anti-CSWSH) checked before auth; constant-time token
comparison; `HttpOnly; SameSite=Strict` cookies; optional password login is
PBKDF2-HMAC-SHA256 (600k iterations) with a per-IP rate limiter, the hash stored
`0o600` and the plaintext never persisted. File-serving routes (`/asset`,
`/chara/<name>/home/*`) resolve-then-confine to a single subtree and keep session
secrets off the route.

**Personal-website rendering.** A chara's homepage is served read-only with a
hardened CSP (`connect-src`/`form-action 'none'`) and rendered in a sandboxed
iframe (`allow-scripts`, no `allow-same-origin`); the iframe URL carries no token,
so chara-authored JS cannot reach the RPC or read the app credential.

**Browser tool.** `browser_navigate` (and the navigation verbs of `browser_cdp`)
enforce a URL scheme allow-list (`http`/`https`/`about`) plus an SSRF guard that
resolves DNS and blocks private/loopback/link-local ranges and the cloud metadata
IP — so a chara cannot `file://`-read host secrets or pivot to internal services.

## Known limitations (by design — name them when you deploy)

- **`admin` isolation runs with no jail**, at your privileges. It is opt-in and
  intended for a trusted operator; do not give an `admin` chara to untrusted input.
- **The browser jail is deliberately looser than the shell jail.** A real Chromium
  cannot nest its own sandbox inside the OS jail, so it runs with `--no-sandbox`;
  the macOS browser profile is allow-by-default with `~/.chara` denied (so the
  *OpenCharaAgent* secret is protected, but other `$HOME` dotfiles such as `~/.ssh` are
  not specifically hidden from a browser the chara drives), and the Linux browser
  jail keeps host `/proc` visible. Run browser-enabled charas accordingly.
- **Landlock (the Docker/no-userns Linux tier) cannot gate the network** (ABI v1).
  With `sandbox` + Landlock, `/net off` is not enforced; the operator is warned in
  the log. Use a bwrap-capable host if you need enforced network-off.
- The bundled gateways (WeChat/QQ/Telegram) have not been live-tested against
  production credentials; treat them as beta.

## Reporting a vulnerability

Please do **not** open a public issue for a security report. Email the maintainer
at lunamos.thu@gmail.com with a description and, ideally, a reproduction. We aim
to acknowledge within a few days.
