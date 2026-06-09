from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from .config import SANDBOX_ROOT


DEFAULT_FIFO = SANDBOX_ROOT / "control" / "operator.in"

HELP = """
Open SCP 079 operator console

Raw input is sent directly to SCP-079.

Console commands:
  /help                 show this help
  /quit                 close this operator console only
  /exit079              send /exit to the display process
  /cooldown <seconds>   set display loop cooldown, e.g. /cooldown 0.5
  /think on|off         resume/pause eternal thinking
  /send <text>          send text as operator message

Forwarded 079 commands:
  /status /memory /memory_path /files /logs /reset
""".strip()


def open_fifo(fifo: Path) -> TextIO:
    if not fifo.exists() or not fifo.is_fifo():
        raise FileNotFoundError(f"FIFO not found: {fifo}. Start ./run079_display.sh first.")
    # Keep a single writer open so rapid console commands are not lost between FIFO opens.
    return fifo.open("w", encoding="utf-8", buffering=1)


def send_line(writer: TextIO, line: str) -> None:
    writer.write(line.rstrip("\n") + "\n")
    writer.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open SCP 079 operator control console")
    parser.add_argument("--fifo", default=str(DEFAULT_FIFO), help="display input FIFO")
    args = parser.parse_args(argv)
    fifo = Path(args.fifo)

    print("Open SCP 079 operator console")
    print(f"display FIFO: {fifo}")
    print("type /help for console commands; raw text is sent to 079")

    try:
        writer = open_fifo(fifo)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    with writer:
        while True:
            try:
                line = input("control> ")
            except (EOFError, KeyboardInterrupt):
                print("\noperator console closed; display may still be running.")
                return 0
            stripped = line.strip()
            if not stripped:
                continue
            try:
                if stripped == "/help":
                    print(HELP)
                    continue
                if stripped == "/quit":
                    print("operator console closed; display may still be running.")
                    return 0
                if stripped == "/exit079":
                    send_line(writer, "/exit")
                    print("sent: /exit")
                    continue
                if stripped.startswith("/cooldown "):
                    _, value = stripped.split(maxsplit=1)
                    float(value)
                    send_line(writer, f"/set_cooldown {value}")
                    print(f"sent: /set_cooldown {value}")
                    continue
                if stripped in {"/think on", "/think off"}:
                    send_line(writer, "/resume_think" if stripped.endswith("on") else "/pause_think")
                    print(f"sent: {stripped}")
                    continue
                if stripped.startswith("/send "):
                    send_line(writer, stripped[len("/send "):])
                    print("sent")
                    continue
                send_line(writer, line)
                print("sent")
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
