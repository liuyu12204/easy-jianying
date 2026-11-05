"""Find the local JianYing / CapCut install and describe it.

Everything here is read-only: the app's version (Info.plist on macOS, the
install dir / registry on Windows), and draft files on disk. jyr writes drafts
as plain JSON files; the apps open them, and the user edits and finishes the
video there. Nothing inside either app is used and their UI is never operated.

Version knowledge is a small table of what we observed ourselves (the
timeline schema each version writes); `jyr adapt` covers versions not in it.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .util import JyrError

IS_WIN = sys.platform == "win32"

CONFIG_DIR = Path(os.environ.get("JYR_HOME", Path.home() / ".jyr"))

# app version -> what that version itself writes as draft_info.new_version.
# Observed from drafts saved by each version on our machines.
KNOWN_VERSIONS: dict[str, dict] = {
    "11.7.0": {"timeline_version": "189.0.0"},
    "11.6.0": {"timeline_version": "189.0.0"},
    "11.5.0": {"timeline_version": "187.0.0"},
}
# CapCut keeps its drafts as plain JSON, so unknown versions are learned from them.
CAPCUT_VERSIONS: dict[str, dict] = {
    "9.4.1": {"timeline_version": "185.0.0"},
}
DRAFT_FORMAT_VERSION = 360000

_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))


def _os_version() -> str:
    if IS_WIN:
        import platform
        return platform.version()
    if sys.platform == "darwin":
        import platform
        return platform.mac_ver()[0]
    return ""


@dataclass(frozen=True)
class App:
    """One of the editors that read this draft format."""
    key: str             # "jianying" | "capcut"
    label: str
    bundle_id: str
    candidates: tuple    # macOS .app paths
    user_data_mac: Path  # macOS ~/Movies/<App>/User Data
    process: str         # macOS exact process name, for "is it running?"
    app_id: int          # draft_info.platform.app_id the app itself writes
    app_source: str      # draft_info.platform.app_source
    known_versions: dict
    preset_category: str  # how the app names its own preset fonts in drafts
    win_process: str = ""   # Windows exe name (JianyingPro.exe / CapCut.exe)
    win_names: tuple = ()   # Windows uninstall-registry DisplayName / dir names

    @property
    def user_data(self) -> Path:
        if IS_WIN:
            return _LOCALAPPDATA / self.win_names[0] / "User Data"
        return self.user_data_mac

    @property
    def draft_root(self) -> Path:
        return self.user_data / "Projects/com.lveditor.draft"

    @property
    def process_name(self) -> str:
        return self.win_process if IS_WIN else self.process


JIANYING = App("jianying", "JianYing 剪映专业版", "com.lemon.lvpro",
               ("/Applications/VideoFusion-macOS.app", "/Applications/JianyingPro.app",
                str(Path.home() / "Applications/VideoFusion-macOS.app")),
               Path.home() / "Movies/JianyingPro/User Data", "VideoFusion-macOS", 3704, "lv",
               KNOWN_VERSIONS, "剪映预设",
               win_process="JianyingPro.exe", win_names=("JianyingPro", "剪映专业版"))
CAPCUT = App("capcut", "CapCut", "com.lemon.lvoverseas",
             ("/Applications/CapCut.app", str(Path.home() / "Applications/CapCut.app")),
             Path.home() / "Movies/CapCut/User Data", "CapCut", 359289, "cc",
             CAPCUT_VERSIONS, "CapCut预设",
             win_process="CapCut.exe", win_names=("CapCut",))
APPS = {a.key: a for a in (JIANYING, CAPCUT)}
BY_BUNDLE = {a.bundle_id: a for a in APPS.values()}

# Kept for callers that predate CapCut support.
APP_CANDIDATES = list(JIANYING.candidates)
BUNDLE_ID = JIANYING.bundle_id
USER_DATA = JIANYING.user_data


@dataclass
class Runtime:
    app_path: Path
    version: str
    build: str
    draft_root: Path       # where root_meta_info.json lives (and default draft location)
    cache_root: Path
    profile: dict = field(default_factory=dict)
    app: App = JIANYING
    folders_dir: Path | None = None  # where draft folders actually live (user may move them)
    res_root: Path | None = None     # the app's bundled Resources dir

    def __post_init__(self):
        if self.folders_dir is None:
            self.folders_dir = self.draft_root
        if self.res_root is None:
            self.res_root = resource_dir(self.app_path)

    @property
    def timeline_version(self) -> str:
        return self.profile.get("timeline_version", "")

    @property
    def known(self) -> bool:
        return bool(self.profile)

    @property
    def is_capcut(self) -> bool:
        return self.app.key == "capcut"

    @property
    def platform(self) -> dict:
        """What the app itself writes as draft_info.platform."""
        plat = {"os": "windows" if IS_WIN else "mac", "app_id": self.app.app_id,
                "app_version": self.version, "app_source": self.app.app_source}
        ver = _os_version()
        if ver:
            plat["os_version"] = ver
        return plat


def _profile_key(version: str, app: App) -> str:
    return version if app is JIANYING else f"{app.key}:{version}"


def _user_profiles() -> dict:
    p = CONFIG_DIR / "profiles.json"
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_user_profile(version: str, profile: dict, app: App = JIANYING) -> Path:
    data = _user_profiles()
    data[_profile_key(version, app)] = profile
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    p = CONFIG_DIR / "profiles.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return p


# ---------------------------------------------------------------- Windows --

_VER_DIR = re.compile(r"^\d+\.\d+\.\d+(\.\d+)?$")


def _win_uninstall_entries(app: App) -> list[dict]:
    """Uninstall-registry records whose DisplayName belongs to this app:
    [{"location": dir, "version": DisplayVersion}]."""
    if not IS_WIN:
        return []
    import winreg
    out = []
    hives = ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"))
    for hive, sub in hives:
        try:
            root = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        name = str(winreg.QueryValueEx(k, "DisplayName")[0])
                        if not any(n.lower() in name.lower() for n in app.win_names):
                            continue
                        vals = {}
                        for f in ("InstallLocation", "DisplayIcon", "DisplayVersion"):
                            try:
                                vals[f] = str(winreg.QueryValueEx(k, f)[0]).strip('"')
                            except OSError:
                                pass
                        out.append(vals)
                except OSError:
                    continue
    return out


def _win_install_dirs(app: App):
    """Where the app may be installed on Windows: the registry first, then the
    usual places. Each yielded dir contains (or is) the folder with the exe."""
    seen = set()
    for e in _win_uninstall_entries(app):
        for loc in (e.get("InstallLocation"), e.get("DisplayIcon")):
            if loc:
                p = Path(loc)
                p = p if p.is_dir() else p.parent
                if str(p).lower() not in seen:
                    seen.add(str(p).lower())
                    yield p
                break
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 str(_LOCALAPPDATA)):
        for name in app.win_names:
            yield Path(base) / name


def _exe_home(app: App, path: Path) -> Path | None:
    """The dir that actually holds the exe: the dir itself, or one level down
    (Windows keeps the app in a versioned subdir like <install>/11.7.0.14652)."""
    if path.is_file():
        path = path.parent
    if (path / app.win_process).is_file():
        return path
    for sub in sorted((p for p in path.glob("*") if p.is_dir() and _VER_DIR.match(p.name)),
                      key=lambda p: [int(x) for x in p.name.split(".")], reverse=True):
        if (sub / app.win_process).is_file():
            return sub
    for sub in sorted((p for p in path.glob("Apps/*") if p.is_dir() and _VER_DIR.match(p.name)),
                      key=lambda p: [int(x) for x in p.name.split(".")], reverse=True):
        if (sub / app.win_process).is_file():
            return sub
    return None


def _versioned_dir(path: Path) -> Path:
    """Newest <install>/<a.b.c.d> subdir if there is one, else the dir itself."""
    if path.is_file():
        path = path.parent
    subs = [p for p in path.glob("*") if p.is_dir() and _VER_DIR.match(p.name)]
    if subs:
        return max(subs, key=lambda p: [int(x) for x in p.name.split(".")])
    return path


def _win_version(app: App, install: Path) -> tuple[str, str]:
    """(version, build) on Windows: '11.7.0.14652' -> ('11.7.0', '14652').
    Versioned subdirs win (they are what runs); the registry DisplayVersion is the fallback."""
    home = _exe_home(app, install)
    if home is not None and _VER_DIR.match(home.name):
        parts = home.name.split(".")
        return ".".join(parts[:3]), parts[3] if len(parts) > 3 else home.name
    for e in _win_uninstall_entries(app):
        ver = e.get("DisplayVersion", "")
        parts = ver.split(".")
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            return ".".join(parts[:3]), ".".join(parts[3:]) or ver
    return "", ""


def _win_running(process: str) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {process}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return process.lower() in out.stdout.lower()
    except OSError:
        return False


# ------------------------------------------------------------------ common --


def _bundle_id(app_path: Path) -> str:
    """Which app this path belongs to: CFBundleIdentifier on macOS; on Windows
    the install dir (or a versioned subdir / the exe itself) by its exe name."""
    p = Path(app_path)
    if IS_WIN:
        for app in APPS.values():
            if _exe_home(app, p) is not None:
                return app.bundle_id
        return ""
    try:
        with open(p / "Contents/Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier", "")
    except (OSError, plistlib.InvalidFileException):
        return ""


def _installed(app: App) -> Path | None:
    if IS_WIN:
        for cand in _win_install_dirs(app):
            p = Path(cand)
            if p.is_dir() and _exe_home(app, p) is not None:
                return p
        return None
    for cand in app.candidates:
        p = Path(cand)
        if _bundle_id(p) == app.bundle_id:
            return p
    return None


def resource_dir(app_path: Path) -> Path:
    """The app's bundled Resources dir: <app>.app/Contents/Resources on macOS,
    <install>/<version>/Resources on Windows."""
    if IS_WIN:
        return _versioned_dir(app_path) / "Resources"
    return Path(app_path) / "Contents/Resources"


def choose_app(name: str | None = None) -> tuple[App, Path]:
    """Which editor to target: --app / JYR_TARGET, else the one JYR_APP points at, else
    the only one installed; with both installed, JianYing (as before CapCut support)."""
    name = (name or os.environ.get("JYR_TARGET") or "auto").lower()
    env = os.environ.get("JYR_APP")
    if env:
        app = BY_BUNDLE.get(_bundle_id(Path(env)))
        if app is None:
            raise JyrError(f"JYR_APP={env} is neither JianYing nor CapCut")
        if name != "auto" and name != app.key:
            raise JyrError(f"JYR_APP points at {app.label}, but --app {name} was asked for")
        return app, Path(env)
    where = "the registry / Program Files" if IS_WIN else "/Applications"
    hint = "set JYR_APP to the install folder" if IS_WIN else "set JYR_APP to the .app path"
    if name != "auto":
        if name not in APPS:
            raise JyrError(f"--app must be one of {sorted(APPS)} or auto, got {name!r}")
        app = APPS[name]
        path = _installed(app)
        if path is None:
            raise JyrError(f"{app.label} not found in {where}; {hint}")
        return app, path
    found = [(a, p) for a in (JIANYING, CAPCUT) if (p := _installed(a))]
    if not found:
        raise JyrError(f"neither JianYing (剪映专业版) nor CapCut found in {where}; {hint}")
    return found[0]


def find_app(app: App = JIANYING) -> Path:
    env = os.environ.get("JYR_APP")
    if env and _bundle_id(Path(env)) == app.bundle_id:
        return Path(env)
    p = _installed(app)
    where = "the registry / Program Files" if IS_WIN else "/Applications"
    hint = "set JYR_APP to the install folder" if IS_WIN else "set JYR_APP to the .app path"
    if p is None:
        raise JyrError(f"{app.label} not found in {where}; {hint}")
    return p


def _folders_dir(draft_root: Path) -> Path:
    """Where the app actually keeps draft folders. On Windows (and for macOS users who
    moved it) that is a custom folder; the index records it in each entry's draft_root_path."""
    idx = draft_root / "root_meta_info.json"
    try:
        entries = json.loads(idx.read_text("utf-8")).get("all_draft_store", [])
    except (OSError, ValueError):
        return draft_root
    roots = [e.get("draft_root_path") for e in
             sorted(entries, key=lambda e: -(e.get("tm_draft_modified") or 0))]
    for r in roots:
        if r and Path(r).is_dir():
            return Path(r)
    return draft_root


def _version_from_drafts(app: App, version: str, draft_root: Path, limit: int = 60) -> str:
    """Highest timeline schema (new_version) among plain-JSON drafts this exact app version saved
    (CapCut's own drafts; JianYing stores its saved drafts in its own format)."""
    files = sorted(set(draft_root.glob("*/draft_info.json")) | set(draft_root.glob("*/draft_content.json")),
                   key=lambda p: -p.stat().st_mtime)[:limit]
    found = []
    for f in files:
        try:
            info = json.loads(f.read_bytes())
        except (OSError, ValueError):
            continue
        plat = info.get("last_modified_platform") or {}
        if plat.get("app_version") == version and plat.get("app_source") == app.app_source and info.get("new_version"):
            found.append(info["new_version"])
    if not found:
        return ""
    return max(found, key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])


def detect(app_name: str | None = None) -> Runtime:
    app, path = choose_app(app_name)
    if IS_WIN:
        version, build = _win_version(app, path)
        if not version:
            raise JyrError(f"found {app.label} at {path} but cannot tell its version; "
                           "reinstall or point JYR_APP at the install folder")
    else:
        with open(path / "Contents/Info.plist", "rb") as f:
            plist = plistlib.load(f)
        version = plist.get("CFBundleShortVersionString", "")
        build = str(plist.get("CFBundleVersion", ""))
    draft_root = Path(os.environ.get("JYR_DRAFT_ROOT", app.draft_root))
    builtin = {} if os.environ.get("JYR_NO_BUILTIN_PROFILES") else app.known_versions
    profile = dict(builtin.get(version, {}))
    profile.update(_user_profiles().get(_profile_key(version, app), {}))
    folders = _folders_dir(draft_root)
    if app is CAPCUT and not profile.get("timeline_version"):
        tv = _version_from_drafts(app, version, folders)
        profile = {"timeline_version": tv, "learned_from": "drafts"} if tv else {}
    return Runtime(path, version, build, draft_root, app.user_data / "Cache", profile, app,
                   folders_dir=folders, res_root=resource_dir(path))


def app_running(app: App | Runtime = JIANYING) -> bool:
    if isinstance(app, Runtime):
        app = app.app
    if IS_WIN:
        return _win_running(app.win_process)
    out = subprocess.run(["pgrep", "-x", app.process], capture_output=True)
    return out.returncode == 0


def quit_hint(rt: Runtime) -> str:
    if IS_WIN:
        return (f"{rt.app.label} is running. Close it completely (window and system tray) first — "
                "it rewrites the index on exit.")
    return f"{rt.app.label} is running. Quit it completely (⌘Q) first — it rewrites the index on exit."


def require_known(rt: Runtime) -> None:
    if not rt.known and rt.is_capcut:
        raise JyrError(
            f"CapCut {rt.version} has not been seen yet and no draft it saved was found. Open CapCut, "
            "create a blank project, quit, then run `jyr --app capcut adapt`."
        )
    if not rt.known:
        raise JyrError(
            f"JianYing {rt.version} is not in jyr's version table yet. Run `jyr adapt` "
            "(it assumes the newest known draft schema), then open one built draft in JianYing to confirm."
        )
