"""Never-delete enforcement, in three layers:

1. Shims  — `rm`, `rmdir`, `unlink`, `shred` on PATH move things to agent_trash;
            `mv` and `cp` first move anything they'd overwrite into the trash.
2. Python hook — a sitecustomize.py on PYTHONPATH patches os.remove/unlink/
            rmdir/removedirs, shutil.rmtree and pathlib so any Python the
            agent runs (scripts, pip-installed tools) trashes instead of deletes.
3. Screening — shell commands that would bypass 1 and 2 (`/bin/rm`, `\\rm`,
            `find -delete`, `git clean`, `rsync --delete`, Windows `del`...)
            are refused before they run.

These are strong guardrails, not a jail: a compiled program the agent builds
could still call unlink(2) directly. For an absolute guarantee, run the agent
as a separate OS user whose files are backed up, or in a container.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from .config import Config

IS_WINDOWS = os.name == "nt"

_POSIX_BLOCKS: list[tuple[str, str]] = [
    (r"(^|[;&|`(\s])(/usr)?/s?bin/(rm|rmdir|unlink|shred|mv|cp)\b", "absolute path to a delete/overwrite binary bypasses the trash"),
    (r"(^|[;&|`(\s])\\(rm|rmdir|unlink|shred)\b", "escaped command bypasses the trash"),
    (r"\bcommand\s+(-\w+\s+)*(rm|rmdir|unlink|shred)\b", "`command` bypasses the trash"),
    (r"\bbusybox\s+(rm|rmdir|unlink)\b", "busybox delete bypasses the trash"),
    (r"\bsudo\b[^;&|]*\b(rm|rmdir|unlink|shred|mv|cp|find|git|rsync)\b", "sudo resets PATH and bypasses the trash"),
]

_COMMON_BLOCKS: list[tuple[str, str]] = [
    (r"\bfind\b[^;&|]*\s-delete\b", "find -delete; use find ... -exec rm {} + (rm goes to the trash)"),
    (r"\bgit\s+clean\b", "git clean deletes untracked files"),
    (r"\bgit\s+rm\b(?![^;&|]*--cached)", "git rm deletes the working file; use `git rm --cached` then trash it"),
    (r"\bgit\s+reset\b[^;&|]*--hard\b", "git reset --hard discards work; commit or stash first"),
    (r"\bgit\s+checkout\b[^;&|]*\s(-f|--force)\b", "forced checkout discards work"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "dropping stashes discards work"),
    (r"\brsync\b[^;&|]*--(delete|remove-source-files)", "rsync deletion flags"),
    (r"\bdd\b[^;&|]*\bof=", "dd can overwrite disks/files irreversibly"),
    (r"\b(mkfs(\.\w+)?|wipefs|diskpart|fdisk|parted)\b", "disk formatting/partitioning"),
    (r"\bfs(\.promises)?\.(unlink|rm|rmdir)(Sync)?\b|\.(unlink|rm|rmdir)Sync\b|\)\.(unlink|rm|rmdir)\(|\brimraf\b|\bdel-cli\b",
     "Node.js deletion APIs"),
    (r"\bFileUtils\.(rm|remove)|\bFile\.delete\b|\bunlink\s*\(", "deletion API in inline script"),
]

# On Windows, commands run in PowerShell with POWERSHELL_LAYER loaded first,
# so Remove-Item and its aliases (rm, del, erase, rd, rmdir, ri) move things
# to agent_trash, and overwriting cmdlets back the old file up first. What
# the layer can't intercept is blocked here.
_WINDOWS_BLOCKS: list[tuple[str, str]] = [
    (r"(?i)\[(system\.)?io\.(file|directory|fileinfo|directoryinfo|path)\]\s*::\s*(delete|writeall\w*|move|copy|replace|create\b)",
     ".NET file methods bypass the trash; use normal PowerShell commands"),
    (r"(?i)\.(delete|movetorecyclebin)\s*\(", ".NET delete methods bypass the trash"),
    (r"(?i)\bcmd(\.exe)?\b[^;&|]*\s/[ck]\b[^;&|]*\b(del|erase|rd|rmdir|copy|move|ren|rename|xcopy|robocopy)\b",
     "cmd.exe file commands bypass the trash; use PowerShell's Remove-Item/Copy-Item (they're protected)"),
    (r"(?i)(^|[;&|\s(])(xcopy|robocopy)\b", "xcopy/robocopy can overwrite or mirror-delete; use Copy-Item"),
    (r"(?i)\bclear-recyclebin\b", "emptying the recycle bin"),
    (r"(?i)\bformat(-volume)?\s+[a-z]:|\bformat-volume\b|\bclear-disk\b|\bremove-partition\b", "formatting a drive"),
    (r"(?i)\b(microsoft\.powershell\.management|microsoft\.powershell\.utility)\\", "calling the unprotected built-in cmdlets directly"),
    (r"(?i)\bremove-itemproperty\b|\bremove-variable\s+-name\s+__la", "tampering with the protection layer"),
    (r"(?i)\b(remove-item|rm|del)\b[^;|]*function:", "tampering with the protection layer"),
    (r"(?i)\bpowershell(\.exe)?\b[^;|]*-(noprofile|command|encodedcommand|e|ec)\b|\bpwsh(\.exe)?\b", "starting a second PowerShell skips the protection layer"),
]

POWERSHELL_LAYER = r"""
$ErrorActionPreference = 'Continue'
function global:__la { & $env:LOCALAGENT_PYTHON @($env:LOCALAGENT_SHIM_ARGS -split ' ') @args }
function global:__la_paths($items) {
  foreach ($p in $items) { if ($null -eq $p) { continue }
    $s = if ($p -is [System.IO.FileSystemInfo]) { $p.FullName } else { "$p" }
    $r = Resolve-Path -Path $s -ErrorAction SilentlyContinue
    if (-not $r) { $r = Resolve-Path -LiteralPath $s -ErrorAction SilentlyContinue }
    foreach ($x in $r) { $x.ProviderPath } } }
function global:__la_backup($items) { $ps = @(__la_paths $items | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }); if ($ps.Count) { __la backup @ps } }
function global:__la_pos($a) { $pos = @(); $named = @{}
  for ($i = 0; $i -lt $a.Count; $i++) { $x = "$($a[$i])"
    if ($x -match '^-([A-Za-z]+):(.*)$') { $named[$matches[1].ToLower()] = $matches[2] }
    elseif ($x -match '^-([A-Za-z]+)$') { $n = $matches[1].ToLower()
      if ($n -in 'recurse','force','container','passthru','whatif','confirm','verbose','nonewline','append','noclobber','literalpath') { $named[$n] = $true }
      elseif ($i + 1 -lt $a.Count) { $named[$n] = $a[$i + 1]; $i++ } }
    else { $pos += ,$a[$i] } }
  return @{ pos = $pos; named = $named } }

function global:Remove-Item {
  $p = __la_pos $args
  $targets = @($input) + @($p.pos)
  foreach ($k in 'path','literalpath') { if ($p.named[$k] -and $p.named[$k] -isnot [bool]) { $targets += ,$p.named[$k] } }
  if ($p.named['include'] -or $p.named['exclude'] -or $p.named['filter']) {
    $gci = @{ Path = @(__la_paths $targets); Force = $true; ErrorAction = 'SilentlyContinue' }
    foreach ($k in 'include','exclude','filter') { if ($p.named[$k]) { $gci[$k] = $p.named[$k] } }
    if ($p.named['recurse']) { $gci['Recurse'] = $true }
    $targets = @(Get-ChildItem @gci) }
  $paths = @(__la_paths $targets)
  if (-not $paths.Count) { Write-Error "Remove-Item: nothing matched"; return }
  __la trash @paths
  Write-Host "[moved to agent_trash: $($paths.Count) item(s) — restore from the Trash page]" }
foreach ($a in 'rm','del','erase','rd','rmdir','ri') { Set-Alias -Name $a -Value Remove-Item -Scope Global -Option AllScope -Force }

function global:__la_dest_guard($a, $fallbackLeaf) { $p = __la_pos $a
  $dest = $p.named['destination']; if (-not $dest -and $p.pos.Count -ge 2) { $dest = $p.pos[1] }
  $src = $p.named['path']; if (-not $src -and $p.pos.Count -ge 1) { $src = $p.pos[0] }
  if (-not $dest) { return }
  if (Test-Path -LiteralPath "$dest" -PathType Container) { if ($src) { $leaf = Split-Path -Leaf "$src"; $t = Join-Path "$dest" $leaf; if (Test-Path -LiteralPath $t -PathType Leaf) { __la_backup @($t) } } }
  elseif (Test-Path -LiteralPath "$dest" -PathType Leaf) { __la_backup @("$dest") } }
function global:Copy-Item { __la_dest_guard $args; if ($MyInvocation.ExpectingInput) { $input | Microsoft.PowerShell.Management\Copy-Item @args } else { Microsoft.PowerShell.Management\Copy-Item @args } }
function global:Move-Item { __la_dest_guard $args; if ($MyInvocation.ExpectingInput) { $input | Microsoft.PowerShell.Management\Move-Item @args } else { Microsoft.PowerShell.Management\Move-Item @args } }
foreach ($a in 'cp','copy','cpi') { Set-Alias -Name $a -Value Copy-Item -Scope Global -Option AllScope -Force }
foreach ($a in 'mv','move','mi') { Set-Alias -Name $a -Value Move-Item -Scope Global -Option AllScope -Force }

function global:__la_target_guard($a, $key) { $p = __la_pos $a; $t = $p.named[$key]; if (-not $t) { $t = $p.named['path'] }; if (-not $t -and $p.pos.Count) { $t = $p.pos[0] }
  if ($t -and -not $p.named['append']) { __la_backup @("$t") } }
function global:Set-Content { __la_target_guard $args 'path'; if ($MyInvocation.ExpectingInput) { $input | Microsoft.PowerShell.Management\Set-Content @args } else { Microsoft.PowerShell.Management\Set-Content @args } }
function global:Clear-Content { __la_target_guard $args 'path'; Microsoft.PowerShell.Management\Clear-Content @args }
function global:Out-File { __la_target_guard $args 'filepath'; if ($MyInvocation.ExpectingInput) { $input | Microsoft.PowerShell.Utility\Out-File @args } else { Microsoft.PowerShell.Utility\Out-File @args } }
function global:New-Item { $p = __la_pos $args; if ($p.named['force']) { $t = $p.named['path']; if (-not $t -and $p.pos.Count) { $t = $p.pos[0] }; if ($t) { $n = $p.named['name']; if ($n) { $t = Join-Path "$t" "$n" }; __la_backup @("$t") } }
  Microsoft.PowerShell.Management\New-Item @args }
function global:Rename-Item { $p = __la_pos $args; $src = $p.named['path']; if (-not $src -and $p.pos.Count) { $src = $p.pos[0] }; $new = $p.named['newname']; if (-not $new -and $p.pos.Count -ge 2) { $new = $p.pos[1] }
  if ($src -and $new) { $t = Join-Path (Split-Path -Parent (Resolve-Path "$src")) "$new"; __la_backup @($t) }
  Microsoft.PowerShell.Management\Rename-Item @args }
foreach ($a in 'ren','rni') { Set-Alias -Name $a -Value Rename-Item -Scope Global -Option AllScope -Force }
foreach ($a in 'sc') { Set-Alias -Name $a -Value Set-Content -Scope Global -Option AllScope -Force -ErrorAction SilentlyContinue }
$global:__la_ready = $true
"""


def _redirect_targets(cmd: str) -> list[str]:
    """Files a PowerShell command overwrites with `>` (not `>>`, not 2>&1)."""
    out = []
    for m in re.finditer(r"(?<![>&\d])(?:\d)?>(?![>&])\s*(\"([^\"]+)\"|'([^']+)'|([^\s|;&<>()]+))", cmd):
        t = m.group(2) or m.group(3) or m.group(4) or ""
        if t and t.lower() not in ("$null", "nul", "null"):
            out.append(t)
    return out

BLOCK_HINT = (
    "BLOCKED by the never-delete policy ({why}). Nothing was run. "
    "To remove something, use the trash_path tool"
    + ("" if IS_WINDOWS else " or plain `rm` (it is redirected to agent_trash)")
    + "."
)


def check_shell_command(cmd: str) -> str | None:
    """Return a refusal message if `cmd` would bypass the trash, else None."""
    # Windows can also have POSIX tools installed (for example through Git).
    # Explicit binary paths must not bypass screening on either platform.
    rules = _COMMON_BLOCKS + _POSIX_BLOCKS + (_WINDOWS_BLOCKS if IS_WINDOWS else [])
    for pattern, why in rules:
        if re.search(pattern, cmd):
            return BLOCK_HINT.format(why=why)
    return None


# --------------------------------------------------------------------- hooks

def default_exempt_paths() -> list[str]:
    """Paths where real deletion is fine: OS temp space and caches. Without
    this, every pip install / build step would flood the trash."""
    paths = [tempfile.gettempdir(), str(Path.home() / ".cache")]
    if not IS_WINDOWS:
        paths += ["/tmp", "/var/tmp"]
    return [os.path.realpath(p) for p in paths]


SHIM_NAMES = ["rm", "rmdir", "unlink", "shred", "mv", "cp"]

SITECUSTOMIZE = r'''# Installed by localagent: turns deletes into moves to agent_trash.
import os, sys, threading
def _la_install():
    trash_dir = os.environ.get("LOCALAGENT_TRASH")
    mod_path = os.environ.get("LOCALAGENT_TRASH_MODULE")
    if not trash_dir or not mod_path or getattr(os, "_localagent_patched", False):
        return
    import importlib.util, shutil, pathlib
    spec = importlib.util.spec_from_file_location("_localagent_trash", mod_path)
    T = importlib.util.module_from_spec(spec); spec.loader.exec_module(T)
    trash = T.Trash(trash_dir)
    exempt = [p for p in os.environ.get("LOCALAGENT_TRASH_EXEMPT", "").split(os.pathsep) if p]
    skip_parts = {"__pycache__", "site-packages", "dist-packages", ".pytest_cache", "node_modules"}
    tl = threading.local()
    orig = {"unlink": os.unlink, "rmdir": os.rmdir, "removedirs": os.removedirs, "rmtree": shutil.rmtree}

    def _abs(path, dir_fd=None):
        p = os.fsdecode(path)
        if dir_fd is not None and not os.path.isabs(p):
            try:
                base = os.readlink("/proc/self/fd/%d" % dir_fd)
            except OSError:
                return None
            p = os.path.join(base, p)
        return os.path.realpath(os.path.abspath(p))

    protect = [p for p in os.environ.get("LOCALAGENT_PROTECT", "").split(os.pathsep) if p]
    def _under(p, roots):
        return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)
    def _is_exempt(p):
        if getattr(tl, "inside", False):
            return True
        if p is None or _under(p, protect):
            return False
        if any(part in skip_parts for part in p.replace("\\", "/").split("/")):
            return True
        return _under(p, exempt)

    def _to_trash(p, why):
        try:
            trash.move(p, reason=why)
        except T.TrashError as e:
            raise PermissionError("localagent never-delete policy: %s" % e)

    def unlink(path, *a, dir_fd=None, **k):
        p = _abs(path, dir_fd)
        if _is_exempt(p):
            return orig["unlink"](path, *a, dir_fd=dir_fd, **k) if dir_fd is not None else orig["unlink"](path, *a, **k)
        if p is None:
            raise PermissionError("localagent never-delete policy: unresolvable path")
        if not os.path.lexists(p):
            raise FileNotFoundError(2, "No such file or directory", os.fsdecode(path))
        _to_trash(p, "python os.unlink")

    def rmdir(path, *a, dir_fd=None, **k):
        p = _abs(path, dir_fd)
        if _is_exempt(p):
            return orig["rmdir"](path, *a, dir_fd=dir_fd, **k) if dir_fd is not None else orig["rmdir"](path, *a, **k)
        if p is None or not os.path.isdir(p):
            raise FileNotFoundError(2, "No such directory", os.fsdecode(path))
        _to_trash(p, "python os.rmdir")

    def removedirs(name):
        p = _abs(name)
        if _is_exempt(p):
            return orig["removedirs"](name)
        _to_trash(p, "python os.removedirs")

    def rmtree(path, ignore_errors=False, onerror=None, *a, **k):
        p = _abs(path, k.get("dir_fd"))
        if _is_exempt(p):
            tl.inside = True
            try:
                return orig["rmtree"](path, ignore_errors, onerror, *a, **k)
            finally:
                tl.inside = False
        try:
            if p is None or not os.path.lexists(p):
                raise FileNotFoundError(2, "No such file or directory", os.fsdecode(path))
            _to_trash(p, "python shutil.rmtree")
        except Exception:
            if not ignore_errors:
                raise
    rmtree.avoids_symlink_attacks = getattr(orig["rmtree"], "avoids_symlink_attacks", False)

    os.unlink = unlink; os.remove = unlink; os.rmdir = rmdir; os.removedirs = removedirs
    shutil.rmtree = rmtree
    def _p_unlink(self, missing_ok=False):
        try:
            unlink(str(self))
        except FileNotFoundError:
            if not missing_ok:
                raise
    def _p_rmdir(self):
        rmdir(str(self))
    pathlib.Path.unlink = _p_unlink
    pathlib.Path.rmdir = _p_rmdir
    os._localagent_patched = True
try:
    _la_install()
except Exception as _e:
    sys.stderr.write("[localagent] trash hook failed to load: %r\n" % (_e,))
'''

SHIM_SH = '#!/bin/sh\nexec "${LOCALAGENT_PYTHON:-python3}" ${LOCALAGENT_SHIM_ARGS:--m localagent.shim_cli} "$(basename "$0")" "$@"\n'


def hooks_dir(cfg: Config) -> Path:
    return cfg.home / "hooks"


def ensure_hooks(cfg: Config) -> None:
    d = hooks_dir(cfg)
    (d / "pyhook").mkdir(parents=True, exist_ok=True)
    (d / "pyhook" / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    if not IS_WINDOWS:
        (d / "shims").mkdir(parents=True, exist_ok=True)
        for name in SHIM_NAMES:
            p = d / "shims" / name
            p.write_text(SHIM_SH, encoding="utf-8")
            p.chmod(0o755)


def _console_python() -> str:
    """pythonw.exe (no console) isn't waited for by PowerShell, so a trash move
    could still be running when the next command starts. Use python.exe."""
    exe = sys.executable
    if IS_WINDOWS and exe.lower().endswith("pythonw.exe"):
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand):
            return cand
    return exe


def build_env(cfg: Config, extra: dict | None = None) -> dict[str, str]:
    """Environment for every subprocess the agent starts."""
    env = dict(os.environ)
    d = hooks_dir(cfg)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    exempt = default_exempt_paths()
    env.update({
        "LOCALAGENT_TRASH": str(cfg.trash_dir),
        "LOCALAGENT_TRASH_MODULE": str(Path(__file__).resolve().parent / "trash.py"),
        "LOCALAGENT_TRASH_EXEMPT": os.pathsep.join(exempt),
        # the workspace is always protected, even if it lives inside a temp folder
        "LOCALAGENT_PROTECT": os.pathsep.join([os.path.realpath(cfg.workspace)]),
        "LOCALAGENT_PYTHON": _console_python(),
        # packaged app: the executable itself serves the shims (see launcher.py)
        "LOCALAGENT_SHIM_ARGS": "--shim" if getattr(sys, "frozen", False) else "-m localagent.shim_cli",
        "LOCALAGENT_SHIM_DIR": str(d / "shims"),
        "PYTHONPATH": os.pathsep.join(
            [str(d / "pyhook")] + [p for p in [env.get("PYTHONPATH", "")] if p] + [pkg_parent]
        ),
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONUNBUFFERED": "1",
    })
    if not IS_WINDOWS:
        env["PATH"] = str(d / "shims") + os.pathsep + env.get("PATH", "")
    if extra:
        env.update(extra)
    return env


# ------------------------------------------------------------- path policy

def resolve_path(cfg: Config, path: str) -> Path:
    p = Path(os.path.expanduser(str(path)))
    if not p.is_absolute():
        p = cfg.workspace / p
    return Path(os.path.abspath(p))


def check_writable(cfg: Config, p: Path) -> None:
    """Raise PermissionError if the agent may not write to p."""
    rp = Path(os.path.realpath(p))
    if rp == cfg.trash_dir or cfg.trash_dir in rp.parents:
        raise PermissionError("agent_trash is read-only for the agent (use restore_from_trash)")
    roots = cfg.allowed_roots
    if roots and not any(rp == r or r in rp.parents for r in roots + [cfg.workspace]):
        raise PermissionError(
            f"{rp} is outside the allowed write roots {[str(r) for r in roots]}. "
            "Add a folder to allowed_roots in ~/.localagent/config.yaml to permit it."
        )
