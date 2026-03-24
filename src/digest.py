#!/usr/bin/env python3
"""
Goodreads Weekly Digest
-----------------------
Fetches the user's Goodreads RSS feed, groups books read in the last 7 days
into configured categories, and sends an HTML digest email via Gmail SMTP.

Required environment variables:
    GOODREADS_RSS_URL   – e.g. https://www.goodreads.com/user/updates_rss/12345
    GMAIL_USER          – sender Gmail address
    GMAIL_APP_PASSWORD  – Gmail App Password (not your account password)
    TO_EMAIL            – recipient address (comma-separated for multiple)
"""

import os
import sys
import smtplib
import textwrap
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import yaml


# ── Configuration ──────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_FILE = ROOT / "categories.yml"
LOOKBACK_DAYS = 7


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_env(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        print(f"[ERROR] Missing required environment variable: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def load_categories() -> list[dict]:
    with open(CATEGORIES_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("categories", [])


def parse_entry_date(entry) -> datetime | None:
    """Return a timezone-aware UTC datetime from a feedparser entry, or None."""
    for attr in ("updated_parsed", "published_parsed"):
        t = getattr(entry, attr, None)
        if t:
            import time as _time
            ts = _time.mktime(t)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


def entry_shelves(entry) -> set[str]:
    """Extract Goodreads shelf tags from a feedparser entry."""
    shelves: set[str] = set()
    # feedparser exposes tags as entry.tags list of dicts with 'term' key
    for tag in getattr(entry, "tags", []):
        term = tag.get("term", "")
        if term:
            shelves.add(term.lower().strip())
    # Also check user_shelves string field if present
    user_shelves = getattr(entry, "user_shelves", "")
    for s in user_shelves.split(","):
        s = s.strip().lower()
        if s:
            shelves.add(s)
    return shelves


def star_rating(entry) -> str:
    """Return a star string for the book's rating (e.g. '★★★★☆')."""
    try:
        rating = int(getattr(entry, "user_rating", 0))
    except (ValueError, TypeError):
        rating = 0
    if rating == 0:
        return "Derecelendirme yok"
    return "★" * rating + "☆" * (5 - rating)


def build_book_html(entry) -> str:
    title = entry.get("title", "Bilinmeyen Kitap")
    link = entry.get("link", "#")
    author = getattr(entry, "author", "Bilinmeyen Yazar")
    rating = star_rating(entry)
    summary = getattr(entry, "summary", "")
    # Strip HTML from summary
    import re
    summary_text = re.sub(r"<[^>]+>", "", summary).strip()
    if len(summary_text) > 300:
        summary_text = summary_text[:297] + "…"

    return f"""
        <div style="margin-bottom:20px; padding:16px; border-left:4px solid #7b6a5a;
                    background:#faf8f5; border-radius:4px;">
          <h3 style="margin:0 0 4px; font-size:16px;">
            <a href="{link}" style="color:#3a2e28; text-decoration:none;">{title}</a>
          </h3>
          <p style="margin:0 0 6px; color:#6b5e52; font-size:13px;">{author}</p>
          <p style="margin:0 0 8px; color:#c8954a; font-size:14px;">{rating}</p>
          {f'<p style="margin:0; color:#555; font-size:13px; line-height:1.5;">{summary_text}</p>' if summary_text else ''}
        </div>"""


def build_html(sections: dict[str, list], week_str: str) -> str:
    section_html = ""
    for category_name, entries in sections.items():
        if not entries:
            continue
        books_html = "".join(build_book_html(e) for e in entries)
        section_html += f"""
      <section style="margin-bottom:36px;">
        <h2 style="font-size:18px; color:#7b6a5a; border-bottom:2px solid #e8e0d8;
                   padding-bottom:8px; margin-bottom:16px;">{category_name}</h2>
        {books_html}
      </section>"""

    if not section_html:
        section_html = """
      <p style="color:#888; font-style:italic;">
        Bu hafta RSS feed'inde aktif kategorilere ait kitap bulunamadı.
      </p>"""

    return f"""<!DOCTYPE html>
<html lang="tr">
<head><meta charset="UTF-8"><title>Goodreads Haftalık Özet</title></head>
<body style="margin:0; padding:0; background:#f0ebe4; font-family:Georgia,serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ebe4; padding:32px 16px;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0"
             style="background:#fff; border-radius:8px; overflow:hidden;
                    box-shadow:0 2px 8px rgba(0,0,0,.08);">
        <!-- Header -->
        <tr>
          <td style="background:#3a2e28; padding:28px 32px;">
            <h1 style="margin:0; color:#f5f0ea; font-size:22px; font-weight:normal;">
              📚 Goodreads Haftalık Özet
            </h1>
            <p style="margin:6px 0 0; color:#c8b49a; font-size:13px;">{week_str}</p>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:32px;">
            {section_html}
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="background:#f5f0ea; padding:16px 32px; text-align:center;
                     color:#999; font-size:11px;">
            Goodreads RSS feed'inden otomatik olarak oluşturuldu •
            <a href="https://github.com/nevzatalkan/new-books" style="color:#7b6a5a;">
              nevzatalkan/new-books
            </a>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    rss_url = load_env("GOODREADS_RSS_URL")
    gmail_user = load_env("GMAIL_USER")
    gmail_password = load_env("GMAIL_APP_PASSWORD")
    to_email = load_env("TO_EMAIL")

    categories = load_categories()
    active_categories = [c for c in categories if c.get("active")]

    if not active_categories:
        print("[WARN] categories.yml içinde aktif kategori bulunamadı.")

    print(f"[INFO] RSS feed alınıyor: {rss_url}")
    feed = feedparser.parse(rss_url)

    if feed.bozo and not feed.entries:
        print(f"[ERROR] RSS parse hatası: {feed.bozo_exception}", file=sys.stderr)
        sys.exit(1)

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    recent_entries = []
    for entry in feed.entries:
        pub = parse_entry_date(entry)
        if pub is None or pub >= cutoff:
            recent_entries.append(entry)

    print(f"[INFO] Son {LOOKBACK_DAYS} günde {len(recent_entries)} kitap bulundu.")

    # Build sections: {category_name: [entries]}
    sections: dict[str, list] = {}
    uncategorized: list = []

    for entry in recent_entries:
        shelves = entry_shelves(entry)
        matched = False
        for cat in active_categories:
            cat_shelves = {s.lower() for s in cat.get("shelves", [])}
            if shelves & cat_shelves:  # intersection
                sections.setdefault(cat["name"], []).append(entry)
                matched = True
        if not matched:
            uncategorized.append(entry)

    if uncategorized:
        sections["Diğer"] = uncategorized

    week_str = (
        f"{cutoff.strftime('%-d %B %Y')} – {datetime.now(tz=timezone.utc).strftime('%-d %B %Y')}"
    )

    html_body = build_html(sections, week_str)

    # Build email
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📚 Goodreads Haftalık Özet | {week_str}"
    msg["From"] = gmail_user
    msg["To"] = to_email

    total_books = sum(len(v) for v in sections.values())
    plain_text = textwrap.dedent(f"""
        Goodreads Haftalık Özet — {week_str}
        =====================================
        Bu hafta {total_books} kitap bulundu.

        HTML görünümü için e-posta istemcinizde "HTML olarak görüntüle" seçeneğini kullanın.
        Kaynak: {rss_url}
    """).strip()

    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [r.strip() for r in to_email.split(",")]

    print(f"[INFO] E-posta gönderiliyor → {', '.join(recipients)}")
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, recipients, msg.as_string())

    print(f"[OK] Digest başarıyla gönderildi. ({total_books} kitap, "
          f"{len(sections)} bölüm)")


if __name__ == "__main__":
    main()
