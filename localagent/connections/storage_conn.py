"""Online storage: Google Drive, OneDrive, Dropbox, Box, pCloud, MEGA, Proton
Drive, iCloud Drive, Icedrive and anything else that shows up as a drive.

Two ways in:
  * cloud account — through rclone (rclone.org), the standard open-source
    tool for 70+ storage services. You sign in once in your browser; LocalAgent
    keeps its own rclone settings file under ~/.localagent.
  * drive or sync folder on this PC — Google Drive for desktop, OneDrive,
    Dropbox, iCloud Drive, Icedrive, pCloud Drive… are just folders/drives, so
    LocalAgent works with them directly.

Rules, same as everywhere in LocalAgent: nothing is ever deleted and nothing
is overwritten. "Removing" moves things into an agent_trash folder on that
same storage. Freeing space on this PC ("offload") copies a file to the cloud,
checks the copy matches, and only then moves the local file into LocalAgent's
trash — you empty the trash yourself when you're happy.
"""
from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

PROVIDERS = {
    # rclone type, display name, how you connect
    "drive": {"name": "Google Drive", "auth": "browser", "params": {"scope": "drive"}},
    "onedrive": {"name": "OneDrive", "auth": "browser", "params": {}},
    "dropbox": {"name": "Dropbox", "auth": "browser", "params": {}},
    "box": {"name": "Box", "auth": "browser", "params": {}},
    "pcloud": {"name": "pCloud", "auth": "browser", "params": {}},
    "mega": {"name": "MEGA", "auth": "password", "params": {}},
    "protondrive": {"name": "Proton Drive", "auth": "password", "params": {}},
    "jottacloud": {"name": "Jottacloud", "auth": "browser", "params": {}},
    "yandex": {"name": "Yandex Disk", "auth": "browser", "params": {}},
    "zoho": {"name": "Zoho WorkDrive", "auth": "browser", "params": {}},
}

# volume labels / folder names of sync apps
MOUNT_HINTS = {"google drive": "Google Drive", "icedrive": "Icedrive", "pcloud": "pCloud Drive", "mega": "MEGA",
               "box": "Box", "dropbox": "Dropbox", "onedrive": "OneDrive", "icloud": "iCloud Drive", "sync": "Sync.com",
               "proton": "Proton Drive", "tresorit": "Tresorit"}


# ----------------------------------------------------------------- rclone
def rclone_exe(home: Path | None = None) -> str | None:
    found = shutil.which("rclone")
    if found:
        return found
    cands = []
    if home:
        cands.append(home / "bin" / ("rclone.exe" if os.name == "nt" else "rclone"))
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(Path(la) / "Microsoft" / "WinGet" / "Links" / "rclone.exe")
        pk = Path(la) / "Microsoft" / "WinGet" / "Packages"
        if pk.is_dir():
            cands += sorted(pk.glob("Rclone.Rclone*/rclone-*/rclone.exe"))
    for c in cands:
        if Path(c).exists():
            return str(c)
    return None


def install_rclone(home: Path) -> str:
    """Get rclone: winget on Windows if available, otherwise the official zip from rclone.org."""
    if rclone_exe(home):
        return "rclone is already installed."
    if os.name == "nt" and shutil.which("winget"):
        r = subprocess.run(["winget", "install", "--id", "Rclone.Rclone", "-e", "--silent",
                            "--accept-source-agreements", "--accept-package-agreements"],
                           capture_output=True, text=True, timeout=600)
        if rclone_exe(home):
            return "Installed rclone."
    import httpx
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    osname = {"Windows": "windows", "Darwin": "osx", "Linux": "linux"}[platform.system()]
    url = f"https://downloads.rclone.org/rclone-current-{osname}-{arch}.zip"
    bindir = home / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    z = bindir / "rclone.zip"
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with z.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    name = "rclone.exe" if os.name == "nt" else "rclone"
    with zipfile.ZipFile(z) as zf:
        member = next(m for m in zf.namelist() if m.endswith("/" + name))
        (bindir / name).write_bytes(zf.read(member))
    os.chmod(bindir / name, 0o755)
    z.unlink()
    return "Installed rclone."


class Rclone:
    def __init__(self, home: Path):
        self.home = home
        self.conf = home / "rclone.conf"          # LocalAgent's own; holds sign-in tokens, so private
        self.exe = rclone_exe(home)

    def run(self, *args: str, timeout: float = 300, check: bool = True) -> str:
        if not self.exe:
            self.exe = rclone_exe(self.home)
            if not self.exe:
                raise RuntimeError("rclone isn't installed yet. Press Connect again and LocalAgent will install it.")
        for a in args:   # belt and braces: these rclone commands delete things
            if a in ("delete", "deletefile", "purge", "rmdir", "rmdirs", "cleanup", "dedupe", "sync", "bisync"):
                raise PermissionError(f"LocalAgent never runs 'rclone {a}' (it can delete files)")
        r = subprocess.run([self.exe, "--config", str(self.conf), *args], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1][:400] if (r.stderr or r.stdout).strip()
                               else f"rclone failed ({r.returncode})")
        try:
            os.chmod(self.conf, 0o600)
        except OSError:
            pass
        return r.stdout

    def create(self, name: str, rtype: str, params: dict, timeout: float = 600) -> None:
        """Add a remote. For browser sign-in providers rclone opens the browser
        and waits for you to approve, then saves the token."""
        args = ["config", "create", name, rtype]
        for k, v in params.items():
            args.append(f"{k}={v}")
        if any(k in params for k in ("pass", "password")):
            args.append("--obscure")
        self.run(*args, timeout=timeout)

    def remove_remote(self, name: str) -> None:
        self.run("config", "delete", name, check=False)   # removes only LocalAgent's saved sign-in, not files


# ------------------------------------------------------------- detection
def _volume_label(root: str) -> str:
    if os.name != "nt":
        return ""
    buf = ctypes.create_unicode_buffer(261)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), buf, 261, None, None, None, None, 0)
        return buf.value if ok else ""
    except Exception:
        return ""


def detect_mounts() -> list[dict]:
    """Cloud drives and sync folders on this computer."""
    found: list[dict] = []
    seen: set[str] = set()

    def add(path: Path, service: str):
        key = str(path).lower().rstrip("\\/")
        if key in seen or not path.is_dir():
            return
        seen.add(key)
        found.append({"type": "storage", "mode": "folder", "service": service, "path": str(path),
                      "name": f"{service} ({path})"})
    # drives with a cloud volume label (Icedrive, Google Drive, pCloud…)
    try:
        import psutil  # type: ignore
        parts = psutil.disk_partitions(all=True)
    except Exception:
        parts = []
    for p in parts:
        label = _volume_label(p.mountpoint).lower()
        for hint, service in MOUNT_HINTS.items():
            if hint in label:
                root = Path(p.mountpoint)
                md = root / "My Drive"
                add(md if md.is_dir() else root, service)
    home = Path.home()
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(env):
            add(Path(os.environ[env]), "OneDrive")
    for d in home.glob("OneDrive*"):
        add(d, "OneDrive")
    try:
        info = json.loads((Path(os.environ.get("LOCALAPPDATA", "")) / "Dropbox" / "info.json").read_text(encoding="utf-8"))
        for v in info.values():
            if isinstance(v, dict) and v.get("path"):
                add(Path(v["path"]), "Dropbox")
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    for name, service in (("Dropbox", "Dropbox"), ("iCloudDrive", "iCloud Drive"), ("Box", "Box"),
                          ("Google Drive", "Google Drive"), ("pCloudDrive", "pCloud Drive"), ("MEGA", "MEGA"),
                          ("Library/Mobile Documents/com~apple~CloudDocs", "iCloud Drive"),
                          ("Library/CloudStorage", None)):
        p = home / name
        if service is None:
            if p.is_dir():
                for sub in p.iterdir():
                    svc = next((s for h, s in MOUNT_HINTS.items() if h in sub.name.lower()), None)
                    if svc:
                        add(sub, svc)
            continue
        add(p, service)
    return found


def local_drives() -> list[dict]:
    out = []
    try:
        import psutil  # type: ignore
        for p in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            if u.total < 2 * 2**30:
                continue
            out.append({"name": p.mountpoint, "label": _volume_label(p.mountpoint), "total": u.total, "used": u.used, "free": u.free})
    except Exception:
        u = shutil.disk_usage(Path.home().anchor or "/")
        out.append({"name": Path.home().anchor or "/", "label": "", "total": u.total, "used": u.used, "free": u.free})
    return out


def human(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ------------------------------------------------------------ connection
class StorageConnection:
    kind = "storage"

    def __init__(self, cid: str, spec: dict, secrets, home: Path):
        self.id, self.spec, self.secrets, self.home = cid, spec, secrets, home
        self.mode = spec.get("mode", "folder")
        self.service = spec.get("service") or PROVIDERS.get(spec.get("provider", ""), {}).get("name", "Storage")
        self.root = Path(spec.get("path", "")) if self.mode == "folder" else None
        self.remote = spec.get("remote", "")
        self.label = spec.get("name") or (f"{self.service} ({self.root})" if self.root else self.service)
        self.rc = Rclone(home) if self.mode == "rclone" else None

    # --------------------------------------------------------- helpers
    def _r(self, path: str = "") -> str:
        return f"{self.remote}:{path.strip('/')}"

    def _p(self, path: str = "") -> Path:
        p = (self.root / path.strip("/\\")).resolve() if path else self.root.resolve()
        if self.root.resolve() != p and self.root.resolve() not in p.parents:
            raise PermissionError("that path is outside this storage")
        return p

    def exists(self, path: str) -> bool:
        if self.mode == "folder":
            return self._p(path).exists()
        out = self.rc.run("lsjson", "--stat", self._r(path), check=False, timeout=60)
        return bool(out.strip()) and out.strip() != "null"

    # ----------------------------------------------------------- reads
    @property
    def own_volume(self) -> bool:
        """True when the folder is a cloud drive of its own (I:\\, G:\\My Drive), so the drive's
        size is the cloud account's space. A sync folder inside C:\\Users only shows C:'s space."""
        r = self.root.resolve()
        return r == Path(r.anchor) or (r.parent == Path(r.anchor) and r.name.lower() == "my drive")

    def about(self) -> dict:
        if self.mode == "folder":
            if not self.own_volume:
                return {"total": None, "used": None, "free": None, "trashed": None, "synced_folder": True}
            u = shutil.disk_usage(self.root)
            return {"total": u.total, "used": u.used, "free": u.free, "trashed": None}
        d = json.loads(self.rc.run("about", self._r(), "--json", timeout=120) or "{}")
        return {"total": d.get("total"), "used": d.get("used"), "free": d.get("free"), "trashed": d.get("trashed")}

    def test(self) -> str:
        a = self.about()
        if a.get("synced_folder"):
            if not self.root.is_dir():
                raise RuntimeError(f"{self.root} isn't there")
            return (f"Connected: synced folder {self.root}. Its cloud space isn't visible from a folder — "
                    f"connect your {self.service} account too if you want that.")
        if a["total"]:
            return f"Connected: {human(a['used'])} used of {human(a['total'])} ({human(a['free'])} free)"
        return f"Connected: {human(a['used'])} used"

    def list(self, path: str = "", limit: int = 200) -> list[dict]:
        if self.mode == "folder":
            out = []
            for e in sorted(self._p(path).iterdir(), key=lambda x: x.name.lower())[:limit]:
                try:
                    st = e.stat()
                except OSError:
                    continue
                out.append({"name": e.name, "dir": e.is_dir(), "size": None if e.is_dir() else st.st_size,
                            "modified": st.st_mtime, "path": str(e.relative_to(self.root)).replace("\\", "/")})
            return out
        items = json.loads(self.rc.run("lsjson", self._r(path), "--max-depth", "1", timeout=180) or "[]")
        return [{"name": i["Name"], "dir": i["IsDir"], "size": None if i["IsDir"] else i.get("Size"),
                 "modified": _ts(i.get("ModTime")), "path": i["Path"] if not path else f"{path.strip('/')}/{i['Path']}"}
                for i in items[:limit]]

    def walk(self, path: str = "", min_size: int = 0, pattern: str = "", limit: int = 5000, max_depth: int = 25,
             max_seconds: float = 90) -> list[dict]:
        """Files under path (names and sizes only; online-only files aren't downloaded).
        Big network drives can hold millions of files, so a scan stops after
        max_seconds and self.partial says so."""
        self.partial = False
        deadline = time.time() + max_seconds
        if self.mode == "folder":
            out, base = [], self._p(path)
            for dirpath, dirnames, filenames in os.walk(base):
                if time.time() > deadline:
                    self.partial = True
                    return out
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("agent_trash", "$RECYCLE.BIN", "System Volume Information")]
                if Path(dirpath).relative_to(base).parts.__len__() >= max_depth:
                    dirnames[:] = []
                for f in filenames:
                    if pattern and not fnmatch.fnmatch(f.lower(), pattern.lower()):
                        continue
                    fp = Path(dirpath) / f
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    if st.st_size >= min_size:
                        out.append({"path": str(fp.relative_to(self.root)).replace("\\", "/"), "size": st.st_size, "modified": st.st_mtime})
                        if len(out) >= limit:
                            return out
            return out
        args = ["lsjson", self._r(path), "-R", "--files-only", "--fast-list", "--max-depth", str(max_depth)]
        if min_size:
            args += ["--min-size", f"{max(1, min_size // 1024)}k"]
        if pattern:
            args += ["--ignore-case", "--include", pattern if any(c in pattern for c in "*?") else f"*{pattern}*"]
        try:
            items = json.loads(self.rc.run(*args, timeout=max(60.0, max_seconds * 4)) or "[]")
        except subprocess.TimeoutExpired:
            self.partial = True
            return []
        base = path.strip("/")
        return [{"path": f"{base}/{i['Path']}" if base else i["Path"], "size": i.get("Size", 0), "modified": _ts(i.get("ModTime"))}
                for i in items[:limit] if "agent_trash/" not in i["Path"]]

    def search(self, query: str, limit: int = 30) -> list[dict]:
        pat = query if any(c in query for c in "*?") else f"*{query}*"
        return sorted(self.walk(pattern=pat, limit=limit * 4), key=lambda x: -x["modified"])[:limit]

    def big_files(self, min_mb: float = 100, limit: int = 20, path: str = "") -> list[dict]:
        files = self.walk(path=path, min_size=int(min_mb * 2**20), limit=20000)
        return sorted(files, key=lambda x: -x["size"])[:limit]

    # ---------------------------------------------------------- writes
    def download(self, path: str, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _free_name(dest_dir / Path(path).name)
        if self.mode == "folder":
            shutil.copy2(self._p(path), dest)
        else:
            self.rc.run("copyto", self._r(path), str(dest), timeout=3600)
        return dest

    def upload(self, local: Path, dest: str) -> str:
        """Copy a local file or folder in. Never replaces something already there."""
        dest = dest.strip("/") or local.name
        if self.exists(dest):
            stem, suf = Path(dest).stem, Path(dest).suffix
            parent = str(Path(dest).parent).replace("\\", "/").strip(".")
            i = 2
            while self.exists(f"{parent}/{stem} ({i}){suf}".strip("/")):
                i += 1
            dest = f"{parent}/{stem} ({i}){suf}".strip("/")
        if self.mode == "folder":
            target = self._p(dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if local.is_dir() else shutil.copy2)(local, target)
        else:
            self.rc.run("copyto" if local.is_file() else "copy", str(local), self._r(dest), timeout=24 * 3600)
        return dest

    def verify(self, local: Path, dest: str) -> bool:
        """Does the stored copy match the local file? Size plus a checksum when the service offers one."""
        size = local.stat().st_size
        if self.mode == "folder":
            t = self._p(dest)
            return t.is_file() and t.stat().st_size == size and _hash(t, "md5") == _hash(local, "md5")
        st = json.loads(self.rc.run("lsjson", "--stat", "--hash", self._r(dest), timeout=300) or "{}") or {}
        if st.get("Size") != size:
            return False
        hashes = {k.lower(): v for k, v in (st.get("Hashes") or {}).items() if v}
        for algo in ("md5", "sha1", "sha256"):
            if algo in hashes:
                return hashes[algo].lower() == _hash(local, algo)
        return True   # service gives no common checksum (e.g. OneDrive quickxor): size match is the check

    def move(self, src: str, dest: str) -> str:
        if self.exists(dest):
            raise FileExistsError(f"{dest} already exists; LocalAgent never replaces files. Pick another name.")
        if self.mode == "folder":
            t = self._p(dest)
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self._p(src)), str(t))
        else:
            self.rc.run("moveto", self._r(src), self._r(dest), timeout=24 * 3600)
        return dest

    def trash(self, path: str) -> str:
        """'Remove' = move into agent_trash on the same storage. Nothing is deleted."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = f"agent_trash/{stamp}/{path.strip('/')}"
        return self.move(path, dest)


def _ts(s) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _free_name(p: Path) -> Path:
    i = 2
    out = p
    while out.exists():
        out = p.with_name(f"{p.stem} ({i}){p.suffix}")
        i += 1
    return out


def _hash(p: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
