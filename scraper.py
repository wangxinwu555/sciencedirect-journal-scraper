"""Extract ScienceDirect titles and complete abstract text without an AI model.

Python 3.10+, standard library only. Blocked HTTP pages fail explicitly.
A saved, normally accessible browser HTML file is the supported fallback.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
BLOCK = set("div p section li br h1 h2 h3 h4 h5 h6".split())


class ExtractionError(ValueError):
    pass


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.walk()

    def text(self, skip_headings=False):
        if self.tag in {"script", "style", "noscript"}:
            return ""
        if skip_headings and self.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return ""
        pieces = [c.text(skip_headings) if isinstance(c, Element) else c for c in self.children]
        value = "".join(pieces)
        return f" {value} " if self.tag in BLOCK else value


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Element(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean(value):
    return " ".join(value.split())


def validate_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"sciencedirect.com", "www.sciencedirect.com"}:
        raise ExtractionError("Use an HTTPS ScienceDirect article URL.")
    if parts.username or parts.password or parts.port not in {None, 443}:
        raise ExtractionError("Credentials and nonstandard ports are not supported.")
    match = re.fullmatch(r"/(?:org/)?science/article/pii/([A-Za-z0-9]+)", parts.path.rstrip("/"))
    if not match:
        raise ExtractionError("Expected a ScienceDirect /science/article/pii/... article URL.")
    return match.group(1)


@dataclass
class Article:
    title: str
    abstract: str
    journal: str
    doi: str
    pii: str
    year: str
    volume: str
    pages: str
    source_url: str
    license_url: str
    capture_method: str
    captured_at: str
    extracted_at: str
    source_sha256: str


def parse_article(content: str, source_url: str, *, method="saved-html", captured_at="", source_bytes=None):
    pii = validate_url(source_url)
    doc = Document()
    doc.feed(content)
    nodes = list(doc.root.walk())
    meta = {n.attrs.get("name", "").lower(): n.attrs.get("content", "") for n in nodes if n.tag == "meta"}
    title_node = next((n for n in nodes if n.attrs.get("id") == "screen-reader-main-title"), None)
    title = clean(title_node.text()) if title_node else clean(meta.get("citation_title", ""))
    # Only article abstract containers; never a recommendation, search snippet or meta description.
    abstract_node = next((n for n in nodes if n.attrs.get("id") == "abs0001"), None)
    if not abstract_node:
        abstract_node = next((n for n in nodes if n.tag in {"div", "section"}
                              and "abstract" in n.attrs.get("class", "").split()
                              and any(c.tag == "h2" and clean(c.text()).lower() == "abstract" for c in n.walk())), None)
    abstract = clean(abstract_node.text(skip_headings=True)) if abstract_node else ""
    if not title or not abstract:
        raise ExtractionError("Missing article title or complete abstract. This may be a blocked/login page; no result was saved.")
    if len(abstract.split()) < 12:
        raise ExtractionError("Abstract is too short to validate as article content.")
    meta_pii = meta.get("citation_pii", "")
    if meta_pii and meta_pii != pii:
        raise ExtractionError("Saved page PII does not match the requested article URL.")
    doi = clean(meta.get("citation_doi", "") or meta.get("dc.identifier", ""))
    license_url = next((n.attrs.get("href", "") for n in nodes if n.tag == "a"
                        and urlsplit(n.attrs.get("href", "")).hostname == "creativecommons.org"), "")
    first, last = meta.get("citation_firstpage", ""), meta.get("citation_lastpage", "")
    return Article(title, abstract, clean(meta.get("citation_journal_title", "")), doi, pii,
                   meta.get("citation_publication_date", "")[:4], meta.get("citation_volume", ""),
                   f"{first}–{last}" if first and last else first, source_url, license_url, method,
                   captured_at, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   hashlib.sha256(source_bytes if source_bytes is not None else content.encode("utf-8")).hexdigest())


def fetch_article(url, timeout=30):
    validate_url(url)
    request = Request(url, headers={"User-Agent": "JournalMetadataDemo/1.0 (single-article research demonstration)",
                                   "Accept": "text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            validate_url(response.geturl())
            raw = response.read(10_000_001)
            if len(raw) > 10_000_000:
                raise ExtractionError("Page exceeds the 10 MB limit.")
            if "html" not in response.headers.get("Content-Type", ""):
                raise ExtractionError("Expected an HTML article response.")
            return raw
    except HTTPError as e:
        if e.code in {401, 403, 429}:
            raise ExtractionError(f"HTTP {e.code}: access blocked or rate limited. Open normally in a browser and save HTML; rerun with --html. No bypass or simulated result.") from e
        raise ExtractionError(f"HTTP {e.code}: article fetch failed.") from e
    except (URLError, TimeoutError, OSError) as e:
        raise ExtractionError(f"Network request failed ({type(e).__name__}). Check your connection or use --html.") from e


def safe_csv(value):
    # Avoid a formula executing when a downloaded CSV is opened in Excel.
    return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value


def export(articles, destination, *, evidence=None, provenance=None):
    destination.mkdir(parents=True, exist_ok=True)
    data = [asdict(a) for a in articles]
    (destination / "articles.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (destination / "articles.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows({k: safe_csv(v) for k, v in row.items()} for row in data)
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    cards = []
    for i, a in enumerate(articles, 1):
        e = html.escape
        doi_link = f"https://doi.org/{a.doi}" if re.fullmatch(r"10\.\d{4,9}/\S+", a.doi) else ""
        doi = f'<a href="{e(doi_link, quote=True)}" target="_blank" rel="noopener noreferrer">{e(a.doi)} ↗</a>' if doi_link else "未提供"
        license_link = f'<a href="{e(a.license_url, quote=True)}" target="_blank" rel="noopener noreferrer">原文许可 ↗</a>' if a.license_url else "见原文"
        method_label = {"browser-dom": "浏览器页面采集", "saved-html": "已保存网页解析", "http": "HTTP 页面抓取"}.get(a.capture_method, a.capture_method)
        cards.append(f'''<article class="paper" data-paper>
<div class="paper-no">{i:02d}</div><div class="paper-body">
<div class="eyebrow">{e(a.journal)} · {e(a.year)} · VOL. {e(a.volume)}</div>
<h2 lang="en">{e(a.title)}</h2>
<div class="paper-links"><a href="{e(a.source_url, quote=True)}" target="_blank" rel="noopener noreferrer">查看 ScienceDirect 原文 ↗</a><span>{e(method_label)}</span></div>
<div class="abstract-label">ABSTRACT <span>英文原文</span></div>
<p class="abstract" lang="en">{e(a.abstract)}</p>
<details><summary>来源与采集记录</summary><dl>
<dt>DOI</dt><dd>{doi}</dd><dt>PII</dt><dd>{e(a.pii)}</dd>
<dt>页码</dt><dd>{e(a.pages)}</dd><dt>采集时间（UTC）</dt><dd>{e(a.captured_at or '未记录')}</dd>
<dt>解析时间（UTC）</dt><dd>{e(a.extracted_at)}</dd><dt>许可</dt><dd>{license_link}</dd>
<dt>来源 SHA-256</dt><dd class="hash">{e(a.source_sha256)}</dd></dl></details>
</div></article>''')
    evidence_links = []
    for source, name, label in [(evidence, "article.html", "查看来源片段"), (provenance, "capture.json", "查看采集记录")]:
        if source is not None:
            folder = destination / "evidence"
            folder.mkdir(exist_ok=True)
            shutil.copy2(source, folder / name)
            evidence_links.append(f'<a href="evidence/{name}">{label}</a>')
    page = (template.replace("{{CARDS}}", "\n".join(cards)).replace("{{COUNT}}", str(len(articles)))
            .replace("{{EVIDENCE}}", " · ".join(evidence_links)))
    (destination / "index.html").write_text(page, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--url", action="append", help="ScienceDirect article URL (repeat for multiple articles)")
    inputs.add_argument("--html", type=Path, help="Saved article HTML or browser DOM excerpt")
    parser.add_argument("--source-url", help="Original article URL; required with --html")
    parser.add_argument("--provenance", type=Path, help="Capture record JSON associated with --html")
    parser.add_argument("--out", type=Path, default=ROOT / "docs")
    parser.add_argument("--delay", type=float, default=2, help="Delay between HTTP article requests, seconds (minimum 1)")
    args = parser.parse_args(argv)
    try:
        articles = []
        if args.html:
            if not args.source_url:
                parser.error("--source-url is required with --html")
            record = json.loads(args.provenance.read_text(encoding="utf-8")) if args.provenance else {}
            if record.get("source_url") and record["source_url"] != args.source_url:
                raise ExtractionError("Capture record URL does not match --source-url.")
            raw = args.html.read_bytes()
            articles.append(parse_article(raw.decode("utf-8-sig"), args.source_url,
                            method=record.get("capture_method", "saved-html"),
                            captured_at=record.get("captured_at", ""), source_bytes=raw))
        else:
            for i, url in enumerate(dict.fromkeys(args.url)):
                if i:
                    time.sleep(max(1, args.delay))
                raw = fetch_article(url)
                captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                articles.append(parse_article(raw.decode("utf-8-sig"), url, method="http",
                                              captured_at=captured_at, source_bytes=raw))
        export(articles, args.out, evidence=args.html, provenance=args.provenance)
        print(f"OK: {len(articles)} article(s). Output: {args.out.resolve()}")
        return 0
    except (ExtractionError, OSError, UnicodeError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
