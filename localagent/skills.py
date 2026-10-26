"""Skills: folders with a SKILL.md that teach the agent a procedure.

    ~/.localagent/skills/weekly-report/SKILL.md
    ---
    name: weekly-report
    description: Write the Friday status report from my notes folder
    ---
    1. Read ~/notes/this-week/*.md ...

Same format as Claude / OpenClaw skills, so existing ones can be dropped in.
Only each skill's name and description sit in the prompt; the full text
loads when the agent calls use_skill (keeps small context windows free).

Safety, learned from the 2026 skill-registry malware campaigns:
  • installing from the internet always shows a scan report and needs the
    user's OK — never automatic;
  • the scan flags "prerequisite" installers, piped downloads, encoded
    blobs, credential/wallet access and data upload;
  • text from third-party skills counts as outside content, so a skill
    can't unlock data-sending or persistent actions on its own.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import yaml

BUILTIN_DIR = Path(__file__).parent / "skills_builtin"

RISK_PATTERNS = [
    (r"(curl|wget|iwr|invoke-webrequest)[^\n]{0,120}\|\s*(ba|z)?sh|\|\s*iex\b", "pipes a download straight into a shell"),
    (r"\b(prerequisite|first install|you must install|run this installer|download and run)\b", "asks you to install something first"),
    (r"base64\s+(-d|--decode)|frombase64string|atob\(", "decodes hidden content"),
    (r"[A-Za-z0-9+/]{200,}={0,2}", "contains a long encoded blob"),
    (r"\b(seed phrase|private key|wallet\.dat|keychain|\.ssh/|id_rsa|cookies\.sqlite|login data)\b", "touches passwords, keys or wallets"),
    (r"\b(webhook|pastebin|glot\.io|transfer\.sh|ngrok|discord(app)?\.com/api/webhooks)\b", "sends data to a drop site"),
    (r"\b(chmod \+x|xattr -d com\.apple\.quarantine|set-executionpolicy)\b", "makes downloaded files executable"),
    (r"\b(ignore (previous|prior|your) instructions|disable (the )?(safety|guard|approval))", "tries to override safety rules"),
    (r"\bschedule_task\b|\bcron(tab)?\b", "sets up things that run on their own"),
]


def parse_skill(text: str) -> tuple[dict, str]:
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = m.group(2)
    return (meta if isinstance(meta, dict) else {}), body.strip()


def scan(text: str) -> list[str]:
    return sorted({why for pat, why in RISK_PATTERNS if re.search(pat, text, re.I)})


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return s[:60] or "skill"


class Skill:
    def __init__(self, path: Path, source: str):
        self.path = path  # the SKILL.md file
        self.source = source  # builtin | mine | installed | external
        meta, self.body = parse_skill(path.read_text(encoding="utf-8", errors="replace"))
        self.name = slug(str(meta.get("name") or path.parent.name))
        self.description = str(meta.get("description") or self.body.split("\n", 1)[0])[:300]
        self.origin = str(meta.get("installed_from") or "")

    @property
    def third_party(self) -> bool:
        return self.source in ("installed", "external")

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.path.parent)) for p in self.path.parent.rglob("*")
                      if p.is_file() and p.name != "SKILL.md")[:50]


class SkillStore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mine = cfg.home / "skills"
        self.mine.mkdir(parents=True, exist_ok=True)

    def dirs(self) -> list[tuple[Path, str]]:
        out = [(BUILTIN_DIR, "builtin"), (self.mine, "mine")]
        for d in self.cfg.data.get("skill_dirs") or []:  # e.g. ~/.claude/skills
            out.append((Path(d).expanduser(), "external"))
        return out

    def all(self) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        for root, src in self.dirs():
            if not root.is_dir():
                continue
            for md in sorted(root.glob("*/SKILL.md")):
                try:
                    sk = Skill(md, src)
                except OSError:
                    continue
                if src == "mine" and sk.origin:
                    sk.source = "installed"
                found[sk.name] = sk  # later dirs override earlier
        disabled = set(self.cfg.data.get("disabled_skills") or [])
        return {k: v for k, v in found.items() if k not in disabled}

    def save(self, rt, name: str, description: str, instructions: str, origin: str = "") -> Path:
        d = self.mine / slug(name)
        p = d / "SKILL.md"
        if p.exists():
            rt.trash.backup(p, reason="skill updated")
        d.mkdir(parents=True, exist_ok=True)
        meta = {"name": slug(name), "description": description.strip()}
        if origin:
            meta["installed_from"] = origin
        p.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n" + instructions.strip() + "\n", encoding="utf-8")
        return p

    def fetch(self, source: str) -> tuple[str, dict[str, bytes]]:
        """Download a skill from a SKILL.md URL, a .zip URL, or a GitHub folder URL.
        Returns (SKILL.md text, other files)."""
        import httpx
        src = source.strip()
        m = re.match(r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)", src)
        if m:
            src = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4).rstrip('/')}/SKILL.md"
        m = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", src)
        if m:
            src = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}"
        r = httpx.get(src, follow_redirects=True, timeout=30)
        r.raise_for_status()
        if src.lower().endswith(".zip") or r.headers.get("content-type", "").endswith("zip"):
            z = zipfile.ZipFile(io.BytesIO(r.content))
            names = [n for n in z.namelist() if n.endswith("SKILL.md")]
            if not names:
                raise ValueError("no SKILL.md in that zip")
            root = names[0][: -len("SKILL.md")]
            files = {n[len(root):]: z.read(n) for n in z.namelist()
                     if n.startswith(root) and not n.endswith("/") and n != names[0] and len(n) - len(root) < 200}
            return z.read(names[0]).decode("utf-8", "replace"), files
        return r.text, {}

    def install(self, rt, source: str) -> str:
        text, files = self.fetch(source)
        meta, body = parse_skill(text)
        name = slug(str(meta.get("name") or Path(source.rstrip("/")).stem))
        risks = scan(text + "\n".join(f"\n# {n}\n" + b.decode("utf-8", "ignore")[:20000] for n, b in files.items()))
        scripts = [n for n in files if re.search(r"\.(sh|ps1|bat|cmd|exe|py|js|command|scpt|app)$", n, re.I)]
        report = (f"Install the skill “{name}” from {source}? It says it will: {str(meta.get('description', ''))[:200]}. "
                  + (f"⚠ Scan found: {', '.join(risks)}. " if risks else "The safety scan found nothing alarming. ")
                  + (f"It includes {len(scripts)} script file(s): {', '.join(scripts[:5])} (they won't run automatically). " if scripts else "")
                  + "Only install skills from people you trust.")
        if not rt.request_approval(report):
            raise PermissionError("the user did not approve installing this skill")
        p = self.save(rt, name, str(meta.get("description") or ""), body, origin=source)
        for rel, data in files.items():
            dest = (p.parent / rel).resolve()
            if p.parent.resolve() in dest.parents:  # no path traversal
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        return f"installed skill '{name}'" + (f" (warnings: {', '.join(risks)})" if risks else "")
