# webui → 后端需求单（Fable webui 任务）

写给主管：以下是 web 前端重设计需要、但不属于 `front/web/` 范围的后端能力。
按紧急度排序。落地前 UI 一律做"等待后端"占位态，不自己实现。

## 1. attach 不唤醒 resting chara（时效性高——supervisor 正在实现 presence，请直接做进去）

Owner 拍板（2026-06-12）：**进房间 ≠ 叫醒人**。

- attach / `presence.set {present:true}` 对 `rest_until` 未到的 chara 必须是
  **无声的**：只登记 presence 事实，不打断 rest，不触发 on_attach 招呼、
  不触发任何 LLM 轮。
- 用户**消息**永远唤醒（rest 工具既有语义，不变）。
- 它醒来时（自然醒或被消息叫醒），presence/"你来过"作为 env fact 让它自己
  看到、自己决定要不要理会——引擎不替它招呼。
- UI 侧配合：resting 时不显示"它知道你来了"提示（那是假话），界面做
  沉睡氛围 + "说话会唤醒它" placeholder。

## 2. `works.read {name, rel}` — 作品页应用内预览

Owner 新增需求：每个 chara 有与聊天同级的「作品」页（数据源用现成的
`works.list`）。缺一个读内容的 RPC 做应用内预览：

- 入参 name + rel（`works.list` 返回的相对路径）；路径必须确认在
  sandbox 的 workspace/、files/ 之内（防穿越）。
- 返回 `{kind: "text"|"image"|"binary", content|data_uri, size, truncated}`；
  建议大小上限 ~512KB，超限返回 truncated 让 UI 提示用 `works.open`。
- 落地前 v1 用 `works.open`（Finder 展示）兜底，本机够用、远程不行。

## 3. 头像重新生成 RPC（如 `card.avatar_draft`）

Owner 提了优先级：点头像 → 编辑器，路径之一是"AI 重新生成"。

- 入参：一句描述 + 卡的 persona 摘要（或直接传 card path）；
- 出参：2–3 个候选 `avatar_svg`（走既有 `_sanitize_avatar_svg`，
  viewBox 64×64 / ≤1500 字符约束不变）+ 可选 theme_color 建议。
- 现状只有整卡生成（cards.draft）里捎带 avatar，没有单独重生成。
- 编辑器另两条路径（换主题色、直接改 SVG）纯前端，无需后端。

## 4. 沙盒终端：PTY over WS

Owner 新增需求：「终端」页与聊天同级，是用户进入 chara 沙盒的 shell。

- 形如 `/chara/<name>/pty` 的 WS 端点（Hermes `/api/pty` 同构），
  shell 起在该 chara 的隔离边界内（dir/sandbox-exec/docker 同款），
  token 鉴权同既有 WS。
- curriculum 注记（不擅自决定，留给 owner）：操作员在它沙盒里敲的命令
  不进 transcript——它不知道你动过它的家。要不要让它知道（env fact），
  是设计问题不是管道问题。

## 5. `weixin.qr {name}` — 个人微信扫码流程（任务书既有条目，登记备查）

返回 qrcode_img_content + 登录态轮询。落地前网关配置页做占位
（"启动网关后在终端扫码"）。

## v2 / 暂不做（登记免得丢）

- **卡片自定义状态词**：`extensions.lunamoth` 允许卡片覆盖 life.state 的
  展示词（石像的 resting 可以叫"风化"）。引擎侧的【听着】这类姿态文案
  本次在前端全部删除，保留的状态文案只许事实陈述。
- **多 key 管理**（维护多把 key 任选）：任务书已明确本次不做，等后端 RPC。
- **作品 → 会话消息回溯**（Hermes Artifacts 的 session 列）：需要后端记录
  文件 ↔ 工具调用映射，v2。
