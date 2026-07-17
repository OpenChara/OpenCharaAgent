#!/usr/bin/env python3
"""Build a self-contained old->new chat-UI mockup from the generated assets.

Shows the SAME current chat layout (centered column, small avatar + name + text,
accent user bubbles, gold speak card) with only a richer skin: Q-ver avatar,
a faint character background, a subtle 立绘 watermark, and a sticker reaction.
Downscaled + embedded so the file is light and never blocks.
"""
import base64, io, json
from pathlib import Path
from PIL import Image

OUT = Path(__file__).parent / "out"
CHARS = {
    "Vesper": {"name": "Vesper", "theme": "#5E7C4F", "theme2": "#C9A84A",
               "tag": "Hedge-witch & cartographer", "sticker": 1},
    "K-9": {"name": "K-9", "theme": "#FF2D8E", "theme2": "#28E0D0",
            "tag": "Runner · ex-company dog", "sticker": 8},
    "Hoshi": {"name": "Hoshi", "theme": "#5B4BE6", "theme2": "#FF8FD0",
              "tag": "rookie virtual idol ⭐", "sticker": 2},
}
LINES = {
    "Vesper": [("char", "Oh — good, you're back before the dark. I've started the page for the northern spit; there's a spring the old maps swear doesn't exist."),
               ("user", "Show me what you found today."),
               ("speak", "Come look — a ley-line crosses right under that spring. That's why it's there. I'm inking it now.")],
    "K-9": [("char", "Channel's clean, I swept it twice. Last night's run is in the log."),
            ("user", "You bringing me work or just checking I'm alive?"),
            ("speak", "Both. Hóngténg moved people through a shell clinic — I pulled the manifest before their ICE woke up.")],
    "Hoshi": [("char", "Ohh you're here, you're here — okay we're not even live yet, this is just us! ⭐"),
              ("user", "Ready for the next stream?"),
              ("speak", "Chat hit the goal!! ...you really showed up for me today. (don't make me cry off-stream.)")],
}


def b64(img: Image.Image, fmt="PNG", **kw) -> str:
    buf = io.BytesIO(); img.save(buf, fmt, **kw)
    mime = "jpeg" if fmt == "JPEG" else fmt.lower()
    return f"data:image/{mime};base64," + base64.b64encode(buf.getvalue()).decode()


def load(name):
    base = OUT / name
    av = Image.open(base / "avatar.png").convert("RGBA"); av.thumbnail((180, 180))
    bg = Image.open(base / "background.jpg").convert("RGB")
    bg = bg.resize((1000, int(1000 * bg.height / bg.width)))
    sp = Image.open(base / "sprite.png").convert("RGBA")
    sp = sp.resize((int(560 * sp.width / sp.height), 560))
    st = Image.open(base / "stickers" / f"sticker_{CHARS[name]['sticker']:02d}.png").convert("RGBA")
    st.thumbnail((240, 240))
    return {"avatar": b64(av), "bg": b64(bg, "JPEG", quality=78),
            "sprite": b64(sp, "WEBP", quality=82), "sticker": b64(st)}


data = {k: {**CHARS[k], "lines": LINES[k], **load(k)} for k in CHARS}

HTML = """<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenCharaAgent 聊天界面 · 现在 vs 接入素材后</title>
<style>
:root{--accent:#5B9FD4;--text:#1D2730;--text-2:#5F7280;--text-3:#93A3AE;
  --bg:#F5F6F8;--panel:#FFFFFF;--hairline:rgba(20,40,60,.10);--radius:10px;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
*{box-sizing:border-box}
body{margin:0;font-family:var(--sans);color:var(--text);background:#E9ECF0;padding:22px;font-size:14px;line-height:1.55}
h1{font-size:17px;margin:0 0 4px}.sub{color:var(--text-2);font-size:13px;margin:0 0 16px}
.switch-row{display:flex;gap:8px;margin-bottom:18px}
.cbtn{border:1px solid var(--hairline);background:#fff;border-radius:99px;padding:6px 16px;cursor:pointer;font-size:13px;font-weight:500;color:var(--text-2)}
.cbtn.on{background:var(--accent);color:#fff;border-color:transparent}
.cols{display:flex;gap:22px;flex-wrap:wrap}
.col{flex:1;min-width:340px}
.col h2{font-size:13px;font-weight:600;color:var(--text-2);margin:0 0 8px;display:flex;align-items:center;gap:8px}
.tagpill{font-size:11px;font-weight:500;border-radius:99px;padding:2px 9px}
.tagpill.old{background:rgba(20,40,60,.07);color:var(--text-3)}
.tagpill.new{background:var(--chara-accent,#5B9FD4);color:#fff}
/* the chat frame mirrors the real UI */
.frame{height:560px;border-radius:16px;overflow:hidden;border:1px solid var(--hairline);
  background:var(--panel);display:flex;flex-direction:column;box-shadow:0 6px 24px rgba(20,40,60,.08);position:relative}
.head{display:flex;align-items:center;gap:11px;padding:13px 16px;border-bottom:1px solid var(--hairline);
  background:var(--panel);position:relative;z-index:3}
.av{width:38px;height:38px;border-radius:11px;overflow:hidden;flex:none;position:relative;
  display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:15px}
.av img{width:100%;height:100%;object-fit:cover}
.av .dot{position:absolute;right:-1px;bottom:-1px;width:9px;height:9px;border-radius:50%;background:#3FB67F;border:2px solid var(--panel)}
.who b{font-size:14px}.who span{font-size:11.5px;color:var(--text-3);margin-left:6px}
.stream{flex:1;overflow:hidden;position:relative}
.scene{position:absolute;inset:0;background-size:cover;background-position:center;z-index:0}
.veil{position:absolute;inset:0;background:rgba(245,246,248,.86);z-index:1}
.sprite-wm{position:absolute;right:-10px;bottom:0;height:78%;opacity:.16;z-index:1;pointer-events:none}
.inner{position:relative;z-index:2;max-width:560px;margin:0 auto;padding:18px 22px;display:flex;flex-direction:column;gap:16px;height:100%;overflow:auto}
.char-msg{display:flex;gap:11px}
.char-msg .body{flex:1;min-width:0}
.char-msg .name{font-size:12px;color:var(--text-3);margin-bottom:3px;letter-spacing:.3px}
.char-msg .text{line-height:1.7;font-size:14.5px}
.avatar-s{width:30px;height:30px;border-radius:9px;overflow:hidden;flex:none;margin-top:2px;
  display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:13px}
.avatar-s img{width:100%;height:100%;object-fit:cover}
.super .body{border-radius:14px;padding:10px 13px 11px;
  background:linear-gradient(135deg,rgba(255,232,168,.42),var(--chara-soft,rgba(91,159,212,.13)));
  box-shadow:0 0 0 1px rgba(217,147,13,.22),0 8px 26px rgba(217,147,13,.10)}
.super .text{font-weight:500}
.user-msg{align-self:flex-end;max-width:78%}
.user-msg .bubble{background:var(--chara-soft,rgba(91,159,212,.13));border-radius:14px 14px 4px 14px;
  padding:9px 13px;font-size:14px;line-height:1.6}
.sticker-msg{display:flex;gap:11px;align-items:flex-end}
.sticker-msg img{width:104px;height:104px;object-fit:contain;filter:drop-shadow(0 4px 10px rgba(20,40,60,.18))}
.bar{display:flex;align-items:center;gap:9px;padding:11px 14px;border-top:1px solid var(--hairline);background:var(--panel);position:relative;z-index:3}
.bar .field{flex:1;border:1px solid var(--hairline);border-radius:99px;padding:8px 14px;color:var(--text-3);font-size:13px}
.bar .send{width:34px;height:34px;border-radius:50%;background:var(--chara-accent,var(--accent));flex:none}
.note{font-size:12px;color:var(--text-2);margin-top:9px;line-height:1.6}
.note b{color:var(--text)}
</style>
<body>
<h1>OpenCharaAgent 聊天界面 — 现在 vs 接入素材后</h1>
<p class="sub">同一套布局、同一套组件,只是换了更丰富的皮肤:Q版头像 · 淡化的角色背景 · 立绘水印 · 表情包气泡。不改 DOM 结构,纯素材 + CSS。</p>
<div class="switch-row" id="sw"></div>
<div class="cols">
  <div class="col"><h2>现在 <span class="tagpill old">SVG 徽标 · 无背景</span></h2><div class="frame" id="old"></div>
    <p class="note" id="oldnote"></p></div>
  <div class="col"><h2>接入素材后 <span class="tagpill new">同布局 · 富皮肤</span></h2><div class="frame" id="new"></div>
    <p class="note" id="newnote"></p></div>
</div>
<script>
const DATA = __DATA__;
function hex2rgba(h,a){const n=parseInt(h.slice(1),16);return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`}
function emblemAv(cls,d){const e=document.createElement('div');e.className=cls;
  e.style.background=`linear-gradient(160deg,${hex2rgba(d.theme,.95)},${d.theme2})`;e.textContent=d.name[0];return e}
function imgAv(cls,d){const e=document.createElement('div');e.className=cls;
  const im=document.createElement('img');im.src=d.avatar;im.loading='lazy';e.appendChild(im);return e}
function render(frame,d,isNew){
  frame.innerHTML='';
  frame.style.setProperty('--chara-accent',d.theme);
  frame.style.setProperty('--chara-soft',hex2rgba(d.theme,.13));
  // head
  const head=document.createElement('div');head.className='head';
  const hav=(isNew?imgAv:emblemAv)('av',d);const dot=document.createElement('span');dot.className='dot';hav.appendChild(dot);
  const who=document.createElement('div');who.className='who';who.innerHTML=`<b>${d.name}</b><span>${d.tag}</span>`;
  head.append(hav,who);frame.appendChild(head);
  // stream
  const stream=document.createElement('div');stream.className='stream';
  if(isNew){const sc=document.createElement('div');sc.className='scene';sc.style.backgroundImage=`url(${d.bg})`;
    const veil=document.createElement('div');veil.className='veil';
    const wm=document.createElement('img');wm.className='sprite-wm';wm.src=d.sprite;wm.loading='lazy';
    stream.append(sc,veil,wm);}
  const inner=document.createElement('div');inner.className='inner';
  for(const [kind,text] of d.lines){
    if(kind==='user'){const u=document.createElement('div');u.className='user-msg';
      u.innerHTML=`<div class="bubble"></div>`;u.querySelector('.bubble').textContent=text;inner.appendChild(u);continue;}
    const m=document.createElement('div');m.className='char-msg'+(kind==='speak'?' super':'');
    m.appendChild((isNew?imgAv:emblemAv)('avatar-s',d));
    const body=document.createElement('div');body.className='body';
    body.innerHTML=`<div class="name">${d.name}</div><div class="text"></div>`;
    body.querySelector('.text').textContent=text;m.appendChild(body);inner.appendChild(m);
  }
  if(isNew){const s=document.createElement('div');s.className='sticker-msg';
    s.appendChild(imgAv('avatar-s',d));const si=document.createElement('img');si.src=d.sticker;si.loading='lazy';
    s.appendChild(si);inner.appendChild(s);}
  stream.appendChild(inner);frame.appendChild(stream);
  // bar
  const bar=document.createElement('div');bar.className='bar';
  bar.innerHTML=`<div class="field">说点什么…</div><div class="send"></div>`;frame.appendChild(bar);
}
let cur=Object.keys(DATA)[0];
function show(k){cur=k;render(document.getElementById('old'),DATA[k],false);render(document.getElementById('new'),DATA[k],true);
  document.documentElement.style.setProperty('--chara-accent',DATA[k].theme);
  document.querySelectorAll('.cbtn').forEach(b=>b.classList.toggle('on',b.dataset.k===k));
  document.getElementById('oldnote').innerHTML='头像是 64×64 的 <b>SVG 徽标</b>,聊天区纯色背景 —— 即现状。';
  document.getElementById('newnote').innerHTML='换成 <b>Q版头像</b> + 淡化角色<b>背景</b>(白蒙版保证可读) + 角落<b>立绘</b>水印 + <b>表情包</b>气泡。全部是缩放过的静态图,懒加载,不卡。';}
const sw=document.getElementById('sw');
for(const k of Object.keys(DATA)){const b=document.createElement('button');b.className='cbtn';b.dataset.k=k;b.textContent=DATA[k].name;b.onclick=()=>show(k);sw.appendChild(b);}
show(cur);
</script>
</body></html>"""

html = HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False))
dest = Path(__file__).parent / "demo_chat.html"
dest.write_text(html, encoding="utf-8")
print("demo ->", dest, f"({len(html)//1024} KB)")
