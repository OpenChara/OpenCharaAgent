# LunaMoth 角色美术资产集成 — 审查报告

> 只读审查，未改动任何代码/资产。分支：`english-only-prompt-layer`（工作树未提交改动）。
> 范围：`visuals/` 离线 pipeline + `src/` 运行时集成（protocol / agent / hub / supervisor / 前端 / 卡片迁移 / 测试）。

整体判断：核心设计方向正确——`Attachment` 协议事件向后兼容；`_resolve`/`_asset_rel`
路径防穿越是三处实现里最扎实的；卡片 → 资产 schema 基本对齐。但存在 **1 个会让
bundled 卡片整体从仓库消失的致命问题**，以及一组围绕 `send_file` 的架构不一致与若干
违反项目硬规则的"静默失败"。

---

## 🔴 Critical（必须先修）

### C1. `.gitignore` 未随卡片目录迁移更新 → 提交后所有 bundled 卡片会从仓库消失
- **位置：** `.gitignore:88-94`
- **现象：** 卡片布局从扁平文件（`cards/Quinn.en.json`）迁移到每角色目录
  （`cards/Quinn/card.json` + 美术 sidecar），旧扁平文件被删除，但 `.gitignore` 仍是
  `cards/*` 且只 allowlist 了那 4 个**已删除**的扁平文件。
- **确认：** `git check-ignore` 显示 `cards/Quinn/card.json`、`cards/Hoshi/card.json`、
  所有 avatar/sprite/webp/stickers 全部被忽略；`git status` 中也没有任何 untracked 项。
- **后果：** 一旦按现状提交，新克隆 / `lunamoth update` 后 `cards/` 只剩 `.gitkeep`，
  `default_character_path()` 返回 `None`，引擎退回 `_FALLBACK_PERSONA`，没有默认角色。
- **修复方向：** allowlist 改为目录形式（`!cards/*/`、`!cards/*/card*.json`、
  `!cards/*/*.png`、`!cards/*/*.webp`、`!cards/*/stickers/`、`!cards/*/stickers/*.png`…）。
  注意 git 必须先重新包含目录本身才能递归进入。

---

## 🟠 Moderate（架构 / 安全 / 规则违反）

### M1. `send_file` 用 data-URI 内联（base64，最大约 11MB），与 hub 刻意的 `/asset` URL 策略自相矛盾
- **位置：** `core/agent.py`（send_file → attachment payload）、`core/llm.py`（发 Attachment）
- `agent.py` 把文件 base64 塞进 `Attachment.url`；而 `hub.py:_asset_url` 注释明确说重资产
  要走可缓存 URL、"不要让 list_cards 携带 megabytes 的 base64"。同一份美术，两套机制。
- 该 data-URI 帧进入内存 `FrameRing`（容量 4096），rejoin 时 `replay_after` 整份重放——
  几张图即可在常驻 `lunamothd` 钉住数十~数百 MB / 角色。

### M2. 发送的附件不持久化，刷新/重启后丢失
- **位置：** `core/transcript.py`、`protocol/api.py`（snapshot/restore）
- 只有小的 tool-result 元数据行进 transcript，图片本体仅存在于易失的 FrameRing。
  重载后用户只看到 `⚙ send_file ✓`。
- M1+M2 同源：把发送文件落到一个被服务、可持久、带鉴权的位置，可一次性解决
  "资源占用 + 持久化 + 鉴权"三件事。

### M3. `/asset` 路由完全无鉴权
- **位置：** `server/supervisor.py`（`do_GET` / `_serve_asset`）
- `do_POST` 用 `hmac.compare_digest` 校验 token，但 `do_GET` 里 `/asset` 直接服务、无 token。
  任何能访问该 HTTP 端口的进程都能用 `?p=<绝对路径>` 枚举读取 card decks 和**所有 session
  目录**下任意图片扩展名文件。
- 路径限定本身是对的（`resolve()` + parents 检查），但：(a) 与 token 化的 RPC 不对称，
  需 owner 拍板；(b) `.svg` 以 `image/svg+xml` + `Cache-Control: public` 返回，
  若受控 svg 落入某 root，是存储型 XSS 向量。

### M4. `_stage_art_assets` 把文件系统写操作塞进 `_stable_prefix()`（prompt-cache 构建器）
- **位置：** `core/agent.py`（`_stage_art_assets`，由 `_stable_prefix` 调用）
- 违背"稳定前缀字节级不变、可跨进程命中缓存"的契约：`/reset`、reconfigure 都会重跑。
- 因 `if not target.exists()` 守卫，若角色改/删了 `assets/sprite.png`，note 仍声称
  "a full-body portrait"存在——身份提示词出现可能为假的断言。
- 建议挪到 session 激活处，而非前缀组装。

### M5. `wake()` 的 `_copy_card_assets` 用模板卡，但冻结写盘的是编辑后的 `card_data`
- **位置：** `server/hub.py`（`wake` / `_copy_card_assets`）
- 若 wake 两步编辑器改了 `assets` 块（增删改 sprite/sticker），拷贝的文件与冻结声明不一致：
  `card.asset_path()` 指向没拷过去的文件（→ 缺图）或拷了已不被引用的文件（→ 孤儿）。
- 应从产出 `card.json` 的同一个 card 对象去冻结。

### M6. 前端三处新 `<img>` 缺 `onerror` 兜底 → 破图，违反"silent waits are a bug"硬规则
- **位置：** `front/web/chat.js`（附件图、sprite 图）、`front/web/index.html`（静态 `chat-sprite-img`）
- sidecar 拷贝是 best-effort 静默跳过的，一旦缺失就是无诊断破图图标。
- 对照 `app.js` 上传路径有 `onerror`——此处属回退。

### M7. hub 在每个 `hub.state` 发 `keyvisual_url` / `stickers_urls`，但前端无人消费；deck 不显示角色美术
- **位置：** `server/hub.py:_card_entry`（产出字段）vs `front/web/app.js`（消费方）
- `chat.js` 只用 `bg_url`/`sprite_url`，无 sticker picker（`grep sticker` 前端零命中）；
  deck（`app.js`）只显示 avatar。
- 要么功能未做完，要么是常广播消息里的无用负载。任务简介里"deck 显示
  sprite/keyvisual/stickers"并未实现。

### M8. 美术相关代码零测试覆盖
- **位置：** `tests/test_cards.py`、`test_desktop_hub.py`、`test_hub_webrpc.py`、`test_supervisor.py`
- diff 全是机械路径重命名。安全敏感的 `_serve_asset` 路径限定、`asset_path`/`_resolve`
  防穿越、`_stage_art_assets`、`_copy_card_assets` 全部无测试——其中 `/asset` 端点是
  风险最高的未测代码。

---

## 🟡 Minor

- **m1.** `send_file` 双读文件；8MB 上限只在 tool handler 校验，agent-loop 的 `read_bytes()`
  无尺寸检查（TOCTOU，角色可换大文件）。
- **m2. 静默吞异常违反"可见错误"规则：** `agent.py` `except Exception: pass`——工具已返回
  `delivered=True` 但实际无附件送达、用户毫不知情；`_stage_art_assets`、`_copy_card_assets`
  的 `except OSError: pass` 同理。
- **m3.** `chat.js` 把 `bg_url` 注入 CSS `url()` 只转义了 `"`（服务端已预编码，风险低但脆弱，
  宜用 `CSS.escape` 或 CSS 自定义属性）。
- **m4. pipeline 文档/代码漂移：** `cardbrief.py:4` docstring 写 "DeepSeek V4 Flash" 实际是
  `gemini-3.1-pro`；`genviz.py:12` 写 "4x4=16 stickers" 实际 3×3=9；`rematte.py` 仍硬编码旧
  4×4（对当前 3×3 sheet 会错切）；README 密钥表漏了 `REMOVEBG_API_KEY`。
- **m5.** pipeline 的 `assets.json` 产出 `avatar` 键，但 runtime 经独立的 `avatar_file` 字段
  消费 avatar，**不是** `assets.avatar`——直接照搬 manifest 会被静默忽略。
- **m6.** 三套互相分叉的抠图/去边实现（`genviz.cutout`、`localmatte`、`rematte`），只有
  `localmatte` 被 build pipeline 调用；`genviz` 的抠图助手与 `crop_grid` 是死代码。
- **m7.** `detect_language` 对新目录布局基本失效（stem 恒为 `card`，纯中文 `card.json` 仍报 `en`）。
- **m8.** 只有 LunaMoth/Quinn 带 `card.zh.json`，6 个新角色仅英文（作者层面不一致，非 bug）。
- **m9.** `rematte.py` 在 import 时即建 isnet session，违背 pipeline 其余处的惰性加载约定。

---

## ✅ 做对了 / 别去"修"它（避免工作模型误改）

- `_resolve`/`_asset_rel` 防穿越扎实，应作为另两处的范本。
- `codec`/`events` 的 `Attachment` 注册向后兼容（未知类型客户端忽略）。
- `el()` 用 `createTextNode`，无 innerHTML 注入；url/caption/name 不能注入标记。
- 默认卡解析正确：Quinn 在中英两语都是 default。
- 8 个角色资产集完整统一——**K-9 的 stickers 00–08 齐全，先前怀疑不成立**。
- `visuals/` 确实未被 `src/` import（仅注释中出现）。
- 无角色名泄漏进 `src/`。
- 当前无 CSP，故 data: 图能渲染（CSP 缺失是既有问题，非本次引入）。
- pipeline 的 `sprite`/`background`/`keyvisual`/`stickers` 键与 runtime
  `extensions.lunamoth.assets` schema 对齐（仅多出的 `avatar` 键见 m5）。

---

## 主线建议（给工作模型）

1. **C1 先修**——否则一切都会随提交丢失。
2. **M1 / M2 / M3 合并处理**：让 `send_file` 落盘到一个被服务、可持久、且与 RPC 一致鉴权的
   位置并返回 URL（而非 data-URI），可同时解掉资源占用、持久化、鉴权三项。
3. 补 **M8** 的安全路径测试（`_serve_asset` 限定、`_resolve`/`asset_path` 防穿越）。
4. 决定 **M7**：要么把 deck 接上 `keyvisual_url`/`stickers_urls`，要么停止在 `hub.state` 里发它们。

---

## 严重度速查表

| # | 问题 | 严重度 |
|---|------|--------|
| C1 | `.gitignore` 仍 allowlist 已删扁平卡，新目录全被忽略 → bundled 卡片消失 | Critical |
| M1 | `send_file` data-URI 内联，撑爆内存 FrameRing 且 rejoin 重放 | Moderate |
| M2 | 附件不持久化，刷新/重启后丢失 | Moderate |
| M3 | `/asset` 路由无鉴权（与 token 化 RPC 不对称）+ svg XSS 隐患 | Moderate |
| M4 | `_stage_art_assets` 在 prompt-cache 构建器里做 FS 写 | Moderate |
| M5 | `wake` 按模板卡冻结资产，但冻的是编辑后的卡 → 缺图/孤儿 | Moderate |
| M6 | 三处新 `<img>` 无 `onerror` → 破图（违反硬规则） | Moderate |
| M7 | `keyvisual_url`/`stickers_urls` 无消费方；deck 不显示美术 | Moderate |
| M8 | 美术代码零测试覆盖（安全路径未测） | Moderate |
| m1–m9 | TOCTOU/静默吞异常/文档漂移/死代码/惰性加载等 | Minor |
