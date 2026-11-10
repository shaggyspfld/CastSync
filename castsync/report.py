"""Run report: plain text + HTML, sent by SMTP and saved to the reports folder."""
from __future__ import annotations

import html
import logging
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Report:
    started: float = field(default_factory=time.time)
    published: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)   # informational only
    errors: list[str] = field(default_factory=list)     # source/auth problems
    held: list[dict] = field(default_factory=list)      # waiting for Libsyn quota reset
    waiting: int = 0                                    # left for the next run (per-run cap)
    quota_line: str = ""
    quota_reset: str = ""
    dest: str = "Libsyn"
    dry_run: bool = False

    @property
    def did_work(self) -> bool:
        # A skip because only a *date* matched is emailed once, so a wrong match
        # (e.g. two different shows on the same day) never goes unnoticed.
        return bool(self.published or self.failed or self.errors or self.date_matches
                    or self.held)

    @property
    def date_matches(self) -> list[dict]:
        return [s for s in self.skipped if "(same date" in s["reason"]]

    def subject(self, show: str) -> str:
        bits = []
        if self.published:
            bits.append(f"{len(self.published)} episode(s) {'would be ' if self.dry_run else ''}sent to {self.dest}")
        if self.failed:
            bits.append(f"{len(self.failed)} failed")
        if self.errors:
            bits.append(f"{len(self.errors)} error(s)")
        if self.date_matches:
            bits.append(f"{len(self.date_matches)} held back (date match)")
        if self.held:
            bits.append(f"{len(self.held)} waiting for {self.dest} monthly limit")
        prefix = "[castsync DRY RUN]" if self.dry_run else "[castsync]"
        return f"{prefix} {show}: " + (", ".join(bits) or "nothing to do")

    def text(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(self.started))
        lines = [f"castsync run at {when} (took {time.time() - self.started:.0f}s)", ""]
        if self.published:
            lines.append("DONE:" if not self.dry_run else "WOULD DO:")
            for p in self.published:
                lines += [f"  - {p['title']}",
                          f"      source: {p['source']}  ({p.get('when', '')})",
                          *([f"      audio:  {p['audio']}"] if p.get("audio") else []),
                          f"      mp3:    {p.get('mp3', '-')} {p.get('size', '')} {p.get('duration', '')}"
                          + (f" ({p['quality']})" if p.get("quality") else ""),
                          *( [f"      source: {p['original']} (kept)"] if p.get("original") else []),
                          f"      {self.dest.lower() + ':':<7} {p.get('result', '-')}"]
            lines.append("")
        if self.held:
            lines.append(f"WAITING FOR {self.dest.upper()} MONTHLY LIMIT TO RESET ({self.quota_reset}):")
            for h in self.held:
                lines.append(f"  - {h['title']} [{h['source']}] needs {h['mb']} MB")
            lines.append("    These upload automatically after the reset (newest first).")
            lines.append("")
        if self.waiting:
            lines.append(f"{self.waiting} more episode(s) queued for the next run "
                         "(max_episodes_per_run).")
            lines.append("")
        if self.failed:
            lines.append("FAILED (will retry next run):")
            for f in self.failed:
                lines += [f"  - {f['title']} [{f['source']}]", f"      {f['error']}"]
            lines.append("")
        if self.errors:
            lines.append("PROBLEMS:")
            lines += [f"  - {e}" for e in self.errors]
            lines.append("")
        if self.date_matches:
            lines.append(f"NOT UPLOADED - looks like it's already on {self.dest} under a different name:")
            for s in self.date_matches:
                lines.append(f"  - {s['title']}: {s['reason']}")
            lines.append("    If that's wrong, run: castsync allow <key>   (keys: castsync status)")
            lines.append("")
        if self.quota_line:
            lines.append(self.quota_line)
            lines.append("")
        if self.skipped:
            lines.append(f"Already on {self.dest} / skipped: {len(self.skipped)}")
            lines += [f"  - {s['title']} ({s['reason']})" for s in self.skipped[:20]]
        return "\n".join(lines)

    def html(self) -> str:
        return ("<pre style='font-family:ui-monospace,Menlo,monospace;font-size:13px'>"
                + html.escape(self.text()) + "</pre>")

    def save(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / time.strftime("report-%Y%m%d-%H%M%S.txt", time.localtime(self.started))
        p.write_text(self.text())
        return p


# Ready-made settings for common providers; anything set explicitly in the
# config (smtp_host, smtp_port, security) wins over the preset.
PROVIDERS = {
    "zoho":     {"smtp_host": "smtp.zoho.com",    "smtp_port": 465, "security": "ssl"},
    "zoho_pro": {"smtp_host": "smtppro.zoho.com", "smtp_port": 465, "security": "ssl"},
    "gmail":    {"smtp_host": "smtp.gmail.com",   "smtp_port": 587, "security": "starttls"},
    "custom":   {},
}
PORT_SECURITY = {465: "ssl", 587: "starttls", 2525: "starttls", 25: "starttls"}


def smtp_settings(cfg) -> dict:
    """Resolve provider preset + explicit values + sensible defaults."""
    provider = str(cfg.get("email.provider", "custom")).lower().replace("-", "_")
    if provider not in PROVIDERS:
        raise ValueError(f"email.provider '{provider}' unknown; use one of {', '.join(PROVIDERS)}")
    s = dict(PROVIDERS[provider])
    if cfg.get("email.smtp_port") and not cfg.get("email.security"):
        s.pop("security", None)  # port changed without security: infer it from the new port
    for key in ("smtp_host", "smtp_port", "security"):
        if cfg.get(f"email.{key}") not in (None, ""):
            s[key] = cfg.get(f"email.{key}")
    s["smtp_port"] = int(s.get("smtp_port") or 587)
    s["security"] = str(s.get("security") or PORT_SECURITY.get(s["smtp_port"], "starttls")).lower()
    if s["security"] in ("tls", "ssl/tls"):
        s["security"] = "ssl" if s["smtp_port"] == 465 else "starttls"
    if s["security"] not in ("ssl", "starttls", "none"):
        raise ValueError("email.security must be ssl, starttls or none")
    s["username"] = cfg.get("email.username") or ""
    s["password"] = cfg.get("email.password") or ""
    s["from"] = cfg.get("email.from") or s["username"]
    s["from_name"] = cfg.get("email.from_name") or "castsync"
    s["verify_tls"] = bool(cfg.get("email.verify_tls", True))
    s["provider"] = provider
    return s


def _hint(err: Exception, s: dict) -> str:
    text = str(err)
    if isinstance(err, smtplib.SMTPAuthenticationError) or " 535" in text or "535 " in text:
        h = ("The server rejected the username/password. Use your FULL email address as "
             "username. If the account has two-factor sign-in, create an app-specific "
             "password and use that.")
        if s["provider"].startswith("zoho"):
            h += (" Zoho: free/personal accounts use smtp.zoho.com, paid custom-domain "
                  "accounts use smtppro.zoho.com, and non-US accounts use their region's "
                  "host (smtp.zoho.eu, smtp.zoho.in, smtp.zoho.com.au ...).")
        return h
    if "relay" in text.lower() or isinstance(err, smtplib.SMTPSenderRefused):
        return ("The server won't send as this From address. Set email.from to the same "
                "address you log in with (or one of its aliases).")
    if isinstance(err, ssl.SSLError) or "wrong version number" in text.lower():
        return ("TLS mismatch: port 465 needs security: ssl, port 587 needs security: starttls.")
    if isinstance(err, smtplib.SMTPException):
        return ""  # server answered with some other error; the message says what
    if isinstance(err, (TimeoutError, ConnectionRefusedError, OSError)):
        return (f"Could not reach {s['smtp_host']}:{s['smtp_port']}. Check the host name, or "
                "try the other port (465 with ssl / 587 with starttls); some networks block one.")
    return ""


def send_email(cfg, report: Report, show: str) -> None:
    to = cfg.get("email.to")
    if not to:
        log.info("Email not configured; report saved locally only")
        return
    s = smtp_settings(cfg)
    if not s.get("smtp_host"):
        log.info("email.smtp_host / email.provider not set; report saved locally only")
        return
    from email.utils import formataddr

    msg = EmailMessage()
    msg["Subject"] = report.subject(show)
    msg["From"] = formataddr((s["from_name"], s["from"]))
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    msg.set_content(report.text())
    msg.add_alternative(report.html(), subtype="html")

    ctx = ssl.create_default_context()
    if not s["verify_tls"]:  # only for self-hosted servers with self-signed certificates
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        if s["security"] == "ssl":
            server = smtplib.SMTP_SSL(s["smtp_host"], s["smtp_port"], context=ctx, timeout=60)
        else:
            server = smtplib.SMTP(s["smtp_host"], s["smtp_port"], timeout=60)
            if s["security"] == "starttls":
                server.starttls(context=ctx)
        with server:
            if s["username"]:  # no username = unauthenticated relay (e.g. local postfix)
                server.login(s["username"], s["password"])
            server.send_message(msg)
    except Exception as e:
        hint = _hint(e, s)
        where = f"{s['smtp_host']}:{s['smtp_port']} ({s['security']})"
        raise RuntimeError(f"Email via {where} failed: {e}" + (f"\n  Hint: {hint}" if hint else "")) from e
