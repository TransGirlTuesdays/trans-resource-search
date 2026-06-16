#!/usr/bin/env python3
"""
Discover trusted trans/LGBTQ+ resource source candidates without paid search APIs.

What it does:
1. Pulls org websites from Wikidata SPARQL.
2. Pulls external links from relevant Wikipedia pages.
3. Expands/validates domains through robots.txt + sitemaps.
4. Scores trans-relevant pages.
5. Writes candidate_sources.yml for human review.

Install:
  pip install requests beautifulsoup4 pyyaml tldextract

Run:
  python maow.py --existing sources.yml --out candidate_sources.yml --limit 100
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from typing import Iterable
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import requests
import tldextract
import yaml


USER_AGENT = (
    "TransResourceSearchBot/0.1 "
    "(+https://trans-resource-search.pages.dev/; contact: replace-me@example.com)"
)

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 45
TEXT_TIMEOUT = 20
SLEEP = 1.25

TRANS_TERMS = re.compile(
    r"\b("
    r"transgender|transsexual|trans\b|nonbinary|non-binary|gender diverse|"
    r"gender identity|gender dysphoria|gender affirming|gender-affirming|"
    r"hrt\b|hormone therapy|puberty blockers|legal gender recognition|"
    r"name change|lgbtq|lgbti|lgbtiq|queer|intersex"
    r")\b",
    re.I,
)

GOOD_PATH_TERMS = re.compile(
    r"(trans|transgender|lgbt|lgbti|lgbtq|gender|nonbinary|non-binary|"
    r"intersex|health|clinic|legal|rights|safety|crisis|guide|resource|research|policy)",
    re.I,
)

BAD_PATH_TERMS = re.compile(
    r"(/donate|/shop|/store|/cart|/checkout|/events?|/press-release|/tag/|"
    r"/author/|/category/|/wp-json|/feed|/login|/signup)",
    re.I,
)

TRUST_HINTS = {
    ".gov": 30,
    ".edu": 25,
    ".org": 15,
    ".int": 25,
}

WIKIPEDIA_PAGES = [
    "Transgender rights",
    "Transgender health care",
    "Legal recognition of non-binary gender",
    "LGBT rights by country or territory",
    "Gender-affirming healthcare",
    "LGBT community centre",
]

SEED_DOMAINS = [
    "wpath.org",
    "ilga.org",
    "outrightinternational.org",
    "hrw.org",
    "amnesty.org",
    "glaad.org",
    "thetrevorproject.org",
    "translifeline.org",
    "stonewall.org.uk",
    "genderdysphoria.fyi",
]


@dataclass
class Candidate:
    name: str
    base_url: str
    sitemap_urls: list[str]
    allowed_paths: list[str]
    blocked_paths: list[str]
    topic: list[str]
    region: list[str]
    trust_level: str
    review_status: str
    last_checked: str
    discovery: dict
    candidate_pages: list[dict]


def request_json(url: str, **params):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    last_error = None

    for attempt in range(3):
        try:
            r = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            r.raise_for_status()
            time.sleep(SLEEP)
            return r.json()
        except requests.RequestException as e:
            last_error = e
            wait = 2 ** attempt
            print(f"  ! request failed: {url} ({e}); retrying in {wait}s")
            time.sleep(wait)

    print(f"  ! giving up on {url}: {last_error}")
    return None


def request_text(url: str) -> str | None:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xml,text/xml,text/plain,*/*;q=0.8",
    }

    last_error = None

    for attempt in range(2):
        try:
            r = requests.get(
                url,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, TEXT_TIMEOUT),
                allow_redirects=True,
            )

            if r.status_code >= 400:
                return None

            ctype = r.headers.get("content-type", "").lower()

            if (
                "text" not in ctype
                and "xml" not in ctype
                and "html" not in ctype
                and "application/rss" not in ctype
                and "application/atom" not in ctype
            ):
                return None

            time.sleep(SLEEP)
            return r.text[:3_000_000]

        except requests.RequestException as e:
            last_error = e
            wait = 2 ** attempt
            print(f"  ! text request failed: {url} ({e}); retrying in {wait}s")
            time.sleep(wait)

    print(f"  ! giving up on text request: {url}: {last_error}")
    return None


def normalize_base(url: str) -> str | None:
    if not url:
        return None

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if not parsed.netloc:
        return None

    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def registrable_domain(url: str) -> str:
    parsed = urlparse(url)
    ext = tldextract.extract(parsed.netloc)
    return f"{ext.domain}.{ext.suffix}".lower()


def path_of(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"


def load_existing_domains(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
    except FileNotFoundError:
        print(f"  ! existing file not found: {path}; continuing with empty existing list")
        return set()
    except yaml.YAMLError as e:
        print(f"  ! could not parse {path}: {e}; continuing with empty existing list")
        return set()

    domains = set()

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("sources", data.get("candidate_pages", []))
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue

        url = (
            item.get("base_url")
            or item.get("url")
            or item.get("site")
            or item.get("website")
        )

        if url:
            try:
                domains.add(registrable_domain(url))
            except Exception:
                pass

    return domains


def discover_from_wikidata(limit: int = 300) -> list[tuple[str, str]]:
    query = f"""
    SELECT ?item ?itemLabel ?website ?desc WHERE {{
      ?item wdt:P856 ?website.
      ?item rdfs:label ?itemLabel.
      OPTIONAL {{ ?item schema:description ?desc FILTER(LANG(?desc)="en") }}
      FILTER(LANG(?itemLabel)="en")
      FILTER(REGEX(CONCAT(STR(?itemLabel), " ", STR(?desc)),
        "transgender|transsexual|LGBT|LGBTI|LGBTQ|queer|intersex|gender identity|gender affirming", "i"))
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    LIMIT {limit}
    """

    data = request_json(
        "https://query.wikidata.org/sparql",
        query=query,
        format="json",
    )

    if not data:
        print("  ! Wikidata discovery skipped because the request failed")
        return []

    out = []

    try:
        rows = data["results"]["bindings"]
    except KeyError:
        print("  ! Wikidata returned an unexpected response")
        return []

    for row in rows:
        name = row.get("itemLabel", {}).get("value", "Unknown source")
        website = row.get("website", {}).get("value")
        base = normalize_base(website)

        if base:
            out.append((name, base))

    return out


def discover_from_wikipedia() -> list[tuple[str, str]]:
    out = []

    for page in WIKIPEDIA_PAGES:
        params = {
            "action": "query",
            "format": "json",
            "titles": page,
            "prop": "extlinks",
            "ellimit": "max",
        }

        data = request_json("https://en.wikipedia.org/w/api.php", **params)

        if not data:
            print(f"  ! Wikipedia page skipped because request failed: {page}")
            continue

        pages = data.get("query", {}).get("pages", {})

        for _, info in pages.items():
            for link in info.get("extlinks", []):
                url = link.get("*") or link.get("url")
                base = normalize_base(url)

                if not base:
                    continue

                if any(x in base for x in ["wikipedia.org", "wikimedia.org"]):
                    continue

                out.append((f"Wikipedia external link from {page}", base))

    return out


def can_fetch(base_url: str, url: str) -> bool:
    robots_url = urljoin(base_url + "/", "robots.txt")
    rp = RobotFileParser()

    try:
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def find_sitemaps(base_url: str) -> list[str]:
    robots_txt = request_text(urljoin(base_url + "/", "robots.txt")) or ""

    found = re.findall(r"(?im)^sitemap:\s*(https?://\S+)", robots_txt)

    common = [
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/wp-sitemap.xml",
        "/sitemap/sitemap.xml",
    ]

    for path in common:
        found.append(urljoin(base_url + "/", path.lstrip("/")))

    working = []
    seen = set()

    for sitemap_url in found:
        if sitemap_url in seen:
            continue

        seen.add(sitemap_url)

        txt = request_text(sitemap_url)

        if txt and ("<urlset" in txt or "<sitemapindex" in txt):
            working.append(sitemap_url)

    return working[:10]


def parse_sitemap_urlset(xml_text: str) -> list[str]:
    urls = []

    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError:
        return urls

    ns = ""

    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    if root.tag.endswith("sitemapindex"):
        for sm in root.findall(f".//{ns}sitemap/{ns}loc"):
            if not sm.text:
                continue

            nested = request_text(sm.text.strip())

            if nested:
                urls.extend(parse_sitemap_urlset(nested))
    else:
        for loc in root.findall(f".//{ns}url/{ns}loc"):
            if loc.text:
                urls.append(loc.text.strip())

    return urls


def score_url(url: str) -> tuple[int, list[str], list[str]]:
    score = 0
    topics = set()
    reasons = []

    domain = registrable_domain(url)
    path = path_of(url)

    for suffix, points in TRUST_HINTS.items():
        if domain.endswith(suffix):
            score += points
            reasons.append(f"Domain has trust hint {suffix}")

    if GOOD_PATH_TERMS.search(path):
        score += 20
        reasons.append("URL path looks relevant")

    if TRANS_TERMS.search(url):
        score += 25
        reasons.append("URL contains trans/LGBTQ+ terms")

    if BAD_PATH_TERMS.search(path):
        score -= 30
        reasons.append("URL path looks low-value or commercial")

    if re.search(r"health|clinic|medical|hrt|hormone|dysphoria|care", url, re.I):
        topics.add("medical")

    if re.search(r"legal|rights|law|policy|recognition|name-change", url, re.I):
        topics.add("legal")

    if re.search(r"crisis|safety|hotline|violence|support", url, re.I):
        topics.add("safety")

    if re.search(r"research|report|study|paper", url, re.I):
        topics.add("research")

    return score, sorted(topics or {"general"}), reasons


def validate_candidate(name: str, base_url: str) -> Candidate | None:
    base_url = normalize_base(base_url)

    if not base_url:
        return None

    if not can_fetch(base_url, base_url):
        return None

    sitemaps = find_sitemaps(base_url)
    pages = []

    for sitemap_url in sitemaps[:5]:
        txt = request_text(sitemap_url)

        if not txt:
            continue

        urls = parse_sitemap_urlset(txt)

        for url in urls:
            if registrable_domain(url) != registrable_domain(base_url):
                continue

            if BAD_PATH_TERMS.search(path_of(url)):
                continue

            score, topics, reasons = score_url(url)

            if score >= 30:
                pages.append(
                    {
                        "url": url,
                        "score": score,
                        "topics": topics,
                        "reasons": reasons,
                    }
                )

    pages = sorted(pages, key=lambda x: x["score"], reverse=True)[:30]

    if not pages:
        return None

    best_score = pages[0]["score"]
    all_topics = sorted({topic for page in pages for topic in page["topics"]})

    allowed_paths = sorted(
        {
            "/" + path_of(page["url"]).strip("/").split("/")[0] + "/"
            for page in pages
            if path_of(page["url"]) != "/"
        }
    )[:12]

    return Candidate(
        name=name[:120],
        base_url=base_url,
        sitemap_urls=sitemaps,
        allowed_paths=allowed_paths or ["/"],
        blocked_paths=[
            "/donate/",
            "/shop/",
            "/store/",
            "/cart/",
            "/checkout/",
            "/events/",
            "/tag/",
            "/category/",
            "/author/",
            "/feed/",
        ],
        topic=all_topics,
        region=["unknown"],
        trust_level="candidate_auto_discovered",
        review_status="manual_review_needed",
        last_checked=dt.date.today().isoformat(),
        discovery={
            "method": "wikidata_wikipedia_sitemap_discovery",
            "status": "ok",
            "score": best_score,
            "example_url": pages[0]["url"],
            "reasons": pages[0]["reasons"],
        },
        candidate_pages=pages,
    )


def dedupe_sources(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen = set()
    out = []

    for name, url in pairs:
        base = normalize_base(url)

        if not base:
            continue

        domain = registrable_domain(base)

        if domain in seen:
            continue

        seen.add(domain)
        out.append((name, base))

    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing", default="sources.yml")
    parser.add_argument("--out", default="candidate_sources.yml")
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument(
        "--skip-wikidata",
        action="store_true",
        help="Skip Wikidata SPARQL discovery.",
    )
    parser.add_argument(
        "--skip-wikipedia",
        action="store_true",
        help="Skip Wikipedia external-link discovery.",
    )

    args = parser.parse_args()

    existing = load_existing_domains(args.existing)

    discovered = []

    seeds = [(domain, "https://" + domain) for domain in SEED_DOMAINS]
    discovered.extend(seeds)

    if not args.skip_wikidata:
        try:
            discovered.extend(discover_from_wikidata(limit=args.limit))
        except Exception as e:
            print(f"  ! Wikidata discovery crashed and was skipped: {e}")
    else:
        print("  ! Wikidata discovery skipped by flag")

    if not args.skip_wikipedia:
        try:
            discovered.extend(discover_from_wikipedia())
        except Exception as e:
            print(f"  ! Wikipedia discovery crashed and was skipped: {e}")
    else:
        print("  ! Wikipedia discovery skipped by flag")

    pairs = [
        (name, base)
        for name, base in dedupe_sources(discovered)
        if registrable_domain(base) not in existing
    ]

    candidates = []

    for i, (name, base) in enumerate(pairs, start=1):
        print(f"[{i}/{len(pairs)}] checking {base}")

        try:
            candidate = validate_candidate(name, base)

            if candidate:
                candidates.append(asdict(candidate))
                print(
                    "  + candidate: "
                    f"{candidate.discovery['score']} "
                    f"{candidate.discovery['example_url']}"
                )
            else:
                print("  - no useful sitemap pages")

        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break

        except Exception as e:
            print(f"  ! error: {e}")

    candidates = sorted(
        candidates,
        key=lambda candidate: candidate["discovery"]["score"],
        reverse=True,
    )

    with open(args.out, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            candidates,
            f,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )

    print(f"\nWrote {len(candidates)} candidates to {args.out}")


if __name__ == "__main__":
    main()