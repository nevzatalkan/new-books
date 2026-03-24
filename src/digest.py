#!/usr/bin/env python3
"""
Daily New Books Digest
----------------------
Fetches recently published English books from Google Books API for each
configured category and creates a GitHub Issue as the daily digest.
The issue is assigned to the repo owner so GitHub sends an email notification.
Book descriptions are enriched via Open Library (fallback) and Groq AI (Turkish summary).

Required secrets:
    GITHUB_TOKEN  – built-in Actions token
    GROQ_API_KEY  – optional; skips AI enrichment if missing
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_FILE = ROOT / "categories.yml"
BOOKS_PER_CATEGORY = 5

OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPENLIBRARY_WORKS_URL = "https://openlibrary.org"
GROQ_MODEL = "llama-3.1-8b-instant"
OL_HEADERS = {"User-Agent": "new-books-digest/1.0 (github.com/nevzatalkan/new-books)"}


# ── Google Books ───────────────────────────────────────────────────────────────

def _fetch(query: str) -> list[dict]:
    """Single Google Books API call, returns raw book dicts."""
    params = {
        "q":           query,
        "orderBy":     "newest",
        "maxResults":  40,
        "printType":   "books",
        "langRestrict": "en",
    }
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read()).get("items", [])
    except Exception as exc:
        print(f"[WARN] API error for '{query}': {exc}")
        return []


def google_books_search(queries: list[str], want: int) -> list[dict]:
    """Search multiple queries, deduplicate by title, return up to want books."""
    seen: set[str] = set()
    books: list[dict] = []

    for query in queries:
        for item in _fetch(query):
            info = item.get("volumeInfo", {})
            title = info.get("title", "").strip()
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())

            desc = info.get("description", "")
            if len(desc) > 500:
                desc = desc[:497] + "..."

            books.append({
                "title":         title,
                "authors":       ", ".join(info.get("authors", ["Unknown Author"])),
                "published":     info.get("publishedDate", ""),
                "description":   desc,
                "link":          info.get("infoLink", ""),
                "rating":        info.get("averageRating", 0),
                "ratings_count": info.get("ratingsCount", 0),
            })

        if len(books) >= want:
            break

    return books[:want]


# ── Open Library ───────────────────────────────────────────────────────────────

def fetch_openlibrary_description(title: str, author: str) -> str:
    """Open Library API'den kitap açıklaması çeker. Bulamazsa boş string döner."""
    try:
        params = {"title": title, "author": author, "limit": 1, "fields": "key,title"}
        resp = requests.get(OPENLIBRARY_SEARCH_URL, params=params,
                            headers=OL_HEADERS, timeout=10)
        resp.raise_for_status()
        docs = resp.json().get("docs", [])
        if not docs:
            return ""

        work_key = docs[0].get("key", "")
        if not work_key:
            return ""

        work_resp = requests.get(f"{OPENLIBRARY_WORKS_URL}{work_key}.json",
                                 headers=OL_HEADERS, timeout=10)
        work_resp.raise_for_status()
        work_data = work_resp.json()

        desc = work_data.get("description", "")
        if isinstance(desc, dict):
            desc = desc.get("value", "")
        return (desc or "").strip()[:800]
    except Exception as exc:
        print(f"[WARN] Open Library hatası ({title}): {exc}")
        return ""


# ── Groq AI ────────────────────────────────────────────────────────────────────

def enhance_with_groq(title: str, author: str, google_desc: str,
                      ol_desc: str, groq_client) -> dict | None:
    """
    Groq ile kitap bilgilerini zenginleştirir.
    {"topics": ["...", "..."], "audience": "..."} formatında dict döner.
    Hata durumunda None döner.
    """
    source_text = ""
    if google_desc:
        source_text += f"Google Books açıklaması: {google_desc}\n"
    if ol_desc:
        source_text += f"Open Library açıklaması: {ol_desc}\n"
    if not source_text:
        source_text = "Bu kitap hakkında kaynak bilgi bulunmuyor."

    prompt = (
        f"Kitap: \"{title}\" — Yazar: {author}\n\n"
        f"{source_text}\n"
        "Yukarıdaki bilgileri kullanarak aşağıdaki JSON formatında Türkçe yanıt ver:\n"
        "{\n"
        "  \"topics\": [\"konu 1\", \"konu 2\", \"konu 3\"],\n"
        "  \"audience\": \"Bu kitap ... için idealdir.\"\n"
        "}\n\n"
        "- topics: kitabın 2-4 ana konu başlığı (kısa, madde madde)\n"
        "- audience: bu kitabın kime hitap ettiği (1 cümle)\n"
        "Sadece JSON yaz, başka hiçbir şey ekleme."
    )

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        parsed = json.loads(raw)
        if "topics" in parsed and "audience" in parsed:
            return parsed
        return None
    except Exception as exc:
        print(f"[WARN] Groq hatası ({title}): {exc}")
        return None


def enrich_book(book: dict, groq_client) -> dict:
    """
    Bir kitabın bilgilerini zenginleştirir.
    Öncelik: Groq (topics+audience) → Open Library fallback → Google Books
    """
    title = book["title"]
    author = book["authors"]
    google_desc = book["description"]

    # 1) Open Library: Google açıklaması kısa/yoksa çek
    ol_desc = ""
    if not google_desc or len(google_desc) < 100:
        ol_desc = fetch_openlibrary_description(title, author)
        if ol_desc:
            print(f"[INFO]   Open Library açıklaması bulundu: {title}")

    # 2) Groq ile zenginleştir (topics + audience)
    if groq_client:
        enriched = enhance_with_groq(title, author, google_desc, ol_desc, groq_client)
        if enriched:
            return {**book, "topics": enriched["topics"],
                    "audience": enriched["audience"],
                    "description_source": "groq"}

    # 3) Open Library fallback
    if ol_desc:
        truncated = ol_desc[:400] + ("…" if len(ol_desc) > 400 else "")
        return {**book, "description": truncated, "description_source": "openlibrary"}

    # 4) Google Books orijinal açıklama
    return {**book, "description_source": "google"}


# ── Formatting ─────────────────────────────────────────────────────────────────

def format_book(book: dict) -> str:
    rating_str = ""
    if book["rating"]:
        rating_str = f" · ⭐ {book['rating']}/5 ({book['ratings_count']} ratings)"

    source_badge = {
        "groq": " 🤖",
        "openlibrary": " 📖",
        "google": "",
    }.get(book.get("description_source", "google"), "")

    lines = [f"### [{book['title']}]({book['link']})"]
    lines.append(
        f"**{book['authors']}**"
        + (f" · {book['published']}" if book["published"] else "")
        + rating_str
        + source_badge
    )

    # Groq: konu başlıkları + kimler için
    if book.get("topics"):
        lines.append("")
        for topic in book["topics"]:
            lines.append(f"- {topic}")
    if book.get("audience"):
        lines.append("")
        lines.append(f"**Kimler için?** {book['audience']}")

    # Fallback: düz açıklama (OL veya Google)
    elif book.get("description"):
        lines.append("")
        lines.append(book["description"])

    lines.append("")
    return "\n".join(lines)


def build_issue_body(sections: dict[str, list], date_str: str) -> str:
    total = sum(len(v) for v in sections.values())
    body = (
        f"# \U0001f4da Daily New Books Digest\n"
        f"**{date_str}**\n\n"
        f"> {total} new English books across {len(sections)} categories\n\n"
        "---\n\n"
    )
    for name, books in sections.items():
        if not books:
            continue
        body += f"## {name}\n\n"
        for book in books:
            body += format_book(book)
        body += "---\n\n"
    body += (
        "*Generated by [nevzatalkan/new-books](https://github.com/nevzatalkan/new-books)*  \n"
        "*🤖 = Groq AI özeti · 📖 = Open Library*"
    )
    return body


# ── GitHub Issue ───────────────────────────────────────────────────────────────

def create_github_issue(title: str, body: str, owner: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo  = os.environ.get("GITHUB_REPOSITORY", "").strip()

    if not token or not repo:
        print("[ERROR] GITHUB_TOKEN or GITHUB_REPOSITORY is not set.", file=sys.stderr)
        sys.exit(1)

    payload = json.dumps({
        "title":     title,
        "body":      body,
        "assignees": [owner],
    }).encode()

    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=payload,
        headers={
            "Authorization":        f"Bearer {token}",
            "Accept":               "application/vnd.github+json",
            "Content-Type":         "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            issue = json.loads(resp.read())
            print(f"[OK] Issue created: {issue['html_url']}")
    except urllib.error.HTTPError as exc:
        print(f"[ERROR] GitHub API: {exc.read().decode()}", file=sys.stderr)
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    with open(CATEGORIES_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    categories = [c for c in data.get("categories", []) if c.get("active")]
    if not categories:
        print("[WARN] No active categories in categories.yml")
        sys.exit(0)

    groq_api_key = os.environ.get("GROQ_API_KEY", "").strip()
    groq_client = None
    if groq_api_key:
        from groq import Groq
        groq_client = Groq(api_key=groq_api_key)
        print("[INFO] Groq AI özet zenginleştirmesi aktif.")
    else:
        print("[WARN] GROQ_API_KEY bulunamadı, Open Library fallback aktif.")

    repo_env = os.environ.get("GITHUB_REPOSITORY", "/")
    owner = repo_env.split("/")[0]

    sections: dict[str, list] = {}
    for cat in categories:
        queries = cat.get("queries", [])
        if not queries:
            continue
        print(f"[INFO] Searching: {cat['name']}  ({len(queries)} queries)")
        books = google_books_search(queries, want=BOOKS_PER_CATEGORY)
        sections[cat["name"]] = books
        print(f"       Found {len(books)} books")

    # ── Özet zenginleştirme ──────────────────────────────────────────────────
    all_books = [(cat, i, book)
                 for cat, books in sections.items()
                 for i, book in enumerate(books)]

    print(f"[INFO] {len(all_books)} kitap için özet zenginleştiriliyor...")
    for idx, (cat, i, book) in enumerate(all_books):
        print(f"[INFO]   ({idx+1}/{len(all_books)}) {book['title']}")
        sections[cat][i] = enrich_book(book, groq_client)
        if groq_client and idx < len(all_books) - 1:
            time.sleep(0.5)  # rate limit koruması

    total = sum(len(v) for v in sections.values())
    print(f"[INFO] Total: {total} books in {len(sections)} categories")

    today    = datetime.now(tz=timezone.utc)
    date_str = today.strftime("%-d %B %Y")

    create_github_issue(
        title=f"\U0001f4da Daily Books Digest \u2014 {date_str}",
        body=build_issue_body(sections, date_str),
        owner=owner,
    )


if __name__ == "__main__":
    main()
