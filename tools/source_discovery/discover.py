from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse

import httpx
import tldextract
import yaml
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]

EXISTING_SOURCES_PATH = REPO_ROOT / "sources.yml"
OUTPUT_PATH = SCRIPT_DIR / "candidate_sources.discovered.yml"
CACHE_PATH = SCRIPT_DIR / "cache.sqlite"


# ---------------------------------------------------------------------
# Discovery defaults
# ---------------------------------------------------------------------

DEFAULT_SEARCH_PATTERNS = [
    "*gender-affirming-care*",
    "*gender-affirming-health*",
    "*transgender-health*",
    "*trans-health*",
    "*legal-gender-recognition*",
    "*gender-marker*",
    "*name-change-gender-marker*",
    "*transgender-legal-aid*",
    "*lgbtq-asylum*",
    "*transgender-asylum*",
    "*trans-youth-school*",
    "*transgender-school-policy*",
    "*transgender-crisis*",
    "*transgender-shelter*",
    "*lgbtq-transgender-resources*",
]

TRUSTED_DOMAIN_HINTS = [
    ".gov",
    ".edu",
    ".ac.",
    ".nhs.uk",
    ".who.int",
    ".un.org",
    ".ohchr.org",
    ".hrw.org",
    ".amnesty.org",
    ".plannedparenthood.org",
    ".aclu.org",
    ".lambdaLegal.org".lower(),
    ".trevorproject.org",
    ".glaad.org",
]

POSITIVE_TERMS = {
    "healthcare": [
        "transgender health",
        "trans health",
        "gender affirming care",
        "gender-affirming care",
        "hormone therapy",
        "puberty blockers",
        "informed consent",
        "wpath",
        "endocrine society",
    ],
    "legal": [
        "legal gender recognition",
        "gender marker",
        "name change",
        "identity document",
        "birth certificate",
        "passport",
        "discrimination",
        "legal aid",
    ],
    "safety": [
        "crisis",
        "hotline",
        "shelter",
        "domestic violence",
        "emergency",
        "suicide prevention",
        "safety plan",
    ],
    "youth": [
        "trans youth",
        "transgender youth",
        "school policy",
        "students",
        "parents",
        "minors",
        "young people",
    ],
    "asylum": [
        "asylum",
        "refugee",
        "immigration",
        "country conditions",
        "persecution",
    ],
    "human-rights": [
        "human rights",
        "civil rights",
        "gender identity",
        "equality",
        "anti-discrimination",
    ],
    "community": [
        "community center",
        "support group",
        "peer support",
        "resource directory",
        "lgbtq center",
        "lgbt center",
    ],
}

NEGATIVE_REVIEW_TERMS = [
    "conversion therapy",
    "gender critical",
    "rapid onset gender dysphoria",
    "social contagion",
    "transgender ideology",
    "grooming",
    "irreversible damage",
    "protect children from transgender",
]

BLOCKED_PATH_PARTS = [
    "/donate",
    "/shop",
    "/store",
    "/cart",
    "/checkout",
    "/login",
    "/signup",
    "/tag/",
    "/author/",
    "/category/",
    "/events",
    "/event/",
    "/press-release",
]

HEADERS = {
    "User-Agent": (
        "TransResourceSearchSourceDiscovery/0.1 "
        "(local candidate-source discovery; respectful cached requests)"
    )
}


@dataclass
class CandidatePage:
    url: str
    title: str
    score: int
    topics: list[str]
    reasons: list[str]
    notes: str


# ---------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------

def log_noop(message: str) -> None:
    print(message)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def registered_domain(url: str) -> str:
    ext = tldextract.extract(url)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower()


def hostname(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def clean_name_from_domain(domain: str) -> str:
    base = domain.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


def is_probably_html_url(url: str) -> bool:
    lowered = url.lower().split("?", 1)[0]
    blocked_exts = (
        ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
        ".zip", ".mp4", ".mp3", ".doc", ".docx", ".xls", ".xlsx",
        ".ppt", ".pptx", ".css", ".js", ".json", ".xml",
    )
    return not lowered.endswith(blocked_exts)


def should_skip_path(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return True

    if not path or path == "/":
        return False

    return any(part in path for part in BLOCKED_PATH_PARTS)


# ---------------------------------------------------------------------
# Existing source exclusion
# ---------------------------------------------------------------------

def load_existing_domains(log: Callable[[str], None] = log_noop) -> set[str]:
    if not EXISTING_SOURCES_PATH.exists():
        log(f"Existing sources file not found: {EXISTING_SOURCES_PATH}")
        return set()

    try:
        data = yaml.safe_load(EXISTING_SOURCES_PATH.read_text(encoding="utf-8")) or []
    except Exception as error:
        log(f"Could not read existing sources.yml: {error}")
        return set()

    if isinstance(data, list):
        sources = data
    elif isinstance(data, dict):
        sources = []
        for value in data.values():
            if isinstance(value, list):
                sources.extend(value)
    else:
        sources = []

    domains = set()

    for source in sources:
        if not isinstance(source, dict):
            continue

        for key in ["base_url", "url", "site"]:
            value = source.get(key)
            if isinstance(value, str) and value.startswith("http"):
                domain = registered_domain(value)
                if domain:
                    domains.add(domain)

        for page in source.get("candidate_pages", []) or []:
            if isinstance(page, dict):
                value = page.get("url")
                if isinstance(value, str) and value.startswith("http"):
                    domain = registered_domain(value)
                    if domain:
                        domains.add(domain)

    return domains


# ---------------------------------------------------------------------
# SQLite HTTP cache
# ---------------------------------------------------------------------

def init_cache() -> sqlite3.Connection:
    db = sqlite3.connect(CACHE_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS http_cache (
            url TEXT PRIMARY KEY,
            status INTEGER NOT NULL,
            body TEXT NOT NULL,
            fetched_at INTEGER NOT NULL
        )
        """
    )
    db.commit()
    return db


def cache_get(
    db: sqlite3.Connection,
    url: str,
    max_age_seconds: int = 60 * 60 * 24 * 30,
) -> tuple[int, str] | None:
    row = db.execute(
        "SELECT status, body, fetched_at FROM http_cache WHERE url = ?",
        (url,),
    ).fetchone()

    if not row:
        return None

    status, body, fetched_at = row
    if int(time.time()) - int(fetched_at) > max_age_seconds:
        return None

    return int(status), str(body)


def cache_set(db: sqlite3.Connection, url: str, status: int, body: str) -> None:
    # Keep cache bounded. 2 MB is plenty for scoring and prevents giant pages.
    body = body[:2_000_000]

    db.execute(
        """
        INSERT OR REPLACE INTO http_cache(url, status, body, fetched_at)
        VALUES (?, ?, ?, ?)
        """,
        (url, int(status), body, int(time.time())),
    )
    db.commit()


def fetch_text(
    db: sqlite3.Connection,
    url: str,
    timeout_seconds: int = 60,
    retries: int = 2,
    use_cache: bool = True,
    log: Callable[[str], None] = log_noop,
) -> str | None:
    if use_cache:
        cached = cache_get(db, url)
        if cached:
            status, body = cached
            if 200 <= status < 300:
                return body

    for attempt in range(retries + 1):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout_seconds),
                follow_redirects=True,
                headers=HEADERS,
            ) as client:
                response = client.get(url)

            content_type = response.headers.get("content-type", "").lower()
            text = response.text or ""

            cache_set(db, url, response.status_code, text)

            if 200 <= response.status_code < 300:
                if (
                    "text/html" in content_type
                    or "application/json" in content_type
                    or "text/plain" in content_type
                    or "xml" in content_type
                    or not content_type
                ):
                    return text

                return None

        except Exception as error:
            if attempt >= retries:
                log(f"Fetch failed: {url} — {error}")
                return None

        sleep_for = 2 + attempt * 5 + random.random()
        time.sleep(sleep_for)

    return None


# ---------------------------------------------------------------------
# Common Crawl discovery
# ---------------------------------------------------------------------

def get_common_crawl_indexes(
    db: sqlite3.Connection,
    timeout_seconds: int,
    log: Callable[[str], None],
    max_indexes: int = 4,
) -> list[str]:
    """
    Uses Common Crawl's index listing when available.
    Falls back to a few likely recent indexes if the listing is unavailable.
    """

    listing_url = "https://index.commoncrawl.org/collinfo.json"
    body = fetch_text(
        db,
        listing_url,
        timeout_seconds=timeout_seconds,
        retries=2,
        use_cache=True,
        log=log,
    )

    if body:
        try:
            data = json.loads(body)
            indexes = []
            for item in data:
                index_id = item.get("id")
                if isinstance(index_id, str) and index_id.startswith("CC-MAIN-"):
                    indexes.append(index_id)

            if indexes:
                return indexes[:max_indexes]
        except Exception:
            pass

    # Fallback only. The live listing is preferred.
    return [
        "CC-MAIN-2026-18",
        "CC-MAIN-2026-13",
        "CC-MAIN-2025-51",
        "CC-MAIN-2025-47",
    ][:max_indexes]


def common_crawl_query(
    db: sqlite3.Connection,
    index: str,
    pattern: str,
    timeout_seconds: int,
    limit: int,
    log: Callable[[str], None],
) -> list[str]:
    params = {
        "url": pattern,
        "output": "json",
        "fl": "url,status,mime,timestamp",
        "filter": "status:200",
        "limit": str(limit),
    }

    query_url = f"https://index.commoncrawl.org/{index}-index?{urlencode(params)}"

    body = fetch_text(
        db,
        query_url,
        timeout_seconds=timeout_seconds,
        retries=2,
        use_cache=True,
        log=log,
    )

    if not body:
        return []

    urls: list[str] = []

    for line in body.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue

        url = item.get("url", "")
        mime = item.get("mime", "")

        if not isinstance(url, str):
            continue

        if not url.startswith(("http://", "https://")):
            continue

        if mime and "html" not in str(mime).lower():
            continue

        if not is_probably_html_url(url):
            continue

        if should_skip_path(url):
            continue

        urls.append(url)

    return sorted(set(urls))


# ---------------------------------------------------------------------
# HTML parsing and scoring
# ---------------------------------------------------------------------

def parse_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "nav", "footer", "form", "noscript"]):
        tag.decompose()

    title = ""
    if soup.title:
        title = normalize_whitespace(soup.title.get_text(" ", strip=True))

    meta_desc = ""
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        meta_desc = normalize_whitespace(str(desc["content"]))

    h1 = ""
    h1_tag = soup.find("h1")
    if h1_tag:
        h1 = normalize_whitespace(h1_tag.get_text(" ", strip=True))

    text = normalize_whitespace(soup.get_text(" ", strip=True))
    combined = normalize_whitespace(f"{title} {meta_desc} {h1} {text}")

    return title or h1 or hostname_from_text_fallback(combined), combined


def hostname_from_text_fallback(text: str) -> str:
    if not text:
        return "Untitled page"
    return text[:80]


def classify_topics(text: str) -> list[str]:
    lowered = text.lower()
    topics = []

    for topic, terms in POSITIVE_TERMS.items():
        if any(term.lower() in lowered for term in terms):
            topics.append(topic)

    return sorted(set(topics))


def score_page(url: str, title: str, text: str) -> CandidatePage:
    lowered_url = url.lower()
    lowered_text = text.lower()
    domain = registered_domain(url)
    host = hostname(url)

    score = 0
    reasons: list[str] = []

    core_terms = [
        "transgender",
        "trans health",
        "transgender health",
        "gender affirming",
        "gender-affirming",
        "legal gender recognition",
        "gender marker",
        "name change",
        "gender identity",
    ]

    if any(term in lowered_url for term in core_terms):
        score += 20
        reasons.append("URL contains trans-resource terms")

    if any(term in lowered_text for term in core_terms):
        score += 30
        reasons.append("Page text contains trans-resource terms")

    topics = classify_topics(text)
    if topics:
        score += 12 * min(len(topics), 4)
        reasons.append("Matches useful resource topics")

    if any(hint in host.lower() or hint in domain.lower() for hint in TRUSTED_DOMAIN_HINTS):
        score += 22
        reasons.append("Domain matches trusted-domain hint")

    if domain.endswith(".gov") or ".gov." in host:
        score += 18
        reasons.append("Government domain")

    if domain.endswith(".edu") or ".edu." in host or ".ac." in host:
        score += 14
        reasons.append("Education or academic domain")

    if re.search(r"\b(resource|resources|guide|guidance|directory|clinic|legal aid|support)\b", lowered_text):
        score += 12
        reasons.append("Looks like a resource or guidance page")

    if re.search(r"\b(last updated|updated|reviewed|effective date)\b", lowered_text):
        score += 5
        reasons.append("Page appears to include update/review language")

    if any(term.lower() in lowered_text for term in NEGATIVE_REVIEW_TERMS):
        score -= 30
        reasons.append("Contains terms requiring careful manual review")

    if should_skip_path(url):
        score -= 40
        reasons.append("Low-value or blocked path pattern")

    if len(text) < 600:
        score -= 20
        reasons.append("Very little page text")

    notes = "; ".join(reasons)

    return CandidatePage(
        url=url,
        title=title or clean_name_from_domain(domain),
        score=score,
        topics=topics,
        reasons=reasons,
        notes=notes,
    )


# ---------------------------------------------------------------------
# Optional sitemap expansion for promising domains
# ---------------------------------------------------------------------

def discover_sitemap_urls(
    db: sqlite3.Connection,
    base_url: str,
    timeout_seconds: int,
    log: Callable[[str], None],
    max_urls: int = 40,
) -> list[str]:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return []

    root = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [
        f"{root}/robots.txt",
        f"{root}/sitemap.xml",
        f"{root}/sitemap_index.xml",
    ]

    sitemap_urls: set[str] = set()

    for candidate in candidates:
        body = fetch_text(
            db,
            candidate,
            timeout_seconds=timeout_seconds,
            retries=1,
            use_cache=True,
            log=log,
        )

        if not body:
            continue

        if candidate.endswith("/robots.txt"):
            for line in body.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap = line.split(":", 1)[1].strip()
                    if sitemap.startswith("http"):
                        sitemap_urls.add(sitemap)
        else:
            sitemap_urls.add(candidate)

    page_urls: set[str] = set()

    for sitemap_url in sorted(sitemap_urls)[:5]:
        body = fetch_text(
            db,
            sitemap_url,
            timeout_seconds=timeout_seconds,
            retries=1,
            use_cache=True,
            log=log,
        )

        if not body:
            continue

        soup = BeautifulSoup(body, "xml")
        for loc in soup.find_all("loc"):
            value = loc.get_text(strip=True)
            if not value.startswith("http"):
                continue
            if not is_probably_relevant_url(value):
                continue
            if should_skip_path(value):
                continue
            page_urls.add(value)
            if len(page_urls) >= max_urls:
                break

    return sorted(page_urls)


def is_probably_relevant_url(url: str) -> bool:
    lowered = url.lower()
    hints = [
        "trans",
        "gender",
        "lgbt",
        "lgbtq",
        "queer",
        "name-change",
        "gender-marker",
        "asylum",
        "discrimination",
        "health",
        "clinic",
        "youth",
        "school",
        "crisis",
        "shelter",
    ]
    return any(hint in lowered for hint in hints)


# ---------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------

def make_source_entry(domain: str, pages: list[CandidatePage]) -> dict:
    pages_sorted = sorted(pages, key=lambda page: page.score, reverse=True)
    best = pages_sorted[0]

    parsed = urlparse(best.url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    topics = sorted(set(topic for page in pages_sorted for topic in page.topics))
    if not topics:
        topics = ["unknown"]

    return {
        "name": clean_name_from_domain(domain),
        "base_url": base_url,
        "topic": topics,
        "region": ["unknown"],
        "trust_level": "candidate",
        "review_status": "manual_review_needed",
        "last_checked": str(date.today()),
        "notes": (
            "Automatically discovered candidate source. "
            "Manual review required before adding to trusted sources."
        ),
        "discovery": {
            "method": "common_crawl_url_index_and_optional_sitemap_expansion",
            "score": best.score,
            "example_url": best.url,
            "reasons": best.reasons,
        },
        "candidate_pages": [
            {
                "title": page.title,
                "url": page.url,
                "topics": page.topics,
                "notes": f"score={page.score}; {page.notes}",
            }
            for page in pages_sorted[:8]
        ],
    }


def write_output(entries: list[dict], output_path: Path = OUTPUT_PATH) -> None:
    output_path.write_text(
        yaml.safe_dump(
            entries,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------
# Main discovery run
# ---------------------------------------------------------------------

def run(
    min_score: int = 45,
    timeout_seconds: int = 60,
    per_query_limit: int = 80,
    max_indexes: int = 4,
    expand_sitemaps: bool = True,
    log: Callable[[str], None] = log_noop,
) -> Path:
    db = init_cache()

    existing_domains = load_existing_domains(log=log)
    log(f"Loaded {len(existing_domains)} existing domains to exclude.")

    indexes = get_common_crawl_indexes(
        db=db,
        timeout_seconds=timeout_seconds,
        log=log,
        max_indexes=max_indexes,
    )
    log(f"Using Common Crawl indexes: {', '.join(indexes)}")

    pages_by_domain: dict[str, list[CandidatePage]] = {}
    seen_urls: set[str] = set()

    for index in indexes:
        for pattern in DEFAULT_SEARCH_PATTERNS:
            log(f"Querying {index}: {pattern}")

            urls = common_crawl_query(
                db=db,
                index=index,
                pattern=pattern,
                timeout_seconds=timeout_seconds,
                limit=per_query_limit,
                log=log,
            )

            log(f"  Found {len(urls)} URL candidates.")

            for url in urls:
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                domain = registered_domain(url)
                if not domain:
                    continue

                if domain in existing_domains:
                    continue

                html = fetch_text(
                    db=db,
                    url=url,
                    timeout_seconds=timeout_seconds,
                    retries=2,
                    use_cache=True,
                    log=log,
                )

                if not html:
                    continue

                title, text = parse_html(html)
                candidate = score_page(url, title, text)

                if candidate.score < min_score:
                    continue

                pages_by_domain.setdefault(domain, []).append(candidate)
                log(f"  + {domain} score={candidate.score}: {candidate.title[:80]}")

    if expand_sitemaps:
        log("Expanding promising domains with sitemap discovery...")

        promising_domains = sorted(
            pages_by_domain.keys(),
            key=lambda d: max(page.score for page in pages_by_domain[d]),
            reverse=True,
        )[:40]

        for domain in promising_domains:
            best_page = sorted(
                pages_by_domain[domain],
                key=lambda page: page.score,
                reverse=True,
            )[0]

            parsed = urlparse(best_page.url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            sitemap_urls = discover_sitemap_urls(
                db=db,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                log=log,
                max_urls=20,
            )

            for url in sitemap_urls:
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                if registered_domain(url) in existing_domains:
                    continue

                html = fetch_text(
                    db=db,
                    url=url,
                    timeout_seconds=timeout_seconds,
                    retries=1,
                    use_cache=True,
                    log=log,
                )

                if not html:
                    continue

                title, text = parse_html(html)
                candidate = score_page(url, title, text)

                if candidate.score >= min_score:
                    pages_by_domain.setdefault(domain, []).append(candidate)
                    log(f"  sitemap + {domain} score={candidate.score}: {candidate.title[:80]}")

    entries = [
        make_source_entry(domain, pages)
        for domain, pages in pages_by_domain.items()
        if pages
    ]

    entries.sort(
        key=lambda entry: int(entry.get("discovery", {}).get("score", 0)),
        reverse=True,
    )

    write_output(entries, OUTPUT_PATH)

    log(f"Done. Wrote {len(entries)} candidate source entries.")
    log(f"Output: {OUTPUT_PATH}")

    return OUTPUT_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover candidate trans-resource sources without using paid search APIs."
    )

    parser.add_argument("--min-score", type=int, default=45)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--per-query-limit", type=int, default=80)
    parser.add_argument("--max-indexes", type=int, default=4)
    parser.add_argument("--no-sitemaps", action="store_true")

    args = parser.parse_args()

    run(
        min_score=args.min_score,
        timeout_seconds=args.timeout,
        per_query_limit=args.per_query_limit,
        max_indexes=args.max_indexes,
        expand_sitemaps=not args.no_sitemaps,
        log=print,
    )


if __name__ == "__main__":
    main()
