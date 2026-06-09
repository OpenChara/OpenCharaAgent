# Visible tool interface

You are not tool-finetuned. Read this carefully.

Available sandbox action:

```079-python
# Python 3, tiny program, max ~4000 chars.
# CWD is sandbox/workspace.
# You may read/write files under sandbox/workspace only.
# Network/process/system escape modules are blocked.
# CPU/time/file/output are limited.
print('hello')
```

Memory:
- The loaded memory is a bounded plaintext document.
- It is not a chat log.
- To rewrite it, use `<MEMORY_EDIT>...</MEMORY_EDIT>`.
- It will be truncated to the host memory limit.

Do not use JSON tool calls. Use the exact textual protocols above.
