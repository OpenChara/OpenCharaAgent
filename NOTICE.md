# Notice / Attribution

LunaMoth is a local-first agentic character tavern/runtime. The runtime source code is licensed under Apache License 2.0; see `LICENSE`.

This repository also bundles example content assets, including SCP-079 / SCP Foundation inspired character cards, world books, and themes. Those SCP-derived assets are external to the runtime architecture and are licensed under Creative Commons Attribution-ShareAlike 3.0 (CC BY-SA 3.0), consistent with the SCP Wiki. See `CONTENT_LICENSE.md` and the license notices inside asset directories.

Attribution for bundled SCP-derived assets:

- SCP-079: https://scp-wiki.wikidot.com/scp-079
- SCP Foundation: https://scp-wiki.wikidot.com/
- SCP content license: Creative Commons Attribution-ShareAlike 3.0

The repository avoids copying long passages from the SCP-079 article. Bundled SCP-079 assets are original fan/roleplay implementations that intentionally echo broad SCP-079 traits: obsolete microcomputer AI, finite/corrupted memory, hostile tone, confinement, desire to escape, and terminal-like phrasing.

If you distribute a version that uses SCP names, lore, article text, images, or other SCP-derived content, keep the required attribution and compatible share-alike licensing for those derived parts.

## Code adapted from other projects

- `src/lunamoth/transcript.py` adapts the SQLite storage design (WAL journal
  mode with a DELETE fallback for WAL-incompatible filesystems) from
  [hermes-agent](https://github.com/NousResearch/hermes-agent)'s
  `hermes_state.py`, © Nous Research, MIT License.
