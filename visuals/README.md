# visuals/ — character art pipeline (DEV / offline tool)

An experimental, offline pipeline that turns a LunaMoth **character card** into a
small, web-optimized **visual asset library** (avatar, full-body sprite,
background, key visual, sticker set) in a unified anime gacha-game (二游) style.

> **This is a developer/offline tool, NOT part of the LunaMoth runtime.** It has
> heavy dependencies (see below) and costs real API spend when run. Nothing here
> is imported by `src/`. The runtime only ever consumes the finished `web/`
> assets a human copies into a card folder — it never generates art itself.

## What it does

```
card.json
  -> cardbrief.get_brief     LLM reads the card -> cached visual brief
                             {appearance, palette, world, theme}   (cache/<stem>.json)
  -> genviz.generate         5 raw assets, identity-locked, into out/<Name>/
  -> localmatte (BiRefNet)   cut sprite + sticker sheet to transparent PNG (full res)
  -> web variants            downscaled, ship-ready set in out/<Name>/web/
  -> assets.json             manifest under the keys the card uses
```

The card is the soul: the generator never hardcodes who a character is — an LLM
reads the card's identity/personality/lore and writes a concrete, drawable
**visual brief** that the image stage consumes. The key visual is generated first
(against a deterministic blank layout template, `template.py`) to fix the look;
sprite/avatar/stickers are then identity-locked to that key visual; the
background is last.

### Raw assets (`out/<Name>/`, intermediate — not shipped)
| asset | file | notes |
|-------|------|-------|
| key visual 主视觉 | `keyvisual.jpg` | turnaround + action + props + palette sheet, layout-locked |
| sprite 立绘 | `sprite.jpg` → `sprite.png` | full-body, matted to transparent |
| avatar 头像 | `avatar.jpg` → `avatar.png` | chibi bust, rounded-rect |
| stickers 表情包 | `stickers.jpg` → `stickers/sticker_00..08.png` | 3×3 = 9 chibi expressions, matted + cropped |
| background 背景 | `background.jpg` | world scene, center kept open for chat bubbles |

## Model choices

- **Visual brief:** Gemini 3.1 Pro via OpenRouter
  (`google/gemini-3.1-pro-preview`; override with `OPENROUTER_BRIEF_MODEL`).
- **Image generation:** Doubao-Seedream 5.0-lite on Volcano Ark
  (`doubao-seedream-5-0-260128`; override with `ARK_IMAGE_MODEL`).
- **Matting:** BiRefNet via `rembg` (`birefnet-general`, falls back to
  `birefnet-general-lite`) — color-agnostic SOTA matting at full resolution, so
  a green-robed character on a green sticker sheet still cuts cleanly, with
  green-spill suppression.

## Keys (both optional, DEV-only — not a runtime dependency)

| key | used by | source |
|-----|---------|--------|
| `OPENROUTER_API_KEY` | the visual brief LLM | env, else `~/.lunamoth/openrouter_key` |
| `ARK_API_KEY` | Volcano Ark image gen | env, else `~/.lunamoth/ark_api_key` |

The Volcano key is a **separate, explicit dev-only key** for this tool. It is not
required to run LunaMoth; the runtime never calls Ark. (`removebg.py` offers a
cloud matting alternative keyed by `REMOVEBG_API_KEY` if you'd rather not run
BiRefNet locally.)

## Heavy dependencies (why this is dev/offline only)

The matting step pulls in serious weight, which is exactly why it is **not**
bundled into the LunaMoth runtime:

- `rembg` + `onnxruntime`
- the **BiRefNet model is ~970 MB** (downloaded on first use)
- **~5 GB RAM peak** during full-resolution matting
- plus `Pillow` and `numpy`

Install these into a throwaway/dev environment, e.g.:

```bash
pip install "rembg[cpu]" onnxruntime pillow numpy
# (cardbrief/genviz use only the stdlib for HTTP; web variants need Pillow.
#  webp output needs a Pillow built with WebP support; otherwise it falls back
#  to .png and the manifest records the .png names.)
```

## Run it

```bash
python build_character.py <Name|card.json path> [--steps all|images|matte|web] [--out DIR]

python build_character.py Quinn                  # full run -> out/Quinn/web/
python build_character.py cards/K-9.en.json      # by explicit card path
python build_character.py Quinn --steps images   # brief + raw generation only
python build_character.py Quinn --steps matte    # + BiRefNet matting (no web step)
python build_character.py Quinn --steps web      # re-derive web variants from disk
python build_character.py Quinn --out /tmp/qweb  # custom output dir
python build_character.py Quinn --force-brief    # ignore the cached brief, re-query
```

A name (`Quinn`) resolves to `cards/<Name>.en.json`; a path is used as-is. Briefs
are cached in `visuals/cache/<stem>.json` — delete the file or pass
`--force-brief` to rebuild. `--steps web` is cheap (no API calls) and just
re-derives the downscaled set from whatever raw/matted assets are already on disk.

## Output: the `web/` folder (the only thing meant to ship)

Written to `--out` (default `visuals/out/<Name>/web/`). We never write into
`cards/` — the backend owns that layout; this just produces a `web/` folder a
human or a script copies in.

```
web/
  avatar.png          256×256
  sprite.png          transparent, longest side ~1200
  background.webp     width ~1440, q80   (→ .png if Pillow lacks WebP)
  keyvisual.webp      width ~1600, q80   (→ .png if Pillow lacks WebP)
  stickers/
    00.png .. 08.png  ~256px square, transparent
  assets.json
```

`assets.json` lists the produced files under the keys the card will use:

```json
{
  "avatar": "avatar.png",
  "sprite": "sprite.png",
  "background": "background.webp",
  "keyvisual": "keyvisual.webp",
  "stickers": ["stickers/00.png", "stickers/01.png", "..."]
}
```

Any missing source is skipped, so a partial run still yields a valid (partial)
manifest.
