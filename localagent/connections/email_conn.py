"""Email over IMAP/SMTP: read, search, save drafts, and (with your OK) send.

Works with any provider that offers IMAP and app passwords: Gmail, Yahoo,
iCloud, Fastmail, AOL, Zoho, most work and school mail. Drafts are saved
into your real Drafts folder, so you can open them in Gmail/Outlook on any
device, check them, and press send yourself.

Outlook.com/Hotmail is moving to Microsoft sign-in only and needs a
different method; see the Connections page.
"""
from __future__ import annotations

import email
import email.header
import email.policy
import email.utils
import imaplib
import re
import smtplib
import socket
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path

PRESETS = {
    "gmail": {"name": "Gmail", "imap": "imap.gmail.com", "smtp": "smtp.gmail.com", "smtp_port": 465,
              "password_help": "https://myaccount.google.com/apppasswords",
              "steps": "Turn on 2-Step Verification for your Google account, then create an app password "
                       "(name it LocalAgent) and paste the 16 letters here."},
    "yahoo": {"name": "Yahoo Mail", "imap": "imap.mail.yahoo.com", "smtp": "smtp.mail.yahoo.com", "smtp_port": 465,
              "password_help": "https://login.yahoo.com/account/security/app-passwords",
              "steps": "In Yahoo Account Security, generate an app password and paste it here."},
    "icloud": {"name": "iCloud Mail", "imap": "imap.mail.me.com", "smtp": "smtp.mail.me.com", "smtp_port": 587,
               "password_help": "https://account.apple.com/account/manage",
               "steps": "At account.apple.com → Sign-In and Security → App-Specific Passwords, create one and paste it here."},
    "fastmail": {"name": "Fastmail", "imap": "imap.fastmail.com", "smtp": "smtp.fastmail.com", "smtp_port": 465,
                 "password_help": "https://app.fastmail.com/settings/security/apps",
                 "steps": "In Fastmail Settings → Privacy & Security → App passwords, create one with IMAP and SMTP access."},
    "aol": {"name": "AOL Mail", "imap": "imap.aol.com", "smtp": "smtp.aol.com", "smtp_port": 465,
            "password_help": "https://login.aol.com/account/security",
            "steps": "In AOL Account Security, generate an app password and paste it here."},
    "other": {"name": "Other email", "imap": "", "smtp": "", "smtp_port": 465, "password_help": "",
              "steps": "Enter your provider's IMAP and SMTP server names. Use an app password if your provider offers one."},
}

DOMAIN_PRESET = {"gmail.com": "gmail", "googlemail.com": "gmail", "yahoo.com": "yahoo", "ymail.com": "yahoo",
                 "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud", "fastmail.com": "fastmail",
                 "aol.com": "aol"}
MICROSOFT_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


def guess_preset(address: str) -> str:
    dom = address.rsplit("@", 1)[-1].lower().strip()
    if dom in MICROSOFT_DOMAINS:
        return "microsoft"
    return DOMAIN_PRESET.get(dom, "other")


def _decode(s) -> str:
    if s is None:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(str(s))))
    except Exception:
        return str(s)


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "head"]):
            t.decompose()
        text = soup.get_text("\n")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def body_text(msg: email.message.Message) -> str:
    plain, html = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (plain if ctype == "text/plain" else html if ctype == "text/html" else []).append(text)
    text = "\n".join(plain).strip() or html_to_text("\n".join(html))
    # drop long quoted history so the model sees the new part of the conversation
    lines, out = text.splitlines(), []
    for ln in lines:
        if re.match(r"^On .{5,120} wrote:\s*$", ln) or ln.startswith("-----Original Message-----"):
            out.append("[earlier messages in the thread trimmed]")
            break
        if not ln.startswith(">"):
            out.append(ln)
    return "\n".join(out).strip()


class EmailConnection:
    kind = "email"

    def __init__(self, cid: str, spec: dict, secrets):
        self.id, self.spec, self.secrets = cid, spec, secrets
        self.address = spec.get("address", "")
        p = PRESETS.get(spec.get("preset", "other"), PRESETS["other"])
        self.imap_host = spec.get("imap") or p["imap"]
        self.smtp_host = spec.get("smtp") or p["smtp"]
        self.smtp_port = int(spec.get("smtp_port") or p["smtp_port"])
        self.label = spec.get("name") or f"{p['name']} ({self.address})"
        self._folders: dict[str, str] | None = None

    # ------------------------------------------------------------ plumbing
    @property
    def password(self) -> str:
        return self.secrets.get(f"{self.id}:password")

    def _imap(self) -> imaplib.IMAP4_SSL:
        if not self.password:
            raise RuntimeError(f"{self.label} has no password saved. Reconnect it on the Connections page.")
        m = imaplib.IMAP4_SSL(self.imap_host, 993, timeout=30)
        try:
            m.login(self.address, self.password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"{self.label}: sign-in was refused ({e}). If you use 2-step verification, "
                               "you need an app password rather than your normal password.") from None
        return m

    def folders(self, m) -> dict[str, str]:
        """Map special folders (inbox, drafts, sent, all, trash) to this account's names."""
        if self._folders is not None:
            return self._folders
        found = {"inbox": "INBOX"}
        typ, data = m.list()
        for raw in data or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            mm = re.match(r'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$', line)
            if not mm:
                continue
            name = mm.group("name").strip().strip('"')
            flags = mm.group("flags").lower()
            for key, flag in (("drafts", "\\drafts"), ("sent", "\\sent"), ("all", "\\all"), ("trash", "\\trash")):
                if flag in flags:
                    found[key] = name
            low = name.lower()
            if "drafts" not in found and low.endswith("drafts"):
                found["drafts"] = name
            if "sent" not in found and (low.endswith("sent") or low.endswith("sent items") or low.endswith("sent mail")):
                found["sent"] = name
        self._folders = found
        return found

    def test(self) -> str:
        m = self._imap()
        try:
            f = self.folders(m)
            typ, data = m.select("INBOX", readonly=True)
            n = int(data[0]) if typ == "OK" and data and data[0] else 0
        finally:
            _logout(m)
        return f"Connected to {self.address}: {n} messages in the inbox" + ("" if "drafts" in f else " (no Drafts folder found)")

    # ------------------------------------------------------------ reading
    def _summaries(self, m, uids: list[bytes], folder: str) -> list[dict]:
        out = []
        if not uids:
            return out
        typ, data = m.uid("fetch", b",".join(uids), "(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE PRECEDENCE AUTO-SUBMITTED)])")
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            meta = item[0].decode(errors="replace")
            uid = re.search(r"UID (\d+)", meta)
            hdr = email.message_from_bytes(item[1], policy=email.policy.compat32)
            try:
                when = parsedate_to_datetime(hdr.get("Date")).astimezone()
            except Exception:
                when = None
            out.append({"id": f"{folder}:{uid.group(1) if uid else '?'}", "from": _decode(hdr.get("From")),
                        "subject": _decode(hdr.get("Subject")) or "(no subject)",
                        "date": when.strftime("%a %b %d %H:%M") if when else "", "ts": when.timestamp() if when else 0,
                        "unread": "\\Seen" not in meta, "flagged": "\\Flagged" in meta,
                        "bulk": bool(hdr.get("List-Unsubscribe") or (hdr.get("Precedence") or "").lower() in ("bulk", "list", "junk")
                                     or (hdr.get("Auto-Submitted") or "no").lower() != "no"
                                     or re.search(r"no-?reply|notifications?@|newsletter|mailer-daemon|updates@|news@|info@",
                                                  _decode(hdr.get("From")), re.I))})
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out

    def recent(self, limit: int = 15, unread_only: bool = False, days: int = 14, folder: str = "INBOX") -> list[dict]:
        m = self._imap()
        try:
            m.select(_quote(folder), readonly=True)
            since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
            crit = ["SINCE", since] + (["UNSEEN"] if unread_only else [])
            typ, data = m.uid("search", None, *crit)
            uids = (data[0].split() if data and data[0] else [])[-max(1, min(limit, 50)):]
            return self._summaries(m, uids, folder)
        finally:
            _logout(m)

    def search(self, query: str, limit: int = 15, days: int = 365) -> list[dict]:
        m = self._imap()
        try:
            f = self.folders(m)
            folder = f.get("all", "INBOX")
            m.select(_quote(folder), readonly=True)
            if "gmail" in self.imap_host:
                typ, data = m.uid("search", "CHARSET", "UTF-8", "X-GM-RAW", _quote(query + f" newer_than:{days}d"))
            else:
                since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
                q = query.replace('"', "")
                typ, data = m.uid("search", "CHARSET", "UTF-8", "SINCE", since, "OR", "OR", "SUBJECT", _quote(q),
                                  "FROM", _quote(q), "BODY", _quote(q))
            uids = (data[0].split() if data and data[0] else [])[-max(1, min(limit, 50)):]
            return self._summaries(m, uids, folder)
        finally:
            _logout(m)

    def read(self, mid: str) -> dict:
        folder, _, uid = mid.rpartition(":")
        m = self._imap()
        try:
            m.select(_quote(folder or "INBOX"), readonly=True)   # read-only: doesn't mark it as read
            typ, data = m.uid("fetch", uid.encode(), "(BODY.PEEK[])")
            raw = next((x[1] for x in data or [] if isinstance(x, tuple)), None)
            if raw is None:
                raise FileNotFoundError(f"no email {mid}")
        finally:
            _logout(m)
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        atts = [_decode(p.get_filename()) for p in msg.walk() if p.get_filename()]
        return {"id": mid, "from": _decode(msg.get("From")), "to": _decode(msg.get("To")), "cc": _decode(msg.get("Cc")),
                "subject": _decode(msg.get("Subject")), "date": msg.get("Date", ""), "message_id": msg.get("Message-ID", ""),
                "references": msg.get("References", ""), "attachments": atts, "body": body_text(msg)[:12000]}

    def save_attachment(self, mid: str, name: str, dest_dir: Path) -> Path:
        folder, _, uid = mid.rpartition(":")
        m = self._imap()
        try:
            m.select(_quote(folder or "INBOX"), readonly=True)
            typ, data = m.uid("fetch", uid.encode(), "(BODY.PEEK[])")
            raw = next((x[1] for x in data or [] if isinstance(x, tuple)), None)
        finally:
            _logout(m)
        msg = email.message_from_bytes(raw or b"", policy=email.policy.compat32)
        for p in msg.walk():
            fn = _decode(p.get_filename())
            if fn and (fn == name or not name):
                safe = re.sub(r"[^\w.\- ]", "_", Path(fn).name) or "attachment"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest, i = dest_dir / safe, 1
                while dest.exists():
                    dest = dest_dir / f"{Path(safe).stem}_{i}{Path(safe).suffix}"
                    i += 1
                dest.write_bytes(p.get_payload(decode=True) or b"")
                return dest
        raise FileNotFoundError(f"no attachment named {name!r} in that email")

    # ------------------------------------------------------------ writing
    def compose(self, to: str, subject: str, body: str, cc: str = "", reply_to_id: str = "") -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self.spec.get("display_name", ""), self.address))
        if reply_to_id:
            orig = self.read(reply_to_id)
            to = to or parseaddr(orig["from"])[1]
            subject = subject or ("Re: " + orig["subject"] if not orig["subject"].lower().startswith("re:") else orig["subject"])
            if orig["message_id"]:
                msg["In-Reply-To"] = orig["message_id"]
                msg["References"] = (orig["references"] + " " + orig["message_id"]).strip()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.address.rsplit("@", 1)[-1] or None)
        msg.set_content(body)
        return msg

    def save_draft(self, msg: EmailMessage) -> str:
        m = self._imap()
        try:
            drafts = self.folders(m).get("drafts")
            if not drafts:
                raise RuntimeError("couldn't find a Drafts folder on this account")
            typ, data = m.append(_quote(drafts), "(\\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if typ != "OK":
                raise RuntimeError(f"the mail server didn't accept the draft: {data}")
        finally:
            _logout(m)
        return drafts

    def send(self, msg: EmailMessage) -> None:
        if not self.password:
            raise RuntimeError("no password saved")
        recipients = [a for _, a in getaddresses([msg.get("To", ""), msg.get("Cc", "")]) if a]
        if self.smtp_port == 465:
            with smtplib.SMTP_SSL(self.smtp_host, 465, timeout=30) as s:
                s.login(self.address, self.password)
                s.send_message(msg, to_addrs=recipients)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as s:
                s.starttls()
                s.login(self.address, self.password)
                s.send_message(msg, to_addrs=recipients)
        if "gmail" not in self.smtp_host:   # Gmail files sent mail itself
            try:
                m = self._imap()
                try:
                    sent = self.folders(m).get("sent")
                    if sent:
                        m.append(_quote(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                finally:
                    _logout(m)
            except Exception:
                pass


def _logout(m) -> None:
    try:
        m.logout()
    except (imaplib.IMAP4.error, OSError, socket.error):
        pass
