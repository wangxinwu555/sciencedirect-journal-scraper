"""Collect titles and abstracts directly from ScienceDirect browser pages.

Standard headed Chrome, normal JavaScript loading, persistent project profile.
Can attempt the standard visible Turnstile checkbox once per configured budget.
An uncleared CAPTCHA or access denial is a blocked run, never a successful one.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from scraper import ExtractionError, parse_article

ROOT = Path(__file__).resolve().parent
JOURNAL = "procedia-computer-science"
JOURNAL_NAME = "Procedia Computer Science"
ISSUES_URL = f"https://www.sciencedirect.com/journal/{JOURNAL}/issues"
ADMIN = {"contents", "preface", "front matter", "editorial board", "author index"}
CAPTCHA_CLICKS_REMAINING = 0


class AccessBlocked(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def require_website_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "www.sciencedirect.com" or
            parts.username or parts.password or parts.port not in (None, 443)):
        raise ValueError("Only HTTPS ScienceDirect pages are allowed")


def challenge_reason(text):
    lowered = text.casefold()
    if any(term in lowered for term in ("are you a robot", "captcha challenge", "verify you are human",
                                        "验证您是真人", "确认您是真人")):
        return "interactive-captcha"
    if "there was a problem providing the content" in lowered or "ip address has been blocked" in lowered:
        return "access-denied"
    return ""


def initialize(db, year):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, year INTEGER);
        CREATE TABLE IF NOT EXISTS volumes (url TEXT PRIMARY KEY, title TEXT, complete INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs (
            pii TEXT PRIMARY KEY, url TEXT, title TEXT, volume_url TEXT, status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0, error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS issue_pages (
            volume_url TEXT, page INTEGER, captured_at TEXT, row_count INTEGER,
            PRIMARY KEY(volume_url,page));
        CREATE TABLE IF NOT EXISTS articles (
            pii TEXT PRIMARY KEY, title TEXT, abstract TEXT, journal TEXT, doi TEXT, year TEXT,
            volume TEXT, source_url TEXT, captured_at TEXT, sha256 TEXT, raw_file TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS events (at TEXT, kind TEXT, url TEXT, detail TEXT);
    """)
    saved = db.execute("SELECT year FROM config WHERE id=1").fetchone()
    if saved and saved[0] != year:
        raise ValueError("This output database belongs to another year")
    db.execute("INSERT OR IGNORE INTO config VALUES (1, ?)", (year,))
    db.commit()


def evidence(page, output, kind, url):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    base = output / "diagnostics" / f"{stamp}-{kind}"
    # Diagnostics remain local: screenshot can contain access reference/IP details.
    page.screenshot(path=str(base.with_suffix(".png")))
    text = page.locator("body").inner_text()
    text = re.sub(r"IP Address:.*", "IP Address: [omitted]", text)
    data = {"at": now(), "kind": kind, "url": url, "title": page.title(), "visible_text": text[:5000]}
    base.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def attempt_turnstile_checkbox(page, db, output, url):
    """Operate the displayed standard checkbox, without fabricating verification tokens."""
    global CAPTCHA_CLICKS_REMAINING
    if CAPTCHA_CLICKS_REMAINING <= 0:
        return False
    frames = [frame for frame in page.frames if urlsplit(frame.url).hostname == "challenges.cloudflare.com"
              and "/turnstile/" in urlsplit(frame.url).path]
    if len(frames) != 1:
        return False
    frame = frames[0]
    evidence(page, output, "before-turnstile-attempt", url)
    checkbox = frame.get_by_role("checkbox")
    if checkbox.count() == 1 and checkbox.is_visible():
        CAPTCHA_CLICKS_REMAINING -= 1
        checkbox.click(timeout=10000)
    else:
        # Turnstile uses a closed shadow root in this environment. Its frame is the
        # visible 300x65 checkbox widget inspected in the saved diagnostic screenshot.
        element = frame.frame_element()
        bounds = element.bounding_box()
        if not bounds or not 250 <= bounds["width"] <= 400 or not 50 <= bounds["height"] <= 100:
            return False
        CAPTCHA_CLICKS_REMAINING -= 1
        element.click(position={"x":20,"y":bounds["height"]/2},timeout=10000)
    db.execute("INSERT INTO events VALUES (?,?,?,?)",(now(),"turnstile-checkbox-attempt",url,
               "Program operated standard visible checkbox; success still requires real article DOM."))
    db.commit()
    print("Program attempted the standard Turnstile checkbox; checking publisher content",flush=True)
    return True


def ready(page, db, output, url, locator, timeout):
    """Let website JS finish; a checkbox attempt is not itself verification success."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    try:
        locator.wait_for(state="visible", timeout=timeout * 1000)
        if challenge_reason(page.locator("body").inner_text()):
            raise PlaywrightTimeout("Verification page is not publisher content")
        require_website_url(page.url)
        return
    except PlaywrightTimeout as error:
        reason = challenge_reason(page.locator("body").inner_text())
        if reason == "interactive-captcha" and attempt_turnstile_checkbox(page,db,output,url):
            try:
                locator.wait_for(state="visible",timeout=timeout*1000)
                if challenge_reason(page.locator("body").inner_text()):
                    raise PlaywrightTimeout("Challenge remains after checkbox attempt")
                require_website_url(page.url)
                return
            except PlaywrightTimeout:
                evidence(page,output,"after-turnstile-attempt",url)
        if reason:
            evidence(page, output, reason, url)
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (now(), reason, url,
                       "Normal browser loading/standard checkbox handling did not clear the challenge."))
            db.commit()
            raise AccessBlocked(f"{reason}: {url}") from error
        raise


def navigate(page, db, output, url, selector, timeout):
    require_website_url(url)
    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    ready(page, db, output, url, page.locator(selector).first, timeout)
    dismiss_overlays(page)
    if response and response.status >= 400:
        # A browser-side redirect may clear an initial JS challenge. Accept only real content.
        if challenge_reason(page.locator("body").inner_text()):
            raise AccessBlocked(f"HTTP {response.status}: {url}")


def dismiss_overlays(page, settle_seconds=3):
    """Handle observed cookie choice/welcome UI, including overlays loaded after the page."""
    until=time.monotonic()+settle_seconds
    while True:
        necessary=page.get_by_role("button",name="Accept only necessary cookies",exact=True)
        if necessary.count()==1 and necessary.is_visible():
            try:
                necessary.click(timeout=5000)
            except Exception:
                if necessary.is_visible():
                    raise
        welcome_close=page.locator('[id^="pendo-close-guide-"]')
        if welcome_close.count()==1 and welcome_close.is_visible():
            try:
                welcome_close.click(timeout=5000)
            except Exception:
                if welcome_close.is_visible():
                    raise
        if time.monotonic()>=until:
            break
        page.wait_for_timeout(250)


def discover(page, db, output, year, timeout, delay, max_volumes=0):
    if not db.execute("SELECT 1 FROM volumes LIMIT 1").fetchone():
        navigate(page, db, output, ISSUES_URL, "#journal-title", timeout)
        tab = page.get_by_role("tab", name=re.compile(rf"^{year}\s"))
        if tab.count() != 1:
            raise ExtractionError(f"Expected one year tab for {year}")
        controls=tab.get_attribute("aria-controls")
        panel=(page.locator("[id="+json.dumps(controls)+"]") if controls else
               page.get_by_role("tabpanel",name=re.compile(rf"^{year}\s")))
        if not panel.is_visible():
            tab.click()
        dismiss_overlays(page)
        ready(page,db,output,ISSUES_URL,panel,timeout)
        links = panel.get_by_role("link").evaluate_all(
            "links => links.map(a => ({url:a.href,title:a.textContent.trim()}))")
        if not links:
            raise ExtractionError("No yearly volumes found; refusing an empty completion")
        with db:
            for link in links:
                require_website_url(link["url"])
                if not re.fullmatch(rf"/journal/{JOURNAL}/vol/\d+/suppl/[A-Za-z0-9]+", urlsplit(link["url"]).path):
                    raise ExtractionError("Unexpected issue URL in year panel")
                db.execute("INSERT OR IGNORE INTO volumes(url,title) VALUES (?,?)", (link["url"],link["title"]))
    volumes = db.execute("SELECT url,title FROM volumes WHERE complete=0 ORDER BY title").fetchall()
    if max_volumes:
        volumes=volumes[:max_volumes]
    for volume_url, title in volumes:
        navigate(page, db, output, volume_url, "li.js-article-list-item", timeout)
        number = 1
        seen_pages = set()
        while True:
            rows = page.locator("li.js-article-list-item").evaluate_all("""items => items.map(el => {
                const a=el.querySelector('a.article-content-title');
                return a ? {url:a.href,title:a.textContent.trim()} : null;
            }).filter(Boolean)""")
            fingerprint = tuple(row["url"] for row in rows)
            if not rows or fingerprint in seen_pages:
                raise ExtractionError("Empty/repeated issue page; pagination is incomplete")
            seen_pages.add(fingerprint)
            with db:
                for row in rows:
                    require_website_url(row["url"])
                    match = re.fullmatch(r"/science/article/pii/([A-Za-z0-9]+)", urlsplit(row["url"]).path)
                    if not match:
                        raise ExtractionError("Unexpected article URL")
                    status = "excluded" if row["title"].casefold().strip(" .:") in ADMIN else "pending"
                    db.execute("INSERT OR IGNORE INTO jobs(pii,url,title,volume_url,status) VALUES (?,?,?,?,?)",
                               (match[1],row["url"],row["title"],volume_url,status))
                db.execute("INSERT OR REPLACE INTO issue_pages VALUES (?,?,?,?)",
                           (volume_url,number,now(),len(rows)))
            print(f"{title}, page {number}: {len(rows)} directory records", flush=True)
            next_button = page.get_by_role("button", name="Next page", exact=True)
            if not next_button.count() or not next_button.is_enabled():
                with db:
                    db.execute("UPDATE volumes SET complete=1 WHERE url=?", (volume_url,))
                break
            time.sleep(delay)
            old_first = rows[0]["url"]
            next_button.click()
            page.wait_for_function("""old => {
                const a=document.querySelector('li.js-article-list-item a.article-content-title');
                return a && a.href !== old;
            }""", arg=old_first, timeout=timeout*1000)
            ready(page, db, output, page.url, page.locator("li.js-article-list-item").first, timeout)
            number += 1
        time.sleep(delay)


def collect_articles(page, db, output, timeout, delay, max_articles):
    jobs = db.execute("SELECT pii,url FROM jobs WHERE status IN ('pending','failed','blocked') AND attempts<3 ORDER BY pii").fetchall()
    if max_articles:
        jobs = jobs[:max_articles]
    for pii, url in jobs:
        try:
            navigate(page, db, output, url, "#screen-reader-main-title", timeout)
            ready(page, db, output, url, page.locator("#abs0001"), timeout)
            # Capture only actual publisher article DOM and citation metadata, not user navigation.
            content = page.evaluate("""() => {
                const title=document.querySelector('#screen-reader-main-title');
                const abstract=document.querySelector('#abs0001');
                const meta=Array.from(document.querySelectorAll('meta[name]')).filter(m =>
                    m.name.toLowerCase().startsWith('citation_') || m.name.toLowerCase()==='dc.identifier');
                const license=Array.from(document.querySelectorAll('a[href]')).filter(a =>
                    a.href.includes('creativecommons.org/licenses/'));
                return [title,abstract,...meta,...license].filter(Boolean).map(el=>el.outerHTML).join('\\n');
            }""")
            captured = now()
            raw = content.encode("utf-8")
            article = parse_article(content, url, method="browser-dom", captured_at=captured, source_bytes=raw)
            if article.journal != JOURNAL_NAME:
                raise ExtractionError("Article journal does not match the selected journal")
            raw_file = output / "raw" / (pii + ".html")
            raw_file.write_bytes(raw)
            record = asdict(article)
            with db:
                db.execute("INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (pii,article.title,article.abstract,article.journal,article.doi,article.year,
                            article.volume,url,captured,hashlib.sha256(raw).hexdigest(),str(raw_file.relative_to(output)),
                            json.dumps(record,ensure_ascii=False)))
                db.execute("UPDATE jobs SET status='complete',error='',attempts=attempts+1 WHERE pii=?",(pii,))
            count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            print(f"Collected {count}: {pii}", flush=True)
        except AccessBlocked as error:
            with db:
                db.execute("UPDATE jobs SET status='blocked',error=? WHERE pii=?",(str(error),pii))
            raise
        except Exception as error:
            with db:
                db.execute("UPDATE jobs SET status='failed',attempts=attempts+1,error=? WHERE pii=?",(str(error),pii))
            print(f"Failed {pii}: {type(error).__name__}", flush=True)
        time.sleep(delay)


def export(db, output, year, run_error=""):
    records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM articles ORDER BY pii")]
    for record in records:
        record.setdefault("abstract_status","provided" if record.get("abstract") else "not_provided_by_publisher")
    directory=[dict(zip(("pii","source_url","title","volume_url","status","error"),row)) for row in db.execute("SELECT pii,url,title,volume_url,status,error FROM jobs ORDER BY volume_url,pii")]
    (output/"directory.json").write_text(json.dumps(directory,ensure_ascii=False,indent=2),encoding="utf-8")
    with (output/"directory.csv").open("w",encoding="utf-8-sig",newline="") as file:
        writer=csv.DictWriter(file,fieldnames=["pii","title","source_url","volume_url","status","error"])
        writer.writeheader()
        from scraper import safe_csv
        writer.writerows({k:safe_csv(v) for k,v in row.items()} for row in directory)
    (output/"articles.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
    fields = ["title","abstract","abstract_status","article_type","journal","doi","pii","year","volume","pages","source_url","capture_method","captured_at","source_sha256"]
    with (output/"articles.csv").open("w",encoding="utf-8-sig",newline="") as file:
        writer=csv.DictWriter(file,fieldnames=fields,extrasaction="ignore");writer.writeheader()
        for record in records:
            writer.writerow({k: ("'"+v if isinstance(v,str) and v.startswith(("=","+","-","@")) else v)
                             for k,v in record.items() if k in fields})
    statuses=dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())
    total_volumes=db.execute("SELECT COUNT(*) FROM volumes").fetchone()[0]
    complete_volumes=db.execute("SELECT COUNT(*) FROM volumes WHERE complete=1").fetchone()[0]
    directory_rows=db.execute("SELECT COALESCE(SUM(row_count),0) FROM issue_pages").fetchone()[0]
    unique_jobs=sum(statuses.values())
    report={"at":now(),"year":year,"source":"ScienceDirect website browser DOM only",
            "volumes_discovered":total_volumes,"volumes_complete":complete_volumes,
            "directory_row_count":directory_rows,"unique_directory_records":unique_jobs,
            "captcha_checkbox_attempts":db.execute("SELECT COUNT(*) FROM events WHERE kind='turnstile-checkbox-attempt'").fetchone()[0],
            "jobs":statuses,"records_total":len(records),"articles_with_abstract":sum(bool(r.get("abstract")) for r in records),"run_error":run_error,
            "complete":bool(total_volumes) and bool(unique_jobs) and directory_rows==unique_jobs and
                       len(records)==statuses.get("complete",0)+statuses.get("no_abstract",0) and total_volumes==complete_volumes and
                       not any(statuses.get(s,0) for s in ("pending","failed","blocked")) and not run_error}
    (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


def main():
    global CAPTCHA_CLICKS_REMAINING
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year",type=int,default=2024)
    parser.add_argument("--out",type=Path,default=Path("website/2024"))
    parser.add_argument("--channel",choices=["chrome","msedge","firefox"],default="chrome")
    parser.add_argument("--headless",action="store_true",help="Diagnostic only: currently blocked in this environment")
    parser.add_argument("--ready-timeout",type=int,default=45)
    parser.add_argument("--delay",type=float,default=3)
    parser.add_argument("--max-articles",type=int,default=0)
    parser.add_argument("--max-volumes",type=int,default=0,help="Limit newly discovered volumes for a smoke test")
    parser.add_argument("--discover-only",action="store_true")
    parser.add_argument("--export-only",action="store_true")
    parser.add_argument("--captcha-attempts",type=int,default=0,
                        help="Budget for standard visible Turnstile checkbox attempts; 0 disables interaction")
    args=parser.parse_args()
    if not 1900<=args.year<=2100 or args.delay<0 or args.ready_timeout<1 or args.max_articles<0 or args.max_volumes<0 or args.captcha_attempts<0:
        parser.error("Invalid year/delay/timeout/article count")
    output=args.out.resolve();output.mkdir(parents=True,exist_ok=True)
    CAPTCHA_CLICKS_REMAINING=args.captcha_attempts
    for folder in ("raw","diagnostics"):(output/folder).mkdir(exist_ok=True)
    with sqlite3.connect(output/"articles.sqlite3") as db:
        initialize(db,args.year)
        run_error=""
        try:
            if not args.export_only:
                if (ROOT/".runtime").exists():sys.path.insert(0,str(ROOT/".runtime"))
                if (ROOT/".runtime-browsers").exists():
                    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",str(ROOT/".runtime-browsers"))
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    legacy_profile=output/"browser-profile"
                    profile=(legacy_profile if args.channel=="chrome" and legacy_profile.exists() else
                             output/("browser-profile-"+args.channel))
                    if args.channel=="firefox":
                        context=playwright.firefox.launch_persistent_context(str(profile),headless=args.headless)
                    else:
                        context=playwright.chromium.launch_persistent_context(str(profile),
                                  channel=args.channel,headless=args.headless,chromium_sandbox=True)
                    try:
                        page=context.new_page()
                        discover(page,db,output,args.year,args.ready_timeout,args.delay,args.max_volumes)
                        if not args.discover_only:
                            collect_articles(page,db,output,args.ready_timeout,args.delay,args.max_articles)
                    except Exception:
                        try:evidence(page,output,"collector-error",page.url)
                        except Exception:pass
                        raise
                    finally:context.close()
        except Exception as error:
            run_error=f"{type(error).__name__}: {error}"
            print(run_error,file=sys.stderr,flush=True)
        finally:export(db,output,args.year,run_error)
        return 2 if run_error or db.execute("SELECT 1 FROM jobs WHERE status IN ('failed','blocked') LIMIT 1").fetchone() else 0


if __name__=="__main__":
    raise SystemExit(main())
