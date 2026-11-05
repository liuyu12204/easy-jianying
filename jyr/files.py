"""Draft files are plain JSON: jyr writes them as-is and reads only plain JSON.

JianYing and CapCut both open such drafts. After JianYing saves a draft it keeps
it in its own format, which jyr does not read; CapCut keeps plain JSON.

The timeline file is called draft_info.json on macOS and draft_content.json on
Windows; jyr accepts (and on Windows writes) both names.
"""

from __future__ import annotations

import json
from pathlib import Path

from .runtime import IS_WIN, Runtime
from .util import JyrError

# What the app calls the main timeline file in a saved draft.
INFO_NAMES = ("draft_info.json", "draft_content.json") if IS_WIN else ("draft_info.json",)
READ_NAMES = ("draft_info.json", "draft_content.json")


def info_file(folder: Path) -> Path | None:
    """The draft's timeline file, whichever name it uses; None if there is none."""
    folder = Path(folder)
    for name in READ_NAMES:
        p = folder / name
        if p.is_file():
            return p
    return None


def is_plain_json(data: bytes) -> bool:
    return data.lstrip()[:1] in (b"{", b"[")


def not_readable(rt: Runtime, path: Path) -> JyrError:
    if rt.is_capcut:
        return JyrError(f"{path} is not plain JSON — damaged, or not a CapCut draft file")
    return JyrError(f"{Path(path).parent.name}: JianYing has saved this draft in its own format, which jyr does "
                    "not read. jyr reads CapCut drafts and drafts it built itself.")


def read_json(rt: Runtime, path: Path) -> dict:
    raw = Path(path).read_bytes()
    if not is_plain_json(raw):
        raise not_readable(rt, path)
    return json.loads(raw)


def read_draft(rt: Runtime, folder: Path) -> dict:
    """The timeline JSON of a draft folder, under either file name."""
    p = info_file(folder)
    if p is None:
        raise JyrError(f"{folder}: no draft_info.json / draft_content.json found")
    return read_json(rt, p)


def read_many(files: list[Path]) -> dict[Path, dict | None]:
    """Parse many draft files; None for ones that are not plain JSON."""
    out: dict[Path, dict | None] = {}
    for f in files:
        raw = Path(f).read_bytes()
        try:
            out[f] = json.loads(raw) if is_plain_json(raw) else None
        except (ValueError, UnicodeDecodeError):
            out[f] = None
    return out


def draft_files(root: Path) -> dict[Path, Path]:
    """Map each draft folder under root to its timeline file (either name)."""
    out = {}
    if not Path(root).is_dir():
        return out
    for d in sorted(Path(root).iterdir()):
        if d.is_dir():
            p = info_file(d)
            if p is not None:
                out[d] = p
    return out
