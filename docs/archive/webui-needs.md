# webui ↔ 后端需求单（Track B 登记，Track A 实现并写回执）

写给 webui：缺什么后端能力就**追加**到「待办」；做完会移到「可接线」。
落地前 UI 一律做"等待后端"占位态，不自己实现后端。

## 可接线（后端已在 main，前端按此契约直接用）

- `works.read {name, rel}` → `{kind: text|image|binary, content|data_uri,
  size, truncated}`；512KB 截断旗标，超限提示用 `works.open`。
- `messaging.get/save {name}`：秘密掩码读；保存按平台**字段级合并**——
  省略=保留、掩码原样回传=保留、显式 null=删除。
- `weixin.qr {name}` → `{qrcode, img, fallback_url}`；轮询
  `weixin.qr_status {name, qrcode}` → `{status[, account_id]}`，confirmed
  自动持久化登录态（网关启动即已登录）。
- 终端页：`ws://…/chara/<name>/pty?token=…&cols=…&rows=…`（xterm.js；
  二进制帧双向；resize 发整帧 `\x1b[RESIZE:<cols>;<rows>]`；chara 未运行
  也能开 —— 进的是它的家，不是它的进程）。
- `card.avatar_draft {description|card_path}` → `{candidates:
  [{avatar_svg, theme_color}], notes}`（≤3 个 sanitized 候选，全废=可见错误）。
- `card.duplicate {path}`：副本带「（副本）/ (copy)」后缀、剥 default tag、
  PNG 自动提为 JSON —— **复制按钮从 card.read+card.save 切到它**。
- `card.merge_world {card_path, world}`：独立世界书并入卡内嵌 book；
  `/upload` 对世界书返回 `{kind:"world"}`，可做"并入卡 X"。
- `/model <id>` 命令：session 范围热切换、不写回默认、空参回显，
  Reply.data 带 `{model, context_max}` —— 模型弹层可接线。
- `session.wake` 接受 `embodiment: "literal"|"actor"` —— **唤醒 sheet 要把
  它发上来**（运行中不出现切换 UI）；tempo 已全移除（删控件/文案）。
- attach 不唤醒 resting chara；无言到访零痕迹；常驻 chara 一生只招呼一次
  （重开页面不再重放招呼）。UI 配合：resting 做沉睡氛围 + "说话会唤醒它"。
- works.list 的点目录误杀已修（后端修复，前端无需动作）。
- **辅助模型（#14）**：`defaults.set {aux_models: {draft?|transcribe?|
  avatar?|compact?}}`（空值=回到主模型，未知任务=报错）；public defaults
  带 `aux_models`；cards.draft/transcribe.card/card.avatar_draft 未显式
  传 model 时自动用对应辅助模型。compact 仅存储，core 接线后启用。
- **多 key（#10，按 UI 契约原样落地）**：`keys.list` →
  `[{label, provider, base_url, model, has_key, active}]`（秘密永不回传）；
  `keys.save {label, provider?, base_url?, api_key?, model?}`（更新省略
  api_key=保留）；`keys.delete {label}`；`defaults.use_key {label}`；
  `session.wake` 接受 `key: <label>`（其 model 仅在 wake 未选时填充）。
  defaults.set 不再抹掉 keys 存储。
- **`toolpacks.list`（#8/#12）** → `[{name, description, tools,
  mcp_servers, path}]` —— 唤醒 sheet 真选单可接。
- **#11 去重语义已定夺**：用户卡只遮蔽**同名同语言的内置卡**（local-first，
  同 skills），幸存条目带 `shadows: <被遮蔽路径>` 可如实展示；用户卡之间
  **永不互相遮蔽**（同名各自出现，path 即身份）。前端的「副本」自动改名
  保留即可（card.duplicate 也会改名）。

## 待办

1. **引擎读取 `extensions.lunamoth.user_name` / `user_persona`**：工坊把
   这两个字段写进卡片，引擎 persona 层目前不读 —— 要让"你是谁"真正进
   prompt，需要 wake/activation 接到 persona 机制。触及 prompt 栈，
   Track A 做，字段语义需 owner 点头。

## 13. gateway.status 增加 state 枚举（学 Hermes 的三 chip tone）

【主管回执】接受。现状已有 running/stopped/backoff + detail；完整枚举
（connecting/fatal/startup_failed + error_message、backoff 健康重置）与
审计 #27（chara 自动重启三振熔断）是同一块 supervisor 手术，下一波一起做。

## v2 / 暂不做（登记免得丢）

- **卡片自定义状态词**：`extensions.lunamoth` 允许卡片覆盖 life.state 的
  展示词（石像的 resting 可以叫"风化"）。引擎侧的【听着】这类姿态文案
  已在前端全部删除，保留的状态文案只许事实陈述。
- **作品 → 会话消息回溯**（Hermes Artifacts 的 session 列）：需要后端记录
  文件 ↔ 工具调用映射。
