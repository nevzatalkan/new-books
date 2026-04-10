#!/usr/bin/env python3
"""
Daily New Books Digest
----------------------
Fetches recently published English books from Google Books API for each
configured category and creates a GitHub Issue as the daily digest.
The issue is assigned to the repo owner so GitHub sends an email notification.
Book descriptions are enriched via Open Library (fallback) and DeepSeek AI (Turkish summary).

Each category has three sections:
  - New books (newest publications, not seen in last 90 days)
  - Popular recent (last 10 years, sorted by popularity)
  - Popular classic (10–100 years old, sorted by popularity)

Required secrets:
    GITHUB_TOKEN  – built-in Actions token
    DEEPSEEK_API_KEY  – optional; skips AI enrichment if missing
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_FILE = ROOT / "categories.yml"
SEEN_BOOKS_FILE = ROOT / "docs" / "data" / "seen_books.json"
BOOKS_PER_CATEGORY = 5
POPULAR_PER_CATEGORY = 5
SEEN_BOOK_EXPIRY_DAYS = 90
GITHUB_PAGES_URL = os.environ.get("PAGES_URL", "https://nevzatalkan.github.io/new-books/")

OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPENLIBRARY_WORKS_URL = "https://openlibrary.org"
DEEPSEEK_MODEL = "deepseek-chat"
OL_HEADERS = {"User-Agent": "new-books-digest/1.0 (github.com/nevzatalkan/new-books)"}


# ── Seen books tracking ────────────────────────────────────────────────────────

def load_seen_books() -> dict:
    """Load the seen-books registry from disk."""
    if SEEN_BOOKS_FILE.exists():
        return json.loads(SEEN_BOOKS_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen_books(seen: dict) -> None:
    """Persist seen-books registry, pruning entries older than expiry."""
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=SEEN_BOOK_EXPIRY_DAYS)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_BOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_BOOKS_FILE.write_text(json.dumps(pruned, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] seen_books.json güncellendi ({len(pruned)} kitap)")


def build_seen_set(seen: dict, today_iso: str) -> set[str]:
    """Return title keys to exclude: sent within 90 days but NOT today (same-day re-runs allowed)."""
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=SEEN_BOOK_EXPIRY_DAYS)).strftime("%Y-%m-%d")
    return {
        title for title, sent_date in seen.items()
        if sent_date != today_iso and sent_date >= cutoff
    }


def mark_seen(books: list[dict], seen: dict, today_iso: str) -> None:
    for book in books:
        seen[book["title"].lower()] = today_iso


# ── Google Books ───────────────────────────────────────────────────────────────

def _parse_year(date_str: str) -> int | None:
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except (ValueError, IndexError):
        return None


def _fetch(query: str, order_by: str = "newest") -> list[dict]:
    """Single Google Books API call. Retries up to 3 times on failure or empty result."""
    params = {
        "q":            query,
        "orderBy":      order_by,
        "maxResults":   40,
        "printType":    "books",
        "langRestrict": "en",
    }
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                items = json.loads(resp.read()).get("items", [])
                if items or attempt == 2:
                    return items
                print(f"[WARN] Empty result for '{query}' (attempt {attempt+1}/3), retrying...")
                time.sleep(2 ** attempt)
        except Exception as exc:
            print(f"[WARN] API error for '{query}' (attempt {attempt+1}/3): {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return []


def _item_to_book(item: dict) -> dict | None:
    """Convert a Google Books API item to our internal dict. Returns None if no title."""
    info = item.get("volumeInfo", {})
    title = info.get("title", "").strip()
    if not title:
        return None
    desc = info.get("description", "")
    if len(desc) > 500:
        desc = desc[:497] + "..."
    thumb = info.get("imageLinks", {}).get("thumbnail", "")
    return {
        "title":         title,
        "authors":       ", ".join(info.get("authors", ["Unknown Author"])),
        "published":     info.get("publishedDate", ""),
        "description":   desc,
        "link":          info.get("infoLink", ""),
        "rating":        info.get("averageRating", 0),
        "ratings_count": info.get("ratingsCount", 0),
        "thumbnail":     thumb.replace("http://", "https://") if thumb else "",
    }


def google_books_search(queries: list[str], want: int, seen_set: set[str]) -> list[dict]:
    """Search newest books, skip seen titles, deduplicate, return up to want books."""
    seen_titles: set[str] = set()
    books: list[dict] = []

    for query in queries:
        for item in _fetch(query, order_by="newest"):
            book = _item_to_book(item)
            if not book:
                continue
            key = book["title"].lower()
            if key in seen_titles or key in seen_set:
                continue
            seen_titles.add(key)
            books.append(book)
        if len(books) >= want:
            break

    return books[:want]


def google_books_popular(queries: list[str], want: int,
                         min_year: int, max_year: int,
                         seen_set: set[str]) -> list[dict]:
    """Fetch popular books within a year range, skip seen, sort by ratings count."""
    seen_titles: set[str] = set()
    candidates: list[dict] = []

    for query in queries:
        for item in _fetch(query, order_by="relevance"):
            book = _item_to_book(item)
            if not book:
                continue
            key = book["title"].lower()
            if key in seen_titles or key in seen_set:
                continue
            year = _parse_year(book["published"])
            if not year or not (min_year <= year <= max_year):
                continue
            seen_titles.add(key)
            candidates.append(book)

    candidates.sort(key=lambda b: (b["ratings_count"], b["rating"]), reverse=True)
    return candidates[:want]


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


# ── DeepSeek AI ───────────────────────────────────────────────────────────────

def enhance_with_deepseek(title: str, author: str, google_desc: str,
                          ol_desc: str, ai_client) -> dict | None:
    """
    DeepSeek ile kitap bilgilerini zenginleştirir.
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
        response = ai_client.chat.completions.create(
            model=DEEPSEEK_MODEL,
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
        print(f"[WARN] DeepSeek hatası ({title}): {exc}")
        return None


def enrich_book(book: dict, ai_client) -> dict:
    """
    Bir kitabın bilgilerini zenginleştirir (sadece yeni kitaplar için).
    Öncelik: DeepSeek (topics+audience) → Open Library fallback → Google Books
    """
    title = book["title"]
    author = book["authors"]
    google_desc = book["description"]

    ol_desc = ""
    if not google_desc or len(google_desc) < 100:
        ol_desc = fetch_openlibrary_description(title, author)
        if ol_desc:
            print(f"[INFO]   Open Library açıklaması bulundu: {title}")

    if ai_client:
        enriched = enhance_with_deepseek(title, author, google_desc, ol_desc, ai_client)
        if enriched:
            return {**book, "topics": enriched["topics"],
                    "audience": enriched["audience"],
                    "description_source": "deepseek"}

    if ol_desc:
        truncated = ol_desc[:400] + ("…" if len(ol_desc) > 400 else "")
        return {**book, "description": truncated, "description_source": "openlibrary"}

    return {**book, "description_source": "google"}


# ── Data helpers ───────────────────────────────────────────────────────────────

def category_slug(name: str) -> str:
    """Category adından URL-safe anchor ID üretir."""
    slug = re.sub(r"[^\w\s-]", "", name).strip().lower()
    slug = re.sub(r"[\s&]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


def build_daily_json(sections: dict[str, dict], date_str: str, date_iso: str) -> dict:
    """sections[cat_name] = {"new": [...], "popular_recent": [...], "popular_classic": [...]}"""
    categories = []
    for name, data in sections.items():
        if not (data["new"] or data["popular_recent"] or data["popular_classic"]):
            continue
        categories.append({
            "name":             name,
            "slug":             category_slug(name),
            "books":            data["new"],
            "popular_recent":   data["popular_recent"],
            "popular_classic":  data["popular_classic"],
        })
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

    filename = f"{date_iso}.json"
    (docs_data / filename).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[OK] Kaydedildi: docs/data/{filename}")

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
    """GitHub Issue içeriği: açıklama yok, özet tablo + kategori başına 3 bölüm."""
    lines = [
        f"# 📚 Daily Books Digest — {data['date']}",
        "",
        "| Kategori | 🆕 Yeni | ⭐ Popüler (10 yıl) | 📜 Klasik (10-100 yıl) |",
        "|----------|:------:|:------------------:|:---------------------:|",
    ]
    for cat in data["categories"]:
        n_new = len(cat["books"])
        n_rec = len(cat.get("popular_recent", []))
        n_cls = len(cat.get("popular_classic", []))
        lines.append(f"| {cat['name']} | {n_new} | {n_rec} | {n_cls} |")

    lines += [
        "",
        f"## 👉 [Tüm digeseti görüntüle]({pages_url})",
        "",
        "---",
        "",
    ]

    def book_table(books: list[dict]) -> list[str]:
        rows = ["| Kitap | Yazar | Yıl | Puan |", "|------|-------|-----|------|"]
        for b in books:
            year   = b["published"][:4] if b.get("published") else "—"
            link   = b.get("link", "")
            title  = b["title"].replace("|", "\\|")
            author = b["authors"].replace("|", "\\|")
            cell   = f"[{title}]({link})" if link else title
            rating = b.get("rating", 0)
            count  = b.get("ratings_count", 0)
            puan   = f"⭐ {rating}/5 ({count:,})" if rating else "—"
            rows.append(f"| {cell} | {author} | {year} | {puan} |")
        return rows

    for cat in data["categories"]:
        lines.append(f"## {cat['name']}")
        lines.append("")

        if cat["books"]:
            lines.append("### 🆕 Yeni Kitaplar")
            lines += book_table(cat["books"])
            lines.append("")

        if cat.get("popular_recent"):
            lines.append("### ⭐ Son 10 Yılın Popülerleri")
            lines += book_table(cat["popular_recent"])
            lines.append("")

        if cat.get("popular_classic"):
            lines.append("### 📜 Klasikler (10-100 Yıl)")
            lines += book_table(cat["popular_classic"])
            lines.append("")

    lines += [
        "---",
        "",
        f"*[Tüm kitaplar → {pages_url}]({pages_url})*  ",
        "*Generated by [nevzatalkan/new-books](https://github.com/nevzatalkan/new-books)"
        " · 🤖 DeepSeek AI · 📖 Open Library*",
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
        cfg = yaml.safe_load(f)

    categories = [c for c in cfg.get("categories", []) if c.get("active")]
    if not categories:
        print("[WARN] No active categories in categories.yml")
        sys.exit(0)

    deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    ai_client = None
    if deepseek_api_key:
        from openai import OpenAI
        ai_client = OpenAI(api_key=deepseek_api_key, base_url="https://api.deepseek.com")
        print("[INFO] DeepSeek AI özet zenginleştirmesi aktif.")
    else:
        print("[WARN] DEEPSEEK_API_KEY bulunamadı, Open Library fallback aktif.")

    repo_env = os.environ.get("GITHUB_REPOSITORY", "/")
    owner = repo_env.split("/")[0]

    today       = datetime.now(tz=timezone.utc)
    today_iso   = today.strftime("%Y-%m-%d")
    date_str    = today.strftime("%-d %B %Y")
    current_year = today.year

    seen     = load_seen_books()
    seen_set = build_seen_set(seen, today_iso)
    print(f"[INFO] {len(seen_set)} kitap son 90 günde gönderilmiş, atlanacak.")

    sections: dict[str, dict] = {}
    for cat in categories:
        queries = cat.get("queries", [])
        if not queries:
            continue
        name = cat["name"]
        print(f"[INFO] Searching: {name}")

        new_books       = google_books_search(queries, BOOKS_PER_CATEGORY, seen_set)
        popular_recent  = google_books_popular(queries, POPULAR_PER_CATEGORY,
                                               current_year - 10, current_year, seen_set)
        popular_classic = google_books_popular(queries, POPULAR_PER_CATEGORY,
                                               current_year - 100, current_year - 10, seen_set)

        print(f"       Yeni: {len(new_books)} | "
              f"Popüler (10 yıl): {len(popular_recent)} | "
              f"Klasik: {len(popular_classic)}")

        sections[name] = {
            "new":              new_books,
            "popular_recent":   popular_recent,
            "popular_classic":  popular_classic,
        }

    # Enrich only new books (descriptions shown on web page)
    all_new = [
        (cat_name, i, book)
        for cat_name, data in sections.items()
        for i, book in enumerate(data["new"])
    ]
    print(f"[INFO] {len(all_new)} yeni kitap için özet zenginleştiriliyor...")
    for idx, (cat_name, i, book) in enumerate(all_new):
        print(f"[INFO]   ({idx+1}/{len(all_new)}) {book['title']}")
        sections[cat_name]["new"][i] = enrich_book(book, ai_client)
        if ai_client and idx < len(all_new) - 1:
            time.sleep(0.5)

    # Mark all sent books as seen
    all_sent = [
        book
        for data in sections.values()
        for book in data["new"] + data["popular_recent"] + data["popular_classic"]
    ]
    mark_seen(all_sent, seen, today_iso)
    save_seen_books(seen)

    total_new = sum(len(s["new"]) for s in sections.values())
    total_rec = sum(len(s["popular_recent"]) for s in sections.values())
    total_cls = sum(len(s["popular_classic"]) for s in sections.values())
    print(f"[INFO] Toplam: {total_new} yeni + {total_rec} popüler + {total_cls} klasik")

    data = build_daily_json(sections, date_str, today_iso)
    save_daily_data(data, today_iso)

    create_github_issue(
        title=f"\U0001f4da Daily Books Digest \u2014 {date_str}",
        body=build_issue_body(data, GITHUB_PAGES_URL),
        owner=owner,
    )


if __name__ == "__main__":
    main()
