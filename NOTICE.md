# Notice / Attribution

OpenCharaAgent is licensed under Apache License 2.0; see `LICENSE`.

## Code adapted from other projects

- `src/chara/transcript.py` adapts the SQLite storage design (WAL journal
  mode with a DELETE fallback for WAL-incompatible filesystems) from
  [hermes-agent](https://github.com/NousResearch/hermes-agent)'s
  `hermes_state.py`, © Nous Research, MIT License.
