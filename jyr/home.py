"""Put a built draft on JianYing's / CapCut's home screen (项目首页).

This is the only step that touches the app's own files. It refuses to run
while the app is open, backs up the index first, and only ever appends.
CapCut keeps the same index (root_meta_info.json) with the same fields.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import files
from .runtime import CONFIG_DIR, IS_WIN, Runtime, app_running, quit_hint
from .util import JyrError, atomic_write, dump, new_id, now_us
from .writer import read_meta

INDEX = "root_meta_info.json"
# The index entry's draft_json_file points at whatever the app calls the timeline file.
JSON_FILE = "draft_content.json" if IS_WIN else "draft_info.json"


def _norm(p) -> str:
    import os
    return os.path.normcase(os.path.normpath(str(p)))


def backup_index(rt: Runtime) -> Path:
    src = rt.draft_root / INDEX
    dst_dir = CONFIG_DIR / "backups"
    dst_dir.mkdir(parents=True, exist_ok=True)
    prefix = "" if rt.app.key == "jianying" else f"{rt.app.key}-"
    dst = dst_dir / f"{prefix}{INDEX}.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(src, dst)
    return dst


def unique_folder(rt: Runtime, name: str) -> Path:
    safe = name.replace("/", "-").strip() or "jyr-draft"
    cand = rt.folders_dir / safe
    n = 2
    while cand.exists():
        cand = rt.folders_dir / f"{safe}({n})"
        n += 1
    return cand


def publish(rt: Runtime, built: Path, name: str | None = None) -> dict:
    """Copy a built draft folder into the app's project root and register it."""
    built = Path(built)
    if files.info_file(built) is None:
        raise JyrError(f"{built} is not a built draft folder")
    if app_running(rt):
        raise JyrError(quit_hint(rt))
    index_path = rt.draft_root / INDEX
    if not index_path.is_file():
        raise JyrError(f"{index_path} not found; open {rt.app.label} once so it creates its project folder")
    meta_jyr = json.loads((built / ".jyr.json").read_text()) if (built / ".jyr.json").exists() else {}
    if meta_jyr.get("app", "jianying") != rt.app.key and "edited_from" not in meta_jyr:
        raise JyrError(f"{built.name} was built for {meta_jyr.get('app', 'jianying')}, not {rt.app.key}; "
                       f"rebuild it with `jyr --app {rt.app.key} build`")
    target = unique_folder(rt, name or meta_jyr.get("name") or built.name)
    backup = backup_index(rt)
    shutil.copytree(built, target)
    try:
        _fix_paths(rt, target)
        meta = read_meta(rt, target)
        index = json.loads(index_path.read_text("utf-8"))
        entry = {
            "cloud_draft_cover": False, "cloud_draft_sync": False, "draft_cloud_last_action_download": False,
            "draft_cloud_purchase_info": "", "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
            "draft_cloud_videocut_purchase_info": "", "draft_cover": str(target / "draft_cover.jpg"),
            "draft_fold_path": str(target), "draft_id": meta["draft_id"], "draft_is_ai_shorts": False,
            "draft_is_cloud_temp_draft": False, "draft_is_infinite_canvas_draft": False,
            "draft_is_invisible": False, "draft_is_pippit_draft": False, "draft_is_web_article_video": False,
            "draft_json_file": str(target / JSON_FILE), "draft_name": target.name,
            "draft_new_version": "", "draft_root_path": str(rt.folders_dir),
            "draft_timeline_materials_size": meta.get("draft_timeline_materials_size_", 0),
            "draft_type": "", "draft_web_article_video_enter_from": "", "pippit_avatar_url": "",
            "pippit_extra_info": "", "pippit_id": "", "pippit_user_name": "",
            "streaming_edit_draft_ready": True, "tm_draft_cloud_completed": "",
            "tm_draft_cloud_entry_id": -1, "tm_draft_cloud_modified": 0, "tm_draft_cloud_parent_entry_id": -1,
            "tm_draft_cloud_space_id": -1, "tm_draft_cloud_user_id": -1,
            "tm_draft_create": meta["tm_draft_create"], "tm_draft_modified": now_us(),
            "tm_draft_removed": 0, "tm_duration": meta["tm_duration"],
        }
        if any(e.get("draft_id") == entry["draft_id"] for e in index.get("all_draft_store", [])):
            raise JyrError("this draft id is already on the home screen")
        before = len(index.get("all_draft_store", []))
        index.setdefault("all_draft_store", []).insert(0, entry)
        if isinstance(index.get("draft_ids"), int):
            index["draft_ids"] += 1
        atomic_write(index_path, dump(index))
        after = json.loads(index_path.read_text("utf-8"))
        if len(after["all_draft_store"]) != before + 1:
            raise JyrError("index check failed after write")
    except BaseException:
        shutil.copy2(backup, index_path)
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"folder": str(target), "draft_id": meta["draft_id"], "index_backup": str(backup)}


def _fix_paths(rt: Runtime, folder: Path) -> None:
    """The meta file records its own folder; rewrite it for the final location."""
    meta = read_meta(rt, folder)
    meta["draft_id"] = new_id()  # each published copy is its own draft
    old = meta.get("draft_fold_path", "")
    meta["draft_fold_path"] = str(folder)
    meta["draft_root_path"] = str(folder.parent)
    meta["draft_name"] = folder.name
    raw = dump(meta)
    if old and old != str(folder):
        raw = raw.replace(json.dumps(old, ensure_ascii=False)[1:-1], json.dumps(str(folder), ensure_ascii=False)[1:-1])
    atomic_write(folder / "draft_meta_info.json", raw)
    # Packed media lives inside the draft; point materials at the new place.
    info_path = files.info_file(folder)
    if old and old != str(folder) and info_path is not None:
        info_raw = info_path.read_text("utf-8")
        o = json.dumps(old, ensure_ascii=False)[1:-1]
        if o in info_raw:
            enc = info_raw.replace(o, json.dumps(str(folder), ensure_ascii=False)[1:-1])
            tl = json.loads(info_raw)["id"]
            tl_dir = folder / "Timelines" / tl
            paths = []
            for name in files.READ_NAMES:
                paths += [folder / name, folder / f"{name}.bak", tl_dir / name, tl_dir / f"{name}.bak"]
            for p in paths:
                if p.exists():
                    atomic_write(p, enc)


def trash(rt: Runtime, names: list[str]) -> dict:
    """Take drafts off the home screen and move their folders to ~/.jyr/trash/<time>/.
    Nothing is deleted; `restore` puts them back."""
    if app_running(rt):
        raise JyrError(quit_hint(rt))
    index_path = rt.draft_root / INDEX
    index = json.loads(index_path.read_text("utf-8"))
    folders = {_norm(rt.folders_dir / n) for n in names}
    missing = [n for n in names if not (rt.folders_dir / n).is_dir()]
    if missing:
        raise JyrError(f"not found: {missing}")
    backup = backup_index(rt)
    bin_dir = CONFIG_DIR / "trash" / time.strftime("%Y%m%d-%H%M%S")
    bin_dir.mkdir(parents=True)
    kept, removed = [], []
    for e in index.get("all_draft_store", []):
        (removed if _norm(e.get("draft_fold_path", "")) in folders else kept).append(e)
    index["all_draft_store"] = kept
    (bin_dir / "index_entries.json").write_text(json.dumps(removed, ensure_ascii=False, indent=1))
    atomic_write(index_path, dump(index))
    for n in names:
        shutil.move(str(rt.folders_dir / n), str(bin_dir / n))
    return {"moved": len(names), "index_entries_removed": len(removed), "trash": str(bin_dir),
            "index_backup": str(backup)}


def restore(rt: Runtime, bin_dir: Path) -> dict:
    """Undo one `trash`: move folders back and re-add their home-screen entries."""
    if app_running(rt):
        raise JyrError(quit_hint(rt))
    bin_dir = Path(bin_dir)
    entries = json.loads((bin_dir / "index_entries.json").read_text("utf-8"))
    index_path = rt.draft_root / INDEX
    backup_index(rt)
    index = json.loads(index_path.read_text("utf-8"))
    for d in bin_dir.iterdir():
        if d.is_dir():
            if (rt.folders_dir / d.name).exists():
                raise JyrError(f"{d.name} already exists in the project folder")
            shutil.move(str(d), str(rt.folders_dir / d.name))
    index["all_draft_store"] = entries + index.get("all_draft_store", [])
    atomic_write(index_path, dump(index))
    return {"restored": len(entries)}
