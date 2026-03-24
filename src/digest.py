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
import re
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
GITHUB_PAGES_URL = os.environ.get("PAGES_URL", "https://nevzatalkan.github.io/new-books/")

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

            thumb = info.get("imageLinks", {}).get("thumbnail", "")
            books.append({
                "title":         title,
                "authors":       ", ".join(info.get("authors", ["Unknown Author"])),
                "published":     info.get("publishedDate", ""),
                "description":   desc,
                "link":          info.get("infoLink", ""),
                "rating":        info.get("averageRating", 0),
                "ratings_count": info.get("ratingsCount", 0),
                "thumbnail":     thumb.replace("http://", "https://") if thumb else "",
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
        "  \"topics\": [\"konu 1\", \"konu 2\", \"konu 3\", \"konu 4\", \"konu 5\"],\n"
        "  \"audience\": \"Bu kitap ... için idealdir.\"\n"
        "}\n\n"
        "- topics: kitabın 3-5 ana konu başlığı (kısa, madde madde)\n"
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


# ── Data helpers ───────────────────────────────────────────────────────────────

def category_slug(name: str) -> str:
    """Category adından URL-safe anchor ID üretir."""
    slug = re.sub(r"[^\w\s-]", "", name).strip().lower()
    slug = re.sub(r"[\s&]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


def build_daily_json(sections: dict[str, list], date_str: str, date_iso: str) -> dict:
    """Günlük özet verisini JSON-serileştirilebilir dict olarak döner."""
    categories = [
        {"name": name, "slug": category_slug(name), "books": books}
        for name, books in sections.items()
        if books
    ]
    return {
        "date":       date_str,
        "date_iso":   date_iso,
        "total":      sum(len(c["books"]) for c in categories),
        "categories": categories,
    }


def save_daily_data(data: dict, date_iso: str) -> str:
    """JSON dosyasını docs/data/ altına kaydeder, manifest.json'u günceller."""
    docs_data = ROOT / "docs" / "data"
    docs_data.mkdir(parents=True, exist_ok=True)

    # Günlük dosya
    filename = f"{date_iso}.json"
    (docs_data / filename).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[OK] Kaydedildi: docs/data/{filename}")

    # Manifest güncelle
    manifest_path = docs_data / "manifest.json"
    manifest: list[dict] = []
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest = [m for m in manifest if m.get("date_iso") != date_iso]
    manifest.insert(0, {
        "date":       data["date"],
        "date_iso":   date_iso,
        "file":       filename,
        "total":      data["total"],
        "categories": len(data["categories"]),
    })

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[OK] Manifest güncellendi ({len(manifest)} giriş)")
    return filename


def build_issue_body(data: dict, pages_url: str) -> str:
    """Sadece linki ve kısa özeti içeren GitHub Issue içeriği."""
    lines = [
        f"# 📚 Daily Books Digest — {data['date']}",
        "",
        f"**{data['total']}** yeni kitap · **{len(data['categories'])}** kategori",
        "",
        f"## 👉 [Tüm digeseti görüntüle]({pages_url})",
        "",
        "---",
        "",
        "| Kategori | Kitap |",
        "|----------|:-----:|",
    ]
    for cat in data["categories"]:
        lines.append(f"| {cat['name']} | {len(cat['books'])} |")

    # Kapak görseli olan ilk 3 kitabı öne çıkar (kategori slug'ı ile birlikte)
    featured: list[tuple[str, dict]] = []
    for cat in data["categories"]:
        for book in cat["books"]:
            if book.get("thumbnail"):
                featured.append((cat["slug"], book))
            if len(featured) >= 3:
                break
        if len(featured) >= 3:
            break

    if featured:
        lines += ["", "---", "", "### Öne Çıkan Kitaplar", ""]
        for cat_slug, book in featured:
            # Sayfada ilgili kategoriye giden link — Google Books linki mailde yok
            page_link = f"{pages_url.rstrip('/')}#{cat_slug}"
            thumb = book["thumbnail"]
            title = book["title"]
            lines.append(f"[![{title}]({thumb})]({page_link})")
            lines.append(f"**[{title}]({page_link})**")
            if book.get("audience"):
                lines.append(f"*{book['audience']}*")
            lines.append("")

    lines += [
        "---",
        "",
        f"*[Tüm kitaplar → {pages_url}]({pages_url})*  ",
        "*Generated by [nevzatalkan/new-books](https://github.com/nevzatalkan/new-books)"
        " · 🤖 Groq AI · 📖 Open Library*",
    ]
    return "\n".join(lines)


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
    date_iso = today.strftime("%Y-%m-%d")

    # JSON verisini oluştur ve kaydet
    data = build_daily_json(sections, date_str, date_iso)
    save_daily_data(data, date_iso)

    create_github_issue(
        title=f"\U0001f4da Daily Books Digest \u2014 {date_str}",
        body=build_issue_body(data, GITHUB_PAGES_URL),
        owner=owner,
    )


if __name__ == "__main__":
    main()
