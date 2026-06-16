#!/usr/bin/env python3
"""
GUI for manually adding a Trustworthy source to sources.yml from a sitemap.

Adds:
- Build YAML preview from sitemap
- Export discovered pages as CSV for ChatGPT topic selection
- Import topic CSV back into the preview
- Append final entry to sources.yml

Dependency:
    pip install pyyaml

Run:
    python gui_add_source_from_sitemap.py
"""

from __future__ import annotations

import csv
import gzip
import io
import queue
import threading
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as exc:
    raise SystemExit("Tkinter is not available in this Python install.") from exc

try:
    import yaml
except ImportError as exc:
    raise SystemExit("Missing dependency: PyYAML. Install it with: pip install pyyaml") from exc


DEFAULT_BLOCKED_PATHS = [
    "/donate/",
    "/donation/",
    "/shop/",
    "/store/",
    "/events/",
    "/event/",
    "/blog/",
    "/news/",
    "/tag/",
    "/category/",
    "/author/",
    "/wp-json/",
    "/feed/",
]

DEFAULT_INCLUDE_HINTS = "/resources/, /reports/, /publications/, /guidance/, /research/"
DEFAULT_TOPICS = "legal, medical, civil_rights, human_rights, policy"
DEFAULT_REGIONS = "global"

CSV_COLUMNS = [
    "include",
    "url",
    "path",
    "topics",
    "region",
    "trust_level",
    "notes",
]


@dataclass
class PageRow:
    include: bool
    url: str
    path: str
    topics: list[str] = field(default_factory=list)
    region: list[str] = field(default_factory=list)
    trust_level: str = ""
    notes: str = ""


@dataclass
class SourceBuildResult:
    entry: dict[str, Any]
    page_rows: list[PageRow]
    sitemap_count: int
    page_count: int
    allowed_count: int
    skipped_external: int
    skipped_blocked: int
    skipped_include: int


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
    return url.rstrip("/")


def csvish(value: str) -> list[str]:
    pieces: list[str] = []
    for raw in value.replace("\n", ",").replace(";", ",").split(","):
        item = raw.strip()
        if item:
            pieces.append(item)
    return dedupe_keep_order(pieces)


def ensure_slash_path(path: str) -> str:
    path = path.strip()
    if not path:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return path


def parse_paths(value: str) -> list[str]:
    return [p for p in (ensure_slash_path(item) for item in csvish(value)) if p]


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    output = []
    for item in items:
        if item not in seen:
            seen.add(item)
            output.append(item)
    return output


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "trusted-source-sitemap-gui/1.1"})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read()
        content_encoding = response.headers.get("Content-Encoding", "").lower()
    if url.lower().endswith(".gz") or content_encoding == "gzip":
        data = gzip.decompress(data)
    return data


def xml_locs(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    locs = []
    for elem in root.iter():
        if elem.tag.endswith("loc") and elem.text:
            locs.append(elem.text.strip())
    return locs


def looks_like_sitemap(url: str) -> bool:
    lower = url.lower()
    return lower.endswith(".xml") or lower.endswith(".xml.gz") or "sitemap" in lower


def crawl_sitemap(sitemap_url: str, max_sitemaps: int = 50) -> tuple[list[str], list[str]]:
    seen_sitemaps = set()
    sitemap_queue = [sitemap_url]
    discovered_sitemaps: list[str] = []
    page_urls: list[str] = []

    while sitemap_queue and len(seen_sitemaps) < max_sitemaps:
        current = sitemap_queue.pop(0)
        if current in seen_sitemaps:
            continue
        seen_sitemaps.add(current)

        xml = fetch_url(current)
        locs = xml_locs(xml)
        discovered_sitemaps.append(current)

        for loc in locs:
            if looks_like_sitemap(loc):
                if loc not in seen_sitemaps and loc not in sitemap_queue:
                    sitemap_queue.append(loc)
            else:
                page_urls.append(loc)

    return discovered_sitemaps, dedupe_keep_order(page_urls)


def same_domain(url: str, base_url: str) -> bool:
    url_host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    base_host = urllib.parse.urlparse(base_url).netloc.lower().removeprefix("www.")
    return bool(url_host and base_host and url_host == base_host)


def path_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and not path.endswith("/"):
        path += "/"
    return path


def is_blocked(path: str, blocked_paths: list[str]) -> bool:
    return any(path.startswith(blocked) for blocked in blocked_paths)


def passes_include(path: str, include_paths: list[str]) -> bool:
    if not include_paths:
        return True
    return any(fragment in path for fragment in include_paths)


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "include", "keep", "x"}


def falsey(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "n", "exclude", "skip"}


def source_already_exists(yml_path: Path, name: str, base_url: str) -> bool:
    if not yml_path.exists():
        return False

    with yml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []

    if not isinstance(data, list):
        raise ValueError("Expected sources.yml to contain a top-level YAML list.")

    wanted_name = name.strip().lower()
    wanted_base = normalize_base_url(base_url)

    for item in data:
        if not isinstance(item, dict):
            continue
        existing_name = str(item.get("name", "")).strip().lower()
        existing_base = normalize_base_url(str(item.get("base_url", "")))
        if existing_name == wanted_name or existing_base == wanted_base:
            return True

    return False


def append_source(yml_path: Path, entry: dict[str, Any]) -> None:
    rendered = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, default_flow_style=False, width=120)
    with yml_path.open("a", encoding="utf-8") as f:
        f.write("\n")
        f.write(rendered)


def build_entry_from_rows(
    *,
    name: str,
    base_url: str,
    sitemap_urls: list[str],
    page_rows: list[PageRow],
    blocked_paths: list[str],
    fallback_topics: list[str],
    fallback_regions: list[str],
    trust_level: str,
    candidate_pages_count: int,
) -> dict[str, Any]:
    included_rows = [row for row in page_rows if row.include]
    if not included_rows:
        raise ValueError("No rows are marked include=yes.")

    allowed_paths = dedupe_keep_order([row.path for row in included_rows])

    topic_union: list[str] = []
    region_union: list[str] = []
    for row in included_rows:
        topic_union.extend(row.topics)
        region_union.extend(row.region)

    topics = dedupe_keep_order(topic_union) or fallback_topics or ["human_rights"]
    regions = dedupe_keep_order(region_union) or fallback_regions or ["global"]

    candidate_pages = []
    for row in included_rows[:candidate_pages_count]:
        candidate_pages.append(
            {
                "url": row.url,
                "score": 100,
                "topics": row.topics or topics,
                "reasons": [
                    "Manually added from sitemap",
                    "Marked Trustworthy by manual review",
                ],
            }
        )

    entry: dict[str, Any] = {
        "name": name.strip(),
        "base_url": normalize_base_url(base_url),
        "sitemap_urls": sitemap_urls,
        "allowed_paths": allowed_paths,
        "blocked_paths": blocked_paths,
        "topic": topics,
        "region": regions,
        "trust_level": trust_level.strip() or "verified_org_candidate",
        "review_status": "Trustworthy",
        "last_checked": date.today().isoformat(),
        "discovery": {
            "method": "manual_sitemap_import_gui",
            "status": "ok",
            "score": 100,
            "example_url": included_rows[0].url,
            "reasons": [
                "Manually added from sitemap",
                "Marked Trustworthy by manual review",
            ],
        },
    }

    if candidate_pages:
        entry["candidate_pages"] = candidate_pages

    return entry


def build_source_entry(
    *,
    yml_path: Path,
    name: str,
    base_url: str,
    sitemap_url: str,
    include_paths: list[str],
    blocked_paths: list[str],
    topics: list[str],
    regions: list[str],
    trust_level: str,
    max_paths: int,
    candidate_pages_count: int,
) -> SourceBuildResult:
    del yml_path

    name = name.strip()
    base_url = normalize_base_url(base_url)
    sitemap_url = sitemap_url.strip()
    trust_level = trust_level.strip() or "verified_org_candidate"

    if not name:
        raise ValueError("Source name is required.")
    if not base_url:
        raise ValueError("Base URL is required.")
    if not sitemap_url:
        raise ValueError("Sitemap URL is required.")
    if max_paths < 1:
        raise ValueError("Max allowed paths must be at least 1.")

    sitemap_urls, page_urls = crawl_sitemap(sitemap_url)

    page_rows: list[PageRow] = []
    allowed_count = 0
    skipped_external = 0
    skipped_blocked = 0
    skipped_include = 0

    for url in page_urls:
        if not same_domain(url, base_url):
            skipped_external += 1
            continue

        path = path_from_url(url)
        blocked = is_blocked(path, blocked_paths)
        included_by_filter = passes_include(path, include_paths)
        include = (not blocked) and included_by_filter and allowed_count < max_paths

        if blocked:
            skipped_blocked += 1
        elif not included_by_filter:
            skipped_include += 1

        if include:
            allowed_count += 1

        page_rows.append(
            PageRow(
                include=include,
                url=urllib.parse.urljoin(base_url + "/", path.lstrip("/")),
                path=path,
                topics=topics,
                region=regions,
                trust_level=trust_level,
                notes="",
            )
        )

    if not any(row.include for row in page_rows):
        raise ValueError(
            "No allowed paths were found. Try clearing Include paths, increasing Max allowed paths, "
            "or using a broader include path such as /resources/."
        )

    entry = build_entry_from_rows(
        name=name,
        base_url=base_url,
        sitemap_urls=sitemap_urls,
        page_rows=page_rows,
        blocked_paths=blocked_paths,
        fallback_topics=topics,
        fallback_regions=regions,
        trust_level=trust_level,
        candidate_pages_count=candidate_pages_count,
    )

    return SourceBuildResult(
        entry=entry,
        page_rows=page_rows,
        sitemap_count=len(sitemap_urls),
        page_count=len(page_urls),
        allowed_count=sum(1 for row in page_rows if row.include),
        skipped_external=skipped_external,
        skipped_blocked=skipped_blocked,
        skipped_include=skipped_include,
    )


def page_rows_to_csv(page_rows: list[PageRow], included_only: bool) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()

    for row in page_rows:
        if included_only and not row.include:
            continue
        writer.writerow(
            {
                "include": "yes" if row.include else "no",
                "url": row.url,
                "path": row.path,
                "topics": ", ".join(row.topics),
                "region": ", ".join(row.region),
                "trust_level": row.trust_level,
                "notes": row.notes,
            }
        )

    return output.getvalue()


def read_topic_csv(text: str) -> list[dict[str, str]]:
    sample = text.strip()
    if not sample:
        raise ValueError("CSV is empty.")
    reader = csv.DictReader(io.StringIO(sample))
    if not reader.fieldnames:
        raise ValueError("CSV must have a header row.")
    return [{str(k).strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def get_first(row: dict[str, str], names: list[str]) -> str:
    lowered = {k.lower().strip(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return ""


def merge_topic_csv_into_result(
    result: SourceBuildResult,
    csv_text: str,
    *,
    name: str,
    base_url: str,
    blocked_paths: list[str],
    fallback_topics: list[str],
    fallback_regions: list[str],
    trust_level: str,
    candidate_pages_count: int,
) -> SourceBuildResult:
    rows = read_topic_csv(csv_text)

    existing_by_url = {row.url.rstrip("/"): row for row in result.page_rows}
    existing_by_path = {row.path: row for row in result.page_rows}
    new_rows: list[PageRow] = []

    for raw in rows:
        url = get_first(raw, ["url", "page", "page_url"])
        path = get_first(raw, ["path", "allowed_path"])

        if not path and url:
            path = path_from_url(url)
        if not url and path:
            url = urllib.parse.urljoin(normalize_base_url(base_url) + "/", path.lstrip("/"))

        if not path or not url:
            continue

        path = path_from_url(url) if url else ensure_slash_path(path)
        url = urllib.parse.urljoin(normalize_base_url(base_url) + "/", path.lstrip("/"))

        original = existing_by_url.get(url.rstrip("/")) or existing_by_path.get(path)

        include_raw = get_first(raw, ["include", "keep", "use", "selected"])
        if include_raw:
            include = truthy(include_raw) or not falsey(include_raw)
        elif original:
            include = original.include
        else:
            include = True

        topics = csvish(get_first(raw, ["topics", "topic", "suggested_topics", "tags"]))
        regions = csvish(get_first(raw, ["region", "regions"]))
        row_trust_level = get_first(raw, ["trust_level", "trust level"]) or trust_level
        notes = get_first(raw, ["notes", "note", "reason", "reasons"])

        if original:
            if not topics:
                topics = original.topics
            if not regions:
                regions = original.region
            if not notes:
                notes = original.notes

        new_rows.append(
            PageRow(
                include=include,
                url=url,
                path=path,
                topics=topics or fallback_topics,
                region=regions or fallback_regions,
                trust_level=row_trust_level,
                notes=notes,
            )
        )

    if not new_rows:
        raise ValueError("No usable rows found. CSV needs at least url or path columns.")

    entry = build_entry_from_rows(
        name=name,
        base_url=base_url,
        sitemap_urls=result.entry.get("sitemap_urls", []),
        page_rows=new_rows,
        blocked_paths=blocked_paths,
        fallback_topics=fallback_topics,
        fallback_regions=fallback_regions,
        trust_level=trust_level,
        candidate_pages_count=candidate_pages_count,
    )

    return SourceBuildResult(
        entry=entry,
        page_rows=new_rows,
        sitemap_count=result.sitemap_count,
        page_count=len(new_rows),
        allowed_count=sum(1 for row in new_rows if row.include),
        skipped_external=result.skipped_external,
        skipped_blocked=result.skipped_blocked,
        skipped_include=result.skipped_include,
    )


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Trusted Source Sitemap Importer")
        self.geometry("1040x790")
        self.minsize(900, 680)

        self.result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.latest_result: SourceBuildResult | None = None
        self.worker: threading.Thread | None = None

        self.yml_path = tk.StringVar(value="sources.yml")
        self.name = tk.StringVar()
        self.base_url = tk.StringVar()
        self.sitemap_url = tk.StringVar()
        self.include_paths = tk.StringVar(value=DEFAULT_INCLUDE_HINTS)
        self.blocked_paths = tk.StringVar(value=", ".join(DEFAULT_BLOCKED_PATHS))
        self.topics = tk.StringVar(value=DEFAULT_TOPICS)
        self.regions = tk.StringVar(value=DEFAULT_REGIONS)
        self.trust_level = tk.StringVar(value="verified_org_candidate")
        self.max_paths = tk.IntVar(value=50)
        self.candidate_pages_count = tk.IntVar(value=20)
        self.export_included_only = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Ready.")

        self._setup_style()
        self._build_ui()
        self.after(100, self._poll_queue)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background="#f7f7f5")
        style.configure("TLabel", background="#f7f7f5", foreground="#1f2328", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Muted.TLabel", foreground="#60646c")
        style.configure("TButton", padding=(10, 6))
        style.configure("Accent.TButton", padding=(12, 7))
        style.configure("TEntry", padding=5)
        style.configure("TLabelframe", background="#f7f7f5")
        style.configure("TLabelframe.Label", background="#f7f7f5", foreground="#1f2328", font=("Segoe UI", 10, "bold"))
        style.configure("TCheckbutton", background="#f7f7f5", foreground="#1f2328")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="Trusted Source Sitemap Importer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Build YAML, export pages as CSV for topic-picking, then import the CSV back.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        body = ttk.PanedWindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, padding=(0, 0, 12, 0))
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=1)

        self._build_form(left)
        self._build_preview(right)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        self.progress = ttk.Progressbar(footer, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").pack(side="right")

    def _build_form(self, parent: ttk.Frame) -> None:
        file_box = ttk.LabelFrame(parent, text="File")
        file_box.pack(fill="x", pady=(0, 10))
        file_row = ttk.Frame(file_box, padding=10)
        file_row.pack(fill="x")
        ttk.Entry(file_row, textvariable=self.yml_path).pack(side="left", fill="x", expand=True)
        ttk.Button(file_row, text="Browse", command=self._browse_file).pack(side="left", padx=(8, 0))

        source_box = ttk.LabelFrame(parent, text="Source")
        source_box.pack(fill="x", pady=(0, 10))
        grid = ttk.Frame(source_box, padding=10)
        grid.pack(fill="x")
        self._field(grid, "Name", self.name, 0)
        self._field(grid, "Base URL", self.base_url, 1)
        self._field(grid, "Sitemap URL", self.sitemap_url, 2)
        self._field(grid, "Trust level", self.trust_level, 3)

        filters_box = ttk.LabelFrame(parent, text="Filters")
        filters_box.pack(fill="x", pady=(0, 10))
        filters = ttk.Frame(filters_box, padding=10)
        filters.pack(fill="x")
        self._field(filters, "Include paths", self.include_paths, 0)
        self._field(filters, "Blocked paths", self.blocked_paths, 1)
        self._field(filters, "Default topics", self.topics, 2)
        self._field(filters, "Default regions", self.regions, 3)

        number_row = ttk.Frame(filters)
        number_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(number_row, text="Max allowed paths").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Spinbox(number_row, from_=1, to=1000, textvariable=self.max_paths, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(number_row, text="Candidate pages").grid(row=0, column=2, sticky="w", padx=(18, 8))
        ttk.Spinbox(number_row, from_=0, to=500, textvariable=self.candidate_pages_count, width=8).grid(row=0, column=3, sticky="w")

        ttk.Label(
            filters,
            text="Leave Include paths empty to accept all sitemap pages except blocked paths.",
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        actions = ttk.LabelFrame(parent, text="Actions")
        actions.pack(fill="x")
        actions_inner = ttk.Frame(actions, padding=10)
        actions_inner.pack(fill="x")

        ttk.Button(actions_inner, text="Build Preview", style="Accent.TButton", command=self.build_preview).grid(row=0, column=0, sticky="w")
        ttk.Button(actions_inner, text="Append to YAML", command=self.append_to_yaml).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(actions_inner, text="Clear Preview", command=self.clear_preview).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Separator(actions_inner).grid(row=1, column=0, columnspan=3, sticky="ew", pady=12)

        ttk.Checkbutton(actions_inner, text="Export included pages only", variable=self.export_included_only).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Button(actions_inner, text="Copy Page CSV", command=self.copy_pages_csv).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Button(actions_inner, text="Save Page CSV", command=self.save_pages_csv).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(actions_inner, text="Import CSV from Clipboard", command=self.import_csv_from_clipboard).grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Button(actions_inner, text="Import CSV from File", command=self.import_csv_from_file).grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Label(
            actions_inner,
            text="CSV columns: include, url, path, topics, region, trust_level, notes",
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _build_preview(self, parent: ttk.Frame) -> None:
        preview_box = ttk.LabelFrame(parent, text="Preview")
        preview_box.pack(fill="both", expand=True)

        self.preview = tk.Text(
            preview_box,
            wrap="none",
            undo=False,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#d7d7d2",
            font=("Consolas", 10),
        )
        self.preview.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)

        yscroll = ttk.Scrollbar(preview_box, orient="vertical", command=self.preview.yview)
        yscroll.pack(side="right", fill="y", pady=10, padx=(0, 10))
        self.preview.configure(yscrollcommand=yscroll.set)

        xscroll = ttk.Scrollbar(parent, orient="horizontal", command=self.preview.xview)
        xscroll.pack(fill="x", padx=10)
        self.preview.configure(xscrollcommand=xscroll.set)

    def _field(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int) -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=5)

    def _browse_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose sources.yml",
            filetypes=[("YAML files", "*.yml *.yaml"), ("All files", "*.*")],
        )
        if filename:
            self.yml_path.set(filename)

    def _form_values(self) -> dict[str, Any]:
        return {
            "yml_path": Path(self.yml_path.get()).expanduser(),
            "name": self.name.get(),
            "base_url": self.base_url.get(),
            "sitemap_url": self.sitemap_url.get(),
            "include_paths": parse_paths(self.include_paths.get()),
            "blocked_paths": parse_paths(self.blocked_paths.get()) or DEFAULT_BLOCKED_PATHS,
            "topics": csvish(self.topics.get()) or ["human_rights"],
            "regions": csvish(self.regions.get()) or ["global"],
            "trust_level": self.trust_level.get(),
            "max_paths": int(self.max_paths.get()),
            "candidate_pages_count": int(self.candidate_pages_count.get()),
        }

    def _render_preview(self, result: SourceBuildResult, status: str) -> None:
        self.clear_preview()
        rendered = yaml.safe_dump([result.entry], sort_keys=False, allow_unicode=True, default_flow_style=False, width=120)
        summary = (
            f"# Sitemaps read: {result.sitemap_count}\n"
            f"# URLs found: {result.page_count}\n"
            f"# Included pages: {result.allowed_count}\n"
            f"# Skipped external: {result.skipped_external}\n"
            f"# Skipped blocked: {result.skipped_blocked}\n"
            f"# Skipped by include filter: {result.skipped_include}\n\n"
        )
        self.preview.insert("1.0", summary + rendered)
        self.status.set(status)

    def _set_busy(self, busy: bool, text: str | None = None) -> None:
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()
        if text:
            self.status.set(text)

    def build_preview(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A sitemap import is already running.")
            return

        try:
            values = self._form_values()
        except Exception as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        self.latest_result = None
        self.clear_preview()
        self._set_busy(True, "Reading sitemap...")
        self.worker = threading.Thread(target=self._build_worker, args=(values,), daemon=True)
        self.worker.start()

    def _build_worker(self, values: dict[str, Any]) -> None:
        try:
            result = build_source_entry(**values)
            self.result_queue.put(("success", result))
        except Exception as exc:
            self.result_queue.put(("error", (exc, traceback.format_exc())))

    def _poll_queue(self) -> None:
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_queue)
            return

        self._set_busy(False)

        if kind == "success":
            result: SourceBuildResult = payload
            self.latest_result = result
            self._render_preview(result, "Preview built.")
        else:
            exc, tb = payload
            self.preview.insert("1.0", tb)
            self.status.set("Error.")
            messagebox.showerror("Could not build source", str(exc))

        self.after(100, self._poll_queue)

    def require_result(self) -> SourceBuildResult | None:
        if self.latest_result is None:
            messagebox.showinfo("No preview", "Build a preview first.")
            return None
        return self.latest_result

    def copy_pages_csv(self) -> None:
        result = self.require_result()
        if result is None:
            return
        text = page_rows_to_csv(result.page_rows, self.export_included_only.get())
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set("Page CSV copied to clipboard.")
        messagebox.showinfo("Copied", "Page CSV copied to clipboard. Paste it into ChatGPT for topic selection.")

    def save_pages_csv(self) -> None:
        result = self.require_result()
        if result is None:
            return
        filename = filedialog.asksaveasfilename(
            title="Save page CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return
        text = page_rows_to_csv(result.page_rows, self.export_included_only.get())
        Path(filename).write_text(text, encoding="utf-8", newline="")
        self.status.set("Page CSV saved.")

    def import_csv_from_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showerror("Clipboard empty", "Clipboard does not contain text.")
            return
        self.import_topic_csv_text(text)

    def import_csv_from_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="Import topic CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return
        text = Path(filename).read_text(encoding="utf-8")
        self.import_topic_csv_text(text)

    def import_topic_csv_text(self, text: str) -> None:
        result = self.require_result()
        if result is None:
            return
        try:
            values = self._form_values()
            merged = merge_topic_csv_into_result(
                result,
                text,
                name=values["name"],
                base_url=values["base_url"],
                blocked_paths=values["blocked_paths"],
                fallback_topics=values["topics"],
                fallback_regions=values["regions"],
                trust_level=values["trust_level"],
                candidate_pages_count=values["candidate_pages_count"],
            )
        except Exception as exc:
            messagebox.showerror("Could not import CSV", str(exc))
            return

        self.latest_result = merged
        self._render_preview(merged, "Topic CSV imported.")
        messagebox.showinfo("Imported", "Topic CSV imported into the YAML preview.")

    def append_to_yaml(self) -> None:
        result = self.require_result()
        if result is None:
            return

        yml_path = Path(self.yml_path.get()).expanduser()
        entry = result.entry

        try:
            if source_already_exists(yml_path, entry["name"], entry["base_url"]):
                messagebox.showwarning("Already exists", "A source with this name or base URL already exists in the YAML file.")
                return
            append_source(yml_path, entry)
        except Exception as exc:
            messagebox.showerror("Could not append", str(exc))
            return

        self.status.set("Added to YAML.")
        messagebox.showinfo("Done", f"Added {entry['name']} to {yml_path}")

    def clear_preview(self) -> None:
        self.preview.delete("1.0", "end")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
