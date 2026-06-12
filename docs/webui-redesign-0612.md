# Web 前端重设计 · 任务书（2026-06-12 修订版）

本文是 web 前端重设计的**唯一现行任务书**，取代
`prompt-webui-fable.md`（原始版，已随交接清理删除）。原任务书经 owner 当面修订过
多处（聊天排版、状态语义、面板分工等），**凡两者冲突，以本文为准**。
执行者假定从零接手：本文自足，不依赖会话记忆。

## 0. 任务一句话

把 `src/lunamoth/front/web/`（无构建步骤的 vanilla HTML/CSS/JS）翻新成产品级
界面。这是**演进不是重做**：owner 对现有聊天主界面非常满意，好的东西
（气泡式聊天、卡册、创建流程、删除摩擦设计、首次启动）全部保留，在其上
重新组织。

## 1. 基线与现状（2026-06-12 已核实）

- 基线 commit：main `8cdc07d`（supervisor 与 messaging-cn 均已合并）。
- 工作分支 `webui` 已建，worktree 在 `../LunaMoss-webui`，venv 已
  `uv sync --extra dev --extra server`。**代码零修改零提交**，从干净状态开始。
- 基线里**已经存在**（不要重复发明）：
  - rpc.js：seq 跟踪 + rejoin 握手 + `rejoin.gap` 回调（重连恢复已可用）；
  - app.js：life.state 渲染（board 状态行 + 聊天 work-status）、
    superchat.read 调用（目前是 ✓ 标记式，要改成淡化式）、
    右侧 drawer（状态/作品/记忆/能力/终端/网关六个 tab，后两个是占位）、
    思考折叠块、工具 chip（已带时长）、`works.list`/`works.open` 接入；
  - 后端 RPC：`hub.state`（board entry 含 `life`/`gateway`/`superchat_unread`/
    `speaks`/`preview`）、`superchat.read`、`gateway.start/stop/status`、
    `chara.start/stop`、`works.list`、`works.open`、`chara.extras`、
    `session.*`、`card.*`、`cards.draft`、`defaults.*`、`key.test`、
    `models.list`、`open.path`；
  - snapshot（per-chara `snapshot` RPC）字段：mode/model/reasoning/
    show_thinking/isolation/net_on/user_present/rest_until/quiet/
    **patience**/embodiment/context_tokens/context_max/memory_chars/
    memory_max/sandbox_root/workspace_root 等（protocol/api.py
    `StateSnapshot`）；
  - 命令注册表（core/commands.py）：/status /memory /files /read /write
    /logs /goal /skills /mcp /net /allow-dir /mode /quiet
    **/patience** /thinking /reasoning /compact /reset。

## 2. 施工纪律（约束性）

- 只改 `src/lunamoth/front/web/`、i18n 文案，及（如确有需要）`apps/desktop/`
  的 Electron 薄壳。**不碰 protocol/、server/、core/**。
- 缺后端接口**不要自己实现**：写进 `docs/archive/webui-needs.md`（已有
  五条 + 三条 v2 登记，往后追加），UI 先做"等待后端"占位形态。
- 不合并不推送；完成后留 commit 在 `webui` 分支等 owner 验收。
  commit message 以 `Co-Authored-By: Claude <noreply@anthropic.com>` 结尾。
- UI 框架文案一律走 i18n.js（zh/en 双语）；chara 的话保持卡片语言。
- `uv run python -m pytest -q` 必须全绿。浅色/深色两主题都不能破。
- 空闲循环由 supervisor 驱动，**前端绝不自己驱动 idle**。
- 多代理共仓：永不 `git add -A`，只 stage 自己的文件。

## 3. 设计基调

以 `docs/archive/hermes-ui-notes.md`（Hermes Desktop 28 张截图研究）为参照，
**学其骨不学其皮**——但下列 owner 修订**推翻**了 Hermes 研究里的对应建议：

1. **聊天保持现有气泡式**。不采用 Hermes 的"用户=边框盒、角色=裸排版"
   （owner 原话：干巴巴）。对齐方式不动。头像是重要元素（见 §4.B）。
2. **重要状态放右侧面板（默认常驻），底部栏只放相对不重要的**
   （版本、WS 连接点、会话计时一类）。和 Hermes 的"状态栏=系统托盘"相反，
   我们的右侧面板承载得比 Hermes 多。
3. 保留采纳：小型大写字距标题作为唯一分组语言；一行式设置行（粗体标签 +
   一行灰色理由 + 右侧控件）；空状态必须说明"什么会填满它"；错误指名修法；
   Appearance 加 "Product | Technical" 开关；模型默认 vs 热切换的措辞
   （"默认应用于新会话；热切换只影响当前会话"）；类似 Hermes "Fallback
   Models" 的位置渲染成一行声明"No fallbacks — failures are shown /
   不做回退——失败会如实显示"。

## 4. 需求清单（owner 验收标准）

### A. 右侧面板「状态/设置」分层（核心重构）

- 聊天页右侧面板**默认常驻**（可折叠）。顶部是**状态区**——高频、温和、
  一眼可读，点击即改：
  - 模型与提供商（点击=热切换弹层，含思考强度五档）
  - 上下文用量（used/真实窗口 + 百分比）
  - 沙盒隔离（dir/sandbox/docker）与工作目录
  - 网络 on/off（走 `/net`）
  - **自主运行 on/off**（取代旧"持续运行/对话模式"标签，走 `/mode live|chat`；
    聊天头部现有的 mode-seg 控件删除）
  - 思考强度、记忆用量、网关状态、可用 tools 与权限
- 状态区之下是**设置选单**：网关 / 能力(toolpack+tools) / 记忆——点击进
  面板内详情页（不是新页面）。原计划的「文件」详情页**取消**，并入
  「作品」同级页（见 §4.C）。
- 旧"⌘ 命令面板"里的动作（整理记忆、安静一会儿、思考、联网、重新开始）
  全部移进右侧面板对应区；**reset 放设置最底部（危险区样式）**。
  斜杠命令在聊天框仍可用。原则：**凡有 /command 的能力，右侧面板都应有
  入口**（/quiet /patience /net /mode /reasoning
  /memory /reset…——用 `handle.command()`，命令注册表现成）。
- **左右栏都可拖动调宽**：拖拽分隔条，宽度持久化 localStorage，各有最小
  宽度与折叠态。

### B. 聊天排版：保持气泡，强化头像

- 现有气泡布局、对齐、五信道区分**全部保持**。只做加法：
  - **SVG 头像进聊天**：现状缺口是卡片 `avatar_svg` 只在卡册渲染，聊天页
    （头部 + 消息行）用的是字母色块（app.js `glyphOf`/`paletteClass`）。
    改成有 `avatar_svg` 用 SVG、没有才回退字母色块。
  - **空聊天状态** = 该卡的大头像 + 名字 + tagline（每张卡一个品牌时刻）。
  - 思考折叠、工具 chip 时长已存在，保持并精修。

### C. 与聊天同级的页面：对话 | 作品 | 终端

- chara 头部（头像+名字+状态点之后）放内联 tab 组：**对话 | 作品 | 终端**。
  - 三个视图**常驻不卸载**（display 切换）：切走时聊天流继续渲染，终端
    滚动缓冲不丢。
  - 不在作品页时有新文件 → 「作品」tab 上未读点。
  - hash 路由：`#/chara/<name>`、`/works`、`/term`，刷新/后退成立。
  - 右侧面板跨 tab 常驻（状态属于"这个角色"，不属于"聊天页"）。
- **作品页**（命名用「作品/Works」，对齐后端 `works.list`）：
  类型图标+文件名 / 大小 / 修改时间 / mono 相对路径；顶部过滤 chip 带计数
  （全部/图片/文本/其他）；应用内预览需要 `works.read`（需求单已登记），
  落地前用 `works.open`（Finder）兜底；空状态："它做出东西的时候，会出现
  在这里"。会话回溯列是 v2。
- **终端页**：需要后端 PTY over WS（需求单已登记）。落地前占位：显示
  sandbox 路径（可复制）+ 一行说明。现有 drawer 的「终端」「网关」占位 tab
  随 drawer 重构吸收。

### D. 存在感知 + 状态氛围（mood layer）

**状态语义（owner 拍板，注意与 supervisor 文档直觉不同）：**

- **waiting** = 安静窗口（`engaged_until`）= **那根进度条**：倒计时，
  总长 = quiet 秒数，"条走完我就去做自己的事"；用户说话即回满。
- **idle_countdown** = 它自己两个自发周期之间的间隔（patience）=
  **机制不是情绪**（省 token + 与现实时间对齐）。**视觉上与 working 合并**
  为同一 register："在做自己的事"——不渲染倒计时条。`next_cycle_at` 只在
  Technical 模式下作为状态区一行小字（"下一周期 ~HH:MM"）。
- 设置文案：quiet =「等你多久」，patience =「它自己生活的节拍」。

**mood layer（界面有情绪，极温和）：**

- 情绪锚点是聊天框（composer）+ 背景极淡随动。两层 CSS 变量架构：
  - 第 1 层：卡片提供基色 `--chara-accent`（card `theme_color`，现只染
    卡册，要接进聊天页）；
  - 第 2 层：mood 状态只做基色变换（`color-mix` 8–15% 染量、不透明度、
    box-shadow），挂 `data-life` 属性于 chat 根。将来每张卡自定义样式只
    覆盖第 1 层。
- 每状态：waiting = 邀请感呼吸光（~4s）+ 进度条；working/idle_countdown =
  流光游走（~8s），周期间隙流速放缓；resting = 整体降亮降饱和（灯暗了），
  7–8s 超慢呼吸，placeholder「它在休息——说话会唤醒它」，**输入永远不做
  disabled 视觉**；backoff = 去饱和 + 错误色 detail，无动画。
  `prefers-reduced-motion` 时全部节律降为静态色。
- **删掉【听着】**（i18n.js `st-listening`，app.js 两处引用）：引擎不得替
  角色宣称姿态。被动在场**不配文字**——呼吸感即是"在"。保留的状态文案
  只许事实陈述（「休息到 HH:MM」「约 N 分钟后开始自己的事」）。
- **attach ≠ 唤醒**（owner 拍板；后端执行在 supervisor，已写需求单第 1 条）：
  UI 侧配合——resting 时不显示"它知道你来了"类提示（那是假话），保持沉睡
  氛围；非 resting 进入聊天时才可有一条居中淡行的到场感知。

### E. Super Chat：已读 = 淡化

- ⚡气泡页面可见时渲染 → 调 `superchat.read {name, ts}`（已接）→ 气泡
  **整体淡化**（降不透明度/饱和度），**取消现有 ✓ 文字标记**。
  未读保持鲜亮："亮的没看过、淡的看过了"。board 未读角标清零逻辑已在。
- 概念备忘：say 是协议信道，speak 是工具——它无人看管时主动决定"值得告诉
  人类"才调 speak，web 渲染成 ⚡。Technical 模式可标注信道来源。

### F. 可编辑头像（owner 提了优先级）

- **点头像即编辑**：卡片视图、聊天头部、卡册的头像都可点，同一个编辑器
  （虚化模态）。三条路径：
  1. **AI 重新生成**：一句描述 + persona 摘要 → 2–3 个候选（需后端 RPC，
     需求单第 3 条；落地前占位）；
  2. **改主题色**：取色器替换 SVG 主色，即时预览，纯前端；
  3. **直接改 SVG**：现有 textarea + 预览保留（safeSvgForPreview 已有）。
- 保存写回 `extensions.lunamoth.avatar_svg`（走 `card.save`；hub sanitize
  不变：viewBox 64×64 / ≤1500 字符）。**v1 不做上传图片**（装不下 data URI，
  且 SVG 是卡片可移植性的一部分）。

### G. 卡片浏览与工坊（沿用原任务书）

- **浏览卡片不再展示原始 JSON**：渲染成卡片视图——SVG 头像 + 主题色、
  名字、tagline、persona 摘要、世界书条目数、种子目标、toolpack、
  embodiment 徽章；「查看原始 JSON」折叠在底部留给开发者。
- **工坊改为模态层 + 背景虚化**（backdrop blur + 半透明遮罩），不再全屏
  接管。
- AI 生成后的编辑表单字段顺序（鼓励改写的放最上）：
  1. 并排两小卡：**chara 名字 | 用户名字（user_name）**；
  2. **用户自己的设定**（persona，"你是谁"）；
  3. chara 设定（description/personality/scenario）；
  4. 其余（开场白、世界书、目标、SVG、主题色、embodiment）。
- **唤醒时必须能选 toolpack**（现在不能是已知缺陷）；唤醒后 toolpack/网络/
  隔离等在右侧面板运行时可改（有命令的走命令）。

### H. 网关（per-chara，刻意不同于 Hermes 的全局连接器页）

- 网关属于每个 chara：从右侧面板「网关」进入，**弹出卡片 + 背景虚化**。
- 详情页公式照抄 Hermes：身份头 + **三枚独立状态 chip**（已启用/未启用 ·
  已配置/待配置 · 网关运行中/已停止）→ GET YOUR CREDENTIALS 白话步骤 +
  外链 → REQUIRED 字段 → RECOMMENDED（带安全理由，如 allowed_senders
  "不填则任何人都能召唤你的 chara"）→ ADVANCED 折叠 → 左下启用开关 +
  右下保存。
- 平台与字段（messaging.json → `adapters.<platform>`；后端 adapter 代码已
  合并在 `src/lunamoth/messaging/`）：
  - `wecom`（企业微信自建应用）：REQUIRED corp_id / secret / agent_id /
    token / encoding_aes_key；RECOMMENDED to_user、顶层 allowed_senders；
    ADVANCED host / port(8128) / path / api_base。注明需要公网回调。
  - `weixin`（个人微信 iLink，扫码）：无必填——凭据是扫码后自动持久化的
    状态文件；ADVANCED base_url / bot_type("3") / long_poll_timeout_ms /
    api_timeout_ms。主体是**扫码流程区**：`weixin.qr {name}` RPC 落地前做
    占位（"启动网关后在终端扫码"）。提示行注明 chara 只能主动联系"本会话
    中先开口过"的用户（context_token 机制）。
  - `qq`（OneBot v11 / NapCat）：REQUIRED url(ws://…) / peer_id（"你自己的
    QQ 号"）；RECOMMENDED access_token、allowed_senders；步骤区白话三步
    （跑 NapCat → WebUI 扫码 → 开 forward WebSocket 粘贴地址）。
- 运行状态用 board `gateway` 字段 + `gateway.start/stop/status` RPC（已有）。
- **配置读写 RPC 契约**（`messaging.get {name}` / `messaging.save {name,
  config}`，秘密字段回传掩码 `"••••"`、UI 留空=不修改）：开工时先 grep
  hub.py 确认是否已落地；没有则按契约编码 + 写需求单 + 占位。

### I. 杂项

- 全局空状态都说明"什么会填满它"。错误指名修法（哪个 key、去哪改）。
- Appearance 加 **"Product | Technical"** 开关：Product 隐藏原始载荷
  （OC 创作者），Technical 显示完整输入输出（开发者）。
- 多 key 管理本次不做（需求单已登记）。

## 5. 协议契约（不要自己发明帧）

- `life.state` 通知：`{state: working|waiting|resting|idle_countdown|backoff,
  next_cycle_at, rest_until, engaged_until, detail}`（CharaClient.onLifeState
  已接线）。
- 重连：rpc.js 已带 seq/rejoin；只处理 `rejoin.gap` 时的全量恢复 UI（已有）。
- `superchat.read {name, ts}`；board entry 含 `superchat_unread`/`gateway`/
  `life`/`speaks`。
- `StateSnapshot.patience`；`/patience` 命令。

## 6. 后端缺口（详见 `docs/archive/webui-needs.md`）

1. attach 不唤醒 resting chara（supervisor 侧；UI 按 §4.D 配合）
2. `works.read {name, rel}`（作品页应用内预览）
3. 头像重新生成 RPC（如 `card.avatar_draft`）
4. 沙盒终端 PTY over WS
5. `weixin.qr {name}` 扫码流程
6. v2 登记：卡片自定义状态词 / 多 key 管理 / 作品↔会话回溯

## 7. 验收路径（完成后逐条自测）

1. 打开聊天：空状态=头像+名字+tagline；右侧面板默认常驻、状态区每项可读
   可点；左右栏可拖宽；底部只有版本/连接/计时类轻信息。
2. 说一句话→邀请感呼吸 +「等你回复」；停止说话→**waiting 进度条**走完→
   它开始做自己的事（流光，无倒计时条）；`/rest` 后界面灯暗、显示休息到
   几点、输入仍可用；说话唤醒。
3. 收到 Super Chat→⚡卡片鲜亮→看到后整体淡化（无✓文字）→board 角标清零。
4. 对话|作品|终端 tab 可切换；作品页列表+过滤+空状态；终端页占位形态；
   切走再回来聊天流无缝。
5. 点头像→编辑器三路径（AI 生成为占位态）；改主题色即时预览；保存后
   卡册/聊天/board 都更新。
6. 浏览卡片=卡片视图非 JSON；工坊=虚化模态；生成后名字/用户设定在最上；
   唤醒能选 toolpack。
7. 网关配置=虚化模态+三状态 chip+三平台表单；i18n 中英无裸字符串；
   浅深色都不破；`pytest -q` 全绿。
