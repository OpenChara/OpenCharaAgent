from __future__ import annotations

import shutil
from pathlib import Path

from ..obs.log import get_logger

_log = get_logger("sandbox")


class SandboxViolation(ValueError):
    pass


class Sandbox:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.logs_dir = (self.root / "logs").resolve()
        self.workspace_dir = (self.root / "workspace").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_files()

    def _migrate_legacy_files(self) -> None:
        """One workspace now: fold any legacy files/ tree into workspace/.

        A separate files/ tree once split the file space (write_file landed in
        files/ while the terminal worked in workspace/). It is gone. To keep an
        existing chara from losing work, the first time we touch a sandbox that
        still has a non-empty files/, MOVE its contents into workspace/ (merge),
        never clobbering an existing workspace file (name clash → suffix the
        incoming one), then drop the empty files/ dir.
        """
        legacy = (self.root / "files").resolve()
        if not legacy.is_dir():
            return
        moved = 0
        for src in sorted(p for p in legacy.rglob("*") if p.is_file()):
            rel = src.relative_to(legacy)
            dest = (self.workspace_dir / rel).resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                # Keep both: suffix the incoming file rather than overwrite.
                stem, suffix = dest.stem, dest.suffix
                n = 1
                while dest.exists():
                    dest = dest.with_name(f"{stem}.migrated-{n}{suffix}")
                    n += 1
            shutil.move(str(src), str(dest))
            moved += 1
        shutil.rmtree(legacy, ignore_errors=True)
        if moved:
            _log.info("migrated %d file(s) from legacy files/ into workspace/", moved)

    def resolve_inside(self, relative: str | Path, base: Path | None = None) -> Path:
        rel = Path(relative)
        if rel.is_absolute():
            raise SandboxViolation("absolute paths are not allowed")
        target_base = (base or self.workspace_dir).resolve()
        target = (target_base / rel).resolve()
        if target != target_base and target_base not in target.parents:
            raise SandboxViolation("path escapes sandbox")
        return target

    # The chara has ONE working directory: workspace/. The terminal runs there,
    # and write_file/read_file/list_files all operate there too — so a file the
    # chara writes with write_file is the same file its `ls`/`cat` in the
    # terminal sees.
    def list_files(self) -> list[str]:
        names: list[str] = []
        for p in sorted(self.workspace_dir.rglob("*")):
            if p.is_file():
                names.append(str(p.relative_to(self.workspace_dir)))
        return names

    def read_file(self, filename: str, max_chars: int = 6000) -> str:
        path = self.resolve_inside(filename, base=self.workspace_dir)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(filename)
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:max_chars]

    def write_file(self, filename: str, text: str) -> None:
        path = self.resolve_inside(filename, base=self.workspace_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_bytes(self, relative: str, data: bytes) -> str:
        """Write raw bytes under workspace/, never overwriting: a name collision
        gets a `name (2).ext` suffix. Returns the workspace-relative path that was
        actually written (e.g. ``uploads/photo.png``). Used for inbound attachments
        (chat uploads, messaging media) that must land beside the chara's own work.
        """
        path = self.resolve_inside(relative, base=self.workspace_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stem, suffix = path.stem, path.suffix
            n = 2
            while True:
                candidate = path.with_name(f"{stem} ({n}){suffix}")
                if not candidate.exists():
                    path = candidate
                    break
                n += 1
        path.write_bytes(data)
        return str(path.relative_to(self.workspace_dir))
