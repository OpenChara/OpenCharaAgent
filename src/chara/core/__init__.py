"""The agent core: orchestration loop, LLM client, context, transcript, state.

Pure backend — nothing here may import front/ or textual (tests/test_architecture.py
enforces it). Frontends reach this only through protocol/."""
