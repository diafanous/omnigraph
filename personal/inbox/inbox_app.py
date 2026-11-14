#!/usr/bin/env python3
"""Omnigraph Inbox: put your own files into the knowledge graph.

Two ways in, one result: every file becomes a `Document` node (metadata,
extracted text and the original bytes as a Blob) in your local graph.
Agents then read the Documents and build the ontology (personal/ONTOLOGY-AGENTS.md).

    1. App   - a page on http://127.0.0.1:8765 . Drop files, press Upload.
    2. Email - send files to a mailbox folder you choose (for example a Gmail
               label "Omnigraph"). The app checks that folder over IMAP.
               Connect the mailbox in the app's "Email connection" panel.

Commands (Python 3.11+, standard library only):

    python inbox_app.py serve                 # start the app (default)
    python inbox_app.py add FILE [FILE ...]   # add files from the command line
    python inbox_app.py check-email           # one email check (for a scheduler)
    python inbox_app.py status                # print config and recent items

Data lives outside the public fork, in --data-dir (or OMNIGRAPH_KNOWLEDGE):
    (default: a `knowledge` folder next to this clone)
    <data>/graph.omni              the graph
    <data>/ontology/schema.pg      the live schema (copied from personal/ontology on first run)
    <data>/inbox/files/            originals, never changed
    <data>/inbox/ledger.jsonl      one line per item received or refused
    <data>/inbox/config.json       settings (no password)
    <data>/inbox/email-password.bin  IMAP app password, encrypted with Windows DPAPI
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import email
import email.policy
import hashlib
import html
import imaplib
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from email.utils import parseaddr
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE.parent / "ontology"
BLOB_MAX = 20 * 1024 * 1024          # bytes stored in the graph (write limit is 32 MiB per load)
TEXT_MAX = 1_000_000                 # characters of extracted text kept in the graph
UPLOAD_MAX = 200 * 1024 * 1024       # largest HTTP request accepted
DEFAULT_PORT = 8765

DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "omnigraph": "",
    "email": {
        "enabled": False,
        "host": "imap.gmail.com",
        "port": 993,
        "username": "",
        "folder": "Omnigraph",
        "allowed_senders": [],
        "require_sender_auth": True,
        "poll_minutes": 10,
        "ingest_body": True,
    },
}


# --------------------------------------------------------------------------- secrets

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out))  # UI_FORBIDDEN
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def save_secret(path: Path, value: str) -> None:
    raw = value.encode("utf-8")
    if os.name == "nt":
        path.write_bytes(b"DPAPI1" + _dpapi(raw, True))
    else:  # fallback: file readable only by you
        path.write_bytes(b"PLAIN1" + raw)
        os.chmod(path, 0o600)


def load_secret(path: Path) -> str | None:
    env = os.environ.get("OMNIGRAPH_INBOX_IMAP_PASSWORD")
    if env:
        return env
    if not path.exists():
        return None
    data = path.read_bytes()
    if data.startswith(b"DPAPI1"):
        return _dpapi(data[6:], False).decode("utf-8")
    if data.startswith(b"PLAIN1"):
        return data[6:].decode("utf-8")
    return None


# --------------------------------------------------------------------------- text extraction

class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    p = _TextOnly()
    p.feed(markup)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts)).strip()


TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
                 ".toml", ".xml", ".pg", ".gq", ".py", ".js", ".ts", ".rs", ".cs", ".ghx",
                 ".ini", ".cfg", ".log", ".bib", ".tex", ".rst", ".srt", ".vtt"}


OFFICE_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/markdown", ".pg": "text/plain", ".gq": "text/plain",
}


def _office_text(data: bytes, members: str, tag: str) -> str:
    out = []
    word = re.compile(rf"<{tag}(?:\s[^>]*)?>([^<]*)</{tag}>")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted(n for n in z.namelist() if re.fullmatch(members, n)):
            xml = z.read(name).decode("utf-8", "replace")
            for para in re.split(r"</w:p>|</a:p>|</si>", xml):
                words = word.findall(para)
                if words:
                    out.append(html.unescape("".join(words)) + "\n")
            out.append("\n")
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def extract_text(filename: str, media_type: str, data: bytes) -> str | None:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix in (".html", ".htm") or media_type == "text/html":
            return html_to_text(data.decode("utf-8", "replace"))
        if suffix in TEXT_SUFFIXES or media_type.startswith("text/"):
            return data.decode("utf-8", "replace")
        if suffix == ".docx":
            return _office_text(data, r"word/document\.xml", "w:t")
        if suffix == ".pptx":
            return _office_text(data, r"ppt/slides/slide\d+\.xml", "a:t")
        if suffix == ".xlsx":
            return _office_text(data, r"xl/sharedStrings\.xml", "t")
        if suffix == ".pdf":
            try:
                import pypdf  # optional: pip install pypdf
            except ImportError:
                return None
            reader = pypdf.PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as exc:  # extraction is best effort; the original is always kept
        return f"[text extraction failed: {exc}]"
    return None


# --------------------------------------------------------------------------- core

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name or "file"
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .") or "file"
    return name[:150]


class Inbox:
    def __init__(self, data_dir: Path):
        self.data = data_dir.resolve()
        self.dir = self.data / "inbox"
        self.files = self.dir / "files"
        self.ledger_path = self.dir / "ledger.jsonl"
        self.config_path = self.dir / "config.json"
        self.secret_path = self.dir / "email-password.bin"
        self.graph = self.data / "graph.omni"
        self.schema = self.data / "ontology" / "schema.pg"
        self.lock = threading.Lock()
        self.status: dict = {"last_email_check": None, "last_email_error": None,
                             "last_email_result": None}
        self.files.mkdir(parents=True, exist_ok=True)
        self.config = self._load_config()

    # ---- config
    def _load_config(self) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if self.config_path.exists():
            stored = json.loads(self.config_path.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in stored.items() if k != "email"})
            cfg["email"].update(stored.get("email", {}))
        return cfg

    def save_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def omnigraph_exe(self) -> str:
        for cand in (self.config.get("omnigraph"), os.environ.get("OMNIGRAPH_BIN"),
                     shutil.which("omnigraph"),
                     str(Path.home() / ".omnigraph" / "bin" / ("omnigraph.exe" if os.name == "nt" else "omnigraph"))):
            if cand and Path(cand).exists():
                return cand
        raise RuntimeError("omnigraph CLI not found: set \"omnigraph\" in inbox/config.json")

    def og(self, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
        res = subprocess.run([self.omnigraph_exe(), *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout).strip()[-1500:])
        return res

    def ensure_graph(self) -> None:
        if not self.schema.exists():
            self.schema.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(TEMPLATE_DIR / "inbox.pg", self.schema)
            qdir = self.schema.parent / "queries"
            qdir.mkdir(exist_ok=True)
            for q in (TEMPLATE_DIR / "queries").glob("*.gq"):
                shutil.copy2(q, qdir / q.name)
        if not self.graph.exists():
            self.og("init", "--schema", str(self.schema), str(self.graph))

    # ---- ledger
    def ledger(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        rows = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _log(self, row: dict) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def known(self, sha: str) -> bool:
        return any(r.get("sha256") == sha and r.get("status") == "added" for r in self.ledger())

    # ---- ingest
    def ingest(self, filename: str, data: bytes, media_type: str | None = None, *, channel: str,
               sender: str | None = None, subject: str | None = None, note: str | None = None) -> dict:
        filename = safe_name(filename)
        if not media_type or media_type == "application/octet-stream":
            media_type = (OFFICE_TYPES.get(Path(filename).suffix.lower())
                          or mimetypes.guess_type(filename)[0] or "application/octet-stream")
        sha = hashlib.sha256(data).hexdigest()
        slug = "doc-" + sha[:16]
        received = now_iso()
        row = {"at": received, "slug": slug, "sha256": sha, "filename": filename,
               "media_type": media_type, "size": len(data), "channel": channel,
               "sender": sender, "subject": subject}
        with self.lock:
            if self.known(sha):
                row["status"] = "duplicate"
                self._log(row)
                return row
            folder = self.files / slug
            folder.mkdir(parents=True, exist_ok=True)
            original = folder / filename
            if not original.exists():
                original.write_bytes(data)
            text = extract_text(filename, media_type, data)
            if text is not None:
                text = text[:TEXT_MAX]
            title = subject if (subject and channel == "email" and filename == "message.txt") \
                else Path(filename).stem.replace("_", " ")
            doc = {"type": "Document", "data": {
                "slug": slug, "title": title[:300], "filename": filename,
                "media_type": media_type, "size_bytes": len(data), "sha256": sha,
                "channel": channel, "sender": sender, "subject": subject,
                "received_at": received, "note": note, "text": text,
                "content": ("base64:" + base64.b64encode(data).decode("ascii"))
                if len(data) <= BLOB_MAX else None}}
            try:
                self.ensure_graph()
                with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                                 encoding="utf-8") as tmp:
                    tmp.write(json.dumps(doc, ensure_ascii=False) + "\n")
                try:
                    self.og("load", "--data", tmp.name, "--mode", "merge", "--target", str(self.graph))
                finally:
                    os.unlink(tmp.name)
            except Exception as exc:
                row.update(status="error", error=str(exc)[-600:])
                self._log(row)
                return row
            row.update(status="added", original=str(original.relative_to(self.data)),
                       blob_stored=len(data) <= BLOB_MAX, text_chars=len(text or ""))
            if note:
                row["note"] = note
            self._log(row)
            return row

    # ---- email
    def _authenticated(self, msg, sender: str) -> tuple[bool, str]:
        results = " ".join(str(h) for h in (msg.get_all("Authentication-Results") or [])).lower()
        domain = sender.rsplit("@", 1)[-1]
        if not results:
            # Mail you send to your own mailbox can carry no Authentication-Results.
            own = sender == self.config["email"]["username"].strip().lower()
            return own, "own mailbox, no auth header" if own else "no Authentication-Results header"
        if "dmarc=pass" in results:
            return True, "dmarc=pass"
        if "dkim=pass" in results and (f"header.i=@{domain}" in results or f"header.d={domain}" in results):
            return True, "dkim=pass (aligned)"
        return False, "sender authentication did not pass"

    def process_message(self, raw: bytes) -> list[dict]:
        """Turn one email into Documents. Testable without a mail server."""
        cfg = self.config["email"]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        sender = parseaddr(str(msg.get("From", "")))[1].strip().lower()
        subject = str(msg.get("Subject", "") or "").strip() or None
        allowed = {a.strip().lower() for a in cfg["allowed_senders"] if a.strip()}
        base = {"at": now_iso(), "channel": "email", "sender": sender, "subject": subject,
                "message_id": str(msg.get("Message-ID", ""))}
        if sender not in allowed:
            row = {**base, "status": "refused", "reason": "sender not in allowed list"}
            self._log(row)
            return [row]
        if cfg.get("require_sender_auth", True):
            ok, why = self._authenticated(msg, sender)
            if not ok:
                row = {**base, "status": "refused", "reason": why}
                self._log(row)
                return [row]
        rows = []
        for part in msg.iter_attachments():
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            rows.append(self.ingest(part.get_filename() or "attachment.bin", payload,
                                    part.get_content_type(), channel="email", sender=sender,
                                    subject=subject))
        if cfg.get("ingest_body", True):
            body = msg.get_body(preferencelist=("plain", "html"))
            if body is not None:
                text = body.get_content()
                if body.get_content_type() == "text/html":
                    text = html_to_text(text)
                text = text.strip()
                if text:
                    header = f"From: {sender}\nSubject: {subject or ''}\nDate: {msg.get('Date', '')}\n\n"
                    rows.append(self.ingest("message.txt", (header + text).encode("utf-8"),
                                            "text/plain", channel="email", sender=sender,
                                            subject=subject))
        return rows

    def _imap(self) -> imaplib.IMAP4_SSL:
        cfg = self.config["email"]
        password = load_secret(self.secret_path)
        if not cfg["username"] or not password:
            raise RuntimeError("email not connected: add the username and app password")
        conn = imaplib.IMAP4_SSL(cfg["host"], int(cfg["port"]), timeout=60)
        conn.login(cfg["username"], password)
        return conn

    @staticmethod
    def _quote_folder(folder: str) -> str:
        return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def test_email(self) -> str:
        conn = self._imap()
        try:
            typ, data = conn.select(self._quote_folder(self.config["email"]["folder"]), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"folder '{self.config['email']['folder']}' not found")
            typ, unseen = conn.search(None, "UNSEEN")
            n = len(unseen[0].split()) if typ == "OK" and unseen and unseen[0] else 0
            return f"connected; folder has {int(data[0])} messages, {n} unread"
        finally:
            conn.logout()

    def check_email(self) -> dict:
        cfg = self.config["email"]
        if not [a for a in cfg["allowed_senders"] if a.strip()]:
            raise RuntimeError("add at least one allowed sender first")
        conn = self._imap()
        summary = {"messages": 0, "added": 0, "duplicate": 0, "refused": 0, "error": 0}
        try:
            typ, _ = conn.select(self._quote_folder(cfg["folder"]))
            if typ != "OK":
                raise RuntimeError(f"folder '{cfg['folder']}' not found")
            typ, data = conn.uid("search", None, "UNSEEN")
            for uid in (data[0].split() if typ == "OK" and data and data[0] else []):
                typ, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw is None:
                    continue
                summary["messages"] += 1
                rows = self.process_message(raw)
                for r in rows:
                    summary[r["status"]] = summary.get(r["status"], 0) + 1
                if all(r["status"] != "error" for r in rows):
                    conn.uid("store", uid, "+FLAGS", "(\\Seen)")  # retry errors next time
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self.status.update(last_email_check=now_iso(), last_email_result=summary,
                           last_email_error=None)
        return summary

    def poll_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            cfg = self.config["email"]
            if cfg.get("enabled") and cfg.get("username") and load_secret(self.secret_path):
                try:
                    self.check_email()
                except Exception as exc:
                    self.status.update(last_email_check=now_iso(), last_email_error=str(exc)[:500])
            stop.wait(max(1, int(cfg.get("poll_minutes", 10))) * 60)


# --------------------------------------------------------------------------- web app

PAGE_CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a66;--line:#e3e1dc;--acc:#2f5d50;--bad:#a33;--ok:#2f6d3a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe7;--mute:#9b9a95;--line:#33322f;--acc:#7fb7a4;--bad:#e07a7a;--ok:#8fcf98}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
.sub{color:var(--mute);margin:0 0 20px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px}
.drop{border:2px dashed var(--line);border-radius:10px;padding:28px 12px;text-align:center;position:relative;color:var(--mute)}
.drop.over{border-color:var(--acc);color:var(--ink)}.drop input{position:absolute;inset:0;opacity:0;cursor:pointer}
label{display:block;font-size:13px;color:var(--mute);margin:10px 0 4px}input[type=text],input[type=number],input[type=password],textarea{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:7px;background:transparent;color:var(--ink);font:inherit}
textarea{min-height:56px}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.chk{display:flex;gap:8px;align-items:center;margin-top:10px;color:var(--ink);font-size:14px}
button{border:1px solid var(--acc);background:var(--acc);color:#fff;padding:8px 14px;border-radius:7px;font:inherit;cursor:pointer}button.ghost{background:transparent;color:var(--acc)}
.msg{padding:10px 12px;border-radius:8px;margin-bottom:16px;border:1px solid var(--line)}.msg.bad{border-color:var(--bad);color:var(--bad)}.msg.ok{border-color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:500}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px}.added{color:var(--ok)}.error,.refused{color:var(--bad)}
code{font-size:12px}.full{grid-column:1/-1}.small{font-size:13px;color:var(--mute)}
"""


def render_page(inbox: Inbox, token: str, message: str = "", bad: bool = False) -> str:
    e = html.escape
    cfg = inbox.config["email"]
    has_pw = load_secret(inbox.secret_path) is not None
    rows = list(reversed(inbox.ledger()))[:40]
    st = inbox.status
    email_state = "off"
    if cfg["enabled"] and cfg["username"] and has_pw:
        email_state = f"on, every {cfg['poll_minutes']} min"
    last = ""
    if st["last_email_check"]:
        last = f"Last check {e(st['last_email_check'])} UTC: " + (
            f"<span class='error'>{e(st['last_email_error'])}</span>" if st["last_email_error"]
            else e(json.dumps(st["last_email_result"])))
    msg_html = f"<div class='msg {'bad' if bad else 'ok'}'>{message}</div>" if message else ""
    table = "".join(
        f"<tr><td>{e(r.get('at', '')[:16].replace('T', ' '))}</td>"
        f"<td>{e(r.get('filename') or r.get('subject') or '')}</td>"
        f"<td>{e(r.get('channel', ''))}{(' · ' + e(r['sender'])) if r.get('sender') else ''}</td>"
        f"<td><span class='pill {e(r.get('status', ''))}'>{e(r.get('status', ''))}</span>"
        f"{('<br><span class=small>' + e(r.get('error') or r.get('reason') or '')[:200] + '</span>') if r.get('error') or r.get('reason') else ''}</td>"
        f"<td><code>{e(r.get('slug', ''))}</code></td></tr>" for r in rows) or \
        "<tr><td colspan=5 class=small>Nothing yet.</td></tr>"
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Omnigraph Inbox</title>
<style>{PAGE_CSS}</style></head><body><main>
<h1>Omnigraph Inbox</h1>
<p class=sub>Files you add here become <code>Document</code> nodes in <code>{e(str(inbox.graph))}</code>. Agents build the ontology from them.</p>
{msg_html}
<div class=grid>
<section class=card>
<h2>Upload files</h2>
<form method=post action=/upload enctype=multipart/form-data>
<input type=hidden name=csrf value="{token}">
<div class=drop id=drop>Drop files here or click to choose<input type=file name=files multiple required id=files></div>
<div class=small id=picked></div>
<label for=note>Note for the agents (optional)</label>
<input type=text name=note id=note placeholder="e.g. precedent for the facade study">
<div class=row><button type=submit>Upload</button></div>
</form>
<p class=small>Text is extracted from txt, md, csv, json, html, docx, pptx, xlsx{', pdf' if _has_pypdf() else ' (pdf after <code>pip install pypdf</code>)'}. Files up to {BLOB_MAX // 1048576} MB are also stored in the graph; every original is kept in <code>inbox/files</code>.</p>
</section>
<section class=card>
<h2>Email connection</h2>
<p class=small>Status: <b>{email_state}</b>. {last}</p>
<form method=post action=/email>
<input type=hidden name=csrf value="{token}">
<div class=grid style="gap:0 12px"><div><label>IMAP server</label><input type=text name=host value="{e(cfg['host'])}"></div>
<div><label>Port</label><input type=number name=port value="{e(str(cfg['port']))}"></div></div>
<label>Mailbox address (username)</label><input type=text name=username value="{e(cfg['username'])}" autocomplete=username>
<label>App password {'(saved — leave empty to keep)' if has_pw else ''}</label>
<input type=password name=password autocomplete=new-password placeholder="{'••••••••' if has_pw else 'Gmail: create an app password'}">
<label>Folder or Gmail label to watch</label><input type=text name=folder value="{e(cfg['folder'])}">
<label>Allowed senders (one per line). Mail from anyone else is refused.</label>
<textarea name=allowed>{e(chr(10).join(cfg['allowed_senders']))}</textarea>
<div class=grid style="gap:0 12px"><div><label>Check every (minutes)</label><input type=number min=1 name=poll value="{e(str(cfg['poll_minutes']))}"></div><div></div></div>
<label class=chk><input type=checkbox name=enabled {'checked' if cfg['enabled'] else ''}> Check automatically while this app runs</label>
<label class=chk><input type=checkbox name=auth {'checked' if cfg['require_sender_auth'] else ''}> Require DKIM/DMARC pass (blocks spoofed senders)</label>
<label class=chk><input type=checkbox name=body {'checked' if cfg['ingest_body'] else ''}> Also add the email text as a Document</label>
<div class=row><button name=action value=save>Save</button><button class=ghost name=action value=test>Test connection</button><button class=ghost name=action value=check>Check now</button>{'<button class=ghost name=action value=forget>Remove password</button>' if has_pw else ''}</div>
</form>
</section>
<section class="card full">
<h2>Recent items</h2>
<table><tr><th>Time (UTC)</th><th>File</th><th>Channel</th><th>Status</th><th>Graph id</th></tr>{table}</table>
</section>
</div></main>
<script>
const d=document.getElementById('drop'),f=document.getElementById('files'),p=document.getElementById('picked');
['dragenter','dragover'].forEach(t=>d.addEventListener(t,()=>d.classList.add('over')));
['dragleave','drop'].forEach(t=>d.addEventListener(t,()=>d.classList.remove('over')));
f.addEventListener('change',()=>{{p.textContent=[...f.files].map(x=>x.name).join(', ');}});
</script></body></html>"""


def _has_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def make_handler(inbox: Inbox, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "OmnigraphInbox/1"

        def log_message(self, fmt, *args):  # quiet console
            pass

        def _guard(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:  # DNS-rebinding defence
                self.send_error(421, "wrong host")
                return False
            return True

        def _send(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._guard():
                return
            if urlparse(self.path).path != "/":
                self.send_error(404)
                return
            self._send(render_page(inbox, token))

        def _post_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc not in allowed_hosts:
                self.send_error(403, "cross-site request refused")
                return False
            return True

        def do_POST(self):
            if not self._guard() or not self._post_ok():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > UPLOAD_MAX:
                self._send(render_page(inbox, token, f"Request too large (max {UPLOAD_MAX // 1048576} MB).", True), 413)
                return
            body = self.rfile.read(length)
            path = urlparse(self.path).path
            try:
                if path == "/upload":
                    msg, bad = self._upload(body)
                elif path == "/email":
                    msg, bad = self._email(body)
                else:
                    self.send_error(404)
                    return
            except PermissionError:
                self.send_error(403, "missing or wrong form token; reload the page")
                return
            except Exception as exc:
                msg, bad = html.escape(str(exc)[:600]), True
            self._send(render_page(inbox, token, msg, bad))

        def _upload(self, body: bytes) -> tuple[str, bool]:
            ctype = self.headers.get("Content-Type", "")
            msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
                                           policy=email.policy.HTTP)
            fields, files = {}, []
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                fname = part.get_filename()
                if fname:
                    files.append((fname, part.get_payload(decode=True) or b"", part.get_content_type()))
                elif name:
                    fields[name] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            if not secrets.compare_digest(fields.get("csrf", ""), token):
                raise PermissionError
            note = fields.get("note", "").strip() or None
            if not files:
                return "Choose at least one file.", True
            results = [inbox.ingest(n, d, t, channel="app", note=note) for n, d, t in files]
            parts = [f"{html.escape(r['filename'])}: <b>{r['status']}</b>" +
                     (f" — {html.escape(r.get('error', '')[:300])}" if r["status"] == "error" else "")
                     for r in results]
            return "<br>".join(parts), any(r["status"] == "error" for r in results)

        def _email(self, body: bytes) -> tuple[str, bool]:
            form = {k: v[-1] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
            if not secrets.compare_digest(form.get("csrf", ""), token):
                raise PermissionError
            action = form.get("action", "save")
            cfg = inbox.config["email"]
            if action == "forget":
                inbox.secret_path.unlink(missing_ok=True)
                return "Saved password removed.", False
            cfg.update(host=form.get("host", cfg["host"]).strip(),
                       port=int(form.get("port") or 993),
                       username=form.get("username", "").strip(),
                       folder=form.get("folder", "").strip() or "INBOX",
                       allowed_senders=[a.strip() for a in form.get("allowed", "").splitlines() if a.strip()],
                       poll_minutes=max(1, int(form.get("poll") or 10)),
                       enabled="enabled" in form, require_sender_auth="auth" in form,
                       ingest_body="body" in form)
            if form.get("password"):
                save_secret(inbox.secret_path, form["password"])
            inbox.save_config()
            if action == "test":
                return "Saved. " + html.escape(inbox.test_email()), False
            if action == "check":
                return "Checked: " + html.escape(json.dumps(inbox.check_email())), False
            return "Email settings saved.", False

    return Handler


def serve(inbox: Inbox, port: int, open_browser: bool) -> None:
    token = secrets.token_urlsafe(32)
    stop = threading.Event()
    threading.Thread(target=inbox.poll_forever, args=(stop,), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(inbox, token, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"Omnigraph Inbox running at {url}  (data: {inbox.data})  Ctrl+C to stop")
    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


def default_data_dir() -> Path:
    """OMNIGRAPH_KNOWLEDGE, else a `knowledge` folder next to this clone (never inside it)."""
    env = os.environ.get("OMNIGRAPH_KNOWLEDGE")
    return Path(env) if env else HERE.parents[2] / "knowledge"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=default_data_dir())
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")
    a = sub.add_parser("add")
    a.add_argument("files", nargs="+", type=Path)
    a.add_argument("--note")
    sub.add_parser("check-email")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    inbox = Inbox(args.data_dir)
    cmd = args.cmd or "serve"
    if cmd == "serve":
        serve(inbox, getattr(args, "port", None) or int(inbox.config.get("port", DEFAULT_PORT)),
              not getattr(args, "no_browser", False))
    elif cmd == "add":
        bad = 0
        for f in args.files:
            r = inbox.ingest(f.name, f.read_bytes(), channel="app", note=args.note)
            print(f"{r['status']:9} {r['slug']}  {f}" + (f"\n          {r.get('error')}" if r["status"] == "error" else ""))
            bad += r["status"] == "error"
        return 1 if bad else 0
    elif cmd == "check-email":
        print(json.dumps(inbox.check_email()))
    elif cmd == "status":
        cfg = dict(inbox.config)
        print(json.dumps({"data": str(inbox.data), "graph": str(inbox.graph), "config": cfg,
                          "password_saved": load_secret(inbox.secret_path) is not None,
                          "recent": inbox.ledger()[-10:]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
