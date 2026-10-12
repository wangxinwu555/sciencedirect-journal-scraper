"""Collect the selected ScienceDirect journal year through ordinary Chrome pages.

Uses the website's issue lists and Article preview buttons. No CAPTCHA solving.
An isolated persistent profile and a SQLite checkpoint make restarts repeatable.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from scraper import Document, ExtractionError, clean, parse_article, validate_url
from website_collect import ADMIN, ISSUES_URL, JOURNAL, JOURNAL_NAME, AccessBlocked, export, initialize, now, require_website_url

ROOT = Path(__file__).resolve().parent


def parse_preview(content, volume_url, year, captured):
    """Parse a real issue row; accept only its labelled Abstract panel."""
    require_website_url(volume_url)
    volume = re.fullmatch(rf"/journal/{JOURNAL}/vol/(\d+)/suppl/[A-Za-z0-9]+", urlsplit(volume_url).path)
    if not volume:
        raise ExtractionError("Unexpected volume source")
    doc = Document(); doc.feed(content)
    nodes = list(doc.root.walk())
    def has_class(node, value):
        return value in node.attrs.get("class", "").split()
    link = next((n for n in nodes if n.tag == "a" and has_class(n, "article-content-title")), None)
    abstract_node = next((n for n in nodes if has_class(n, "js-abstract-body-text")), None)
    if link is None or abstract_node is None:
        raise ExtractionError("Missing article link or labelled Abstract preview")
    source = urljoin(volume_url, link.attrs.get("href", "")); require_website_url(source)
    pii = validate_url(source)
    if link.attrs.get("id") != pii:
        raise ExtractionError("Preview PII mismatch")
    headings = [clean(n.text()).casefold() for n in abstract_node.walk() if n.tag in {"h2", "h3", "h4", "h5"}]
    if "abstract" not in headings:
        raise ExtractionError("Preview does not contain a labelled Abstract")
    title = clean(link.text()); abstract = clean(abstract_node.text(skip_headings=True))
    if not title or len(abstract.split()) < 12 or abstract.endswith(("...", "…")):
        raise ExtractionError("Missing, short or truncated preview abstract")
    doi = next((clean(n.text()).removeprefix("https://doi.org/") for n in nodes
                if n.tag == "div" and "hidden" in n.attrs and re.fullmatch(r"https://doi.org/10\.\d{4,9}/\S+", clean(n.text()))), "")
    def text_of(cls):
        return next((clean(n.text()) for n in nodes if has_class(n, cls)), "")
    return {"pii": pii, "title": title, "abstract": abstract, "journal": JOURNAL_NAME,
            "abstract_status":"provided",
            "doi": doi, "year": str(year), "volume": volume[1], "pages": text_of("js-article-page-range").removeprefix("Pages "),
            "authors_display": text_of("js-article__item__authors"), "article_type": text_of("js-article-subtype"),
            "source_url": source, "issue_url": volume_url, "license_url": "", "capture_method": "website-issue-preview-dom",
            "captured_at": captured, "extracted_at": now(), "source_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}


async def value(tab, expression):
    result=await tab.evaluate("JSON.stringify(" + expression + ")")
    if not isinstance(result,str):
        raise ExtractionError("Browser DOM expression did not return JSON: "+str(result))
    return json.loads(result)


async def wait_for(tab, expression, timeout):
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = await value(tab, "({ok:Boolean(" + expression + "),url:location.href,title:document.title})")
        if last["ok"]:
            require_website_url(last["url"])
            return
        await asyncio.sleep(0.4)
    from website_collect import challenge_reason
    reason = challenge_reason(await value(tab,"document.body?.innerText||''"))
    if reason:
        raise AccessBlocked(reason + ": " + last.get("url", ""))
    raise ExtractionError("Expected publisher DOM did not load: " + last.get("url", ""))


async def dismiss_overlays(tab):
    # Only ordinary cookie/welcome controls; never select challenge frames.
    for _ in range(6):
        for selector in ("#onetrust-reject-all-handler", '[id^="pendo-close-guide-"]'):
            visible = await value(tab, "(()=>{const e=document.querySelector("+json.dumps(selector)+");return Boolean(e&&e.getClientRects().length)})()")
            if visible:
                element = await tab.select(selector, timeout=2)
                await element.click()
        await asyncio.sleep(0.5)


async def navigate(browser, url, selector, timeout):
    require_website_url(url)
    tab = await browser.get(url)
    await wait_for(tab, "document.querySelector(" + json.dumps(selector) + ")", timeout)
    await asyncio.sleep(0.6)
    await dismiss_overlays(tab)
    return tab


async def close_browser(browser, driver):
    # Await our own subprocess so Python 3.14 does not dispose open pipe handles.
    process = browser._process
    try:
        await asyncio.wait_for(browser.send(driver.cdp.browser.close()), 5)
    except Exception:
        pass
    if process is not None:
        try:
            await asyncio.wait_for(process.wait(), 8)
        except asyncio.TimeoutError:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
    await browser.aclose()
    if process is not None and process._transport is not None:
        process._transport.close()
    await asyncio.sleep(0)


async def discover_year(browser, db, output, args):
    tab = await navigate(browser, ISSUES_URL, "[role=tab]", args.timeout)
    tabs = await value(tab, "Array.from(document.querySelectorAll('[role=tab]')).map(e=>({id:e.id,text:e.textContent.trim(),controls:e.getAttribute('aria-controls')}))")
    (output/"year-tabs.json").write_text(json.dumps(tabs,ensure_ascii=False,indent=2),encoding="utf-8")
    matches = [t for t in tabs if re.match(rf"^{args.year}\s", t["text"])]
    if len(matches) != 1 or not matches[0]["id"] or not matches[0]["controls"]:
        raise ExtractionError("Expected one year panel with an associated tab")
    selected = matches[0]; panel_selector = "[id=" + json.dumps(selected["controls"]) + "]"
    visible = await value(tab, "(()=>{const e=document.querySelector("+json.dumps(panel_selector)+");return Boolean(e&&e.getClientRects().length)})()")
    if not visible:
        tab_selector="[id="+json.dumps(selected["id"])+"]"
        tab_html=await value(tab,"document.querySelector("+json.dumps(tab_selector)+")?.outerHTML")
        (output/"year-tab.html").write_text(tab_html or '',encoding="utf-8")
        if await value(tab,"Boolean(document.querySelector("+json.dumps(tab_selector+" button")+"))"):
            tab_selector+=" button"
        element = await tab.select(tab_selector, timeout=5)
        await element.click()
    await wait_for(tab, "document.querySelector("+json.dumps(panel_selector)+")?.querySelector('a[href]')", args.timeout)
    links = await value(tab, "Array.from(document.querySelector("+json.dumps(panel_selector)+").querySelectorAll('a[href]')).map(e=>({url:e.href,title:e.textContent.trim()}))")
    if not links:
        raise ExtractionError("Empty yearly directory")
    for link in links:
        require_website_url(link["url"])
        if not re.fullmatch(rf"/journal/{JOURNAL}/vol/\d+/suppl/[A-Za-z0-9]+", urlsplit(link["url"]).path):
            raise ExtractionError("Unexpected volume URL in year panel")
    saved = set(row[0] for row in db.execute("SELECT url FROM volumes"))
    if saved and saved != {link["url"] for link in links}:
        raise ExtractionError("Website year directory changed; inspect before reusing checkpoints")
    with db:
        for link in links:
            db.execute("INSERT OR IGNORE INTO volumes(url,title) VALUES (?,?)", (link["url"], link["title"]))
    (output/"year-directory.json").write_text(json.dumps({"source_url":ISSUES_URL,"year":args.year,"panel":selected["text"],"captured_at":now(),"volumes":links},ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Website year {args.year}: {len(links)} volumes", flush=True)


async def validate_sample(browser, db, output, args):
    existing = db.execute("SELECT 1 FROM events WHERE kind='preview-fullpage-match' LIMIT 1").fetchone()
    if existing:
        return
    sample = db.execute("SELECT payload FROM articles ORDER BY pii LIMIT 1").fetchone()
    if not sample:
        return
    record = json.loads(sample[0])
    tab = await navigate(browser, record["source_url"], "#screen-reader-main-title", args.timeout)
    await wait_for(tab, "document.querySelector('#abs0001')", args.timeout)
    content = await value(tab, "[document.querySelector('#screen-reader-main-title'),document.querySelector('#abs0001'),...Array.from(document.querySelectorAll('meta[name]')).filter(e=>e.name.toLowerCase().startsWith('citation_'))].filter(Boolean).map(e=>e.outerHTML).join(String.fromCharCode(10))")
    article = parse_article(content, record["source_url"], method="browser-dom", captured_at=now())
    if clean(article.abstract) != clean(record["abstract"]) or clean(article.title) != clean(record["title"]):
        raise ExtractionError("Issue preview does not match the full article's title/abstract")
    (output/"validation"/(record["pii"]+".html")).write_text(content,encoding="utf-8")
    with db:
        db.execute("INSERT INTO events VALUES (?,?,?,?)", (now(),"preview-fullpage-match",record["source_url"],json.dumps({"pii":record["pii"],"words":len(article.abstract.split()),"match":True})))
    print("Full article comparison passed: " + record["pii"], flush=True)


async def full_article_fallback(browser, output, args, row, volume_url):
    """Try the real article page when its issue preview is absent/unparseable."""
    require_website_url(row["url"])
    tab=await browser.get(row["url"],new_tab=True)
    try:
        await wait_for(tab,"document.querySelector('#screen-reader-main-title')",args.timeout)
        await dismiss_overlays(tab)
        if row["type"]=="Editorial":
            await asyncio.sleep(2)
        else:
            await wait_for(tab,"document.querySelector('#abs0001')",args.timeout)
        content=await value(tab,"[document.querySelector('#screen-reader-main-title'),document.querySelector('#abs0001'),...document.querySelectorAll('div.abstract,section.abstract'),...Array.from(document.querySelectorAll('meta[name]')).filter(e=>e.name.toLowerCase().startsWith('citation_'))].filter(Boolean).map(e=>e.outerHTML).join(String.fromCharCode(10))")
        captured=now()
        (output/"diagnostics"/(row["pii"]+"-full-article.html")).write_bytes(content.encode("utf-8"))
        try:
            article=parse_article(content,row["url"],method="browser-article-dom",captured_at=captured)
        except ExtractionError:
            record=parse_editorial_without_abstract(content,row,volume_url,args.year,captured)
            print("Publisher editorial without abstract: "+row["pii"],flush=True)
            return content,record
        if article.journal!=JOURNAL_NAME or article.title!=row["title"]:
            raise ExtractionError("Full article journal/title differs from issue directory")
        record=asdict(article)
        record.update({"abstract_status":"provided","online_publication_year":article.year,"year":str(args.year),"issue_url":volume_url,
                       "volume":re.search(r"/vol/(\d+)/",volume_url)[1],"article_type":row["type"]})
        print("Full article fallback passed: "+row["pii"],flush=True)
        return content,record
    except Exception as error:
        try:
            details=await value(tab,"({url:location.href,title:document.title,body:document.body?.innerText||''})")
            details["body"]=re.sub(r"IP Address:.*","IP Address: [omitted]",details["body"])[:5000]
            details["error"]=str(error)
            (output/"diagnostics"/(row["pii"]+"-fullpage-error.json")).write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding="utf-8")
        except Exception:pass
        raise
    finally:
        await tab.close()


def parse_editorial_without_abstract(content,row,volume_url,year,captured):
    """Retain actual editorial titles only after confirming empty publisher abstract DOM."""
    if row["type"]!="Editorial":
        raise ExtractionError("A research article without a validated abstract remains failed")
    doc=Document();doc.feed(content);nodes=list(doc.root.walk())
    meta={n.attrs.get("name","").casefold():n.attrs.get("content","") for n in nodes if n.tag=="meta"}
    title_node=next((n for n in nodes if n.attrs.get("id")=="screen-reader-main-title"),None)
    title=clean(title_node.text()) if title_node else ""
    containers=[n for n in nodes if n.attrs.get("id")=="abs0001" or "abstract" in n.attrs.get("class","").split()]
    if (title!=row["title"] or meta.get("citation_journal_title")!=JOURNAL_NAME or
            any(clean(n.text(skip_headings=True)).casefold() not in {"","abstract"} for n in containers)):
        raise ExtractionError("Cannot confirm publisher editorial has no abstract")
    pii=validate_url(row["url"])
    if meta.get("citation_pii",pii)!=pii:raise ExtractionError("Full article PII mismatch")
    first,last=meta.get("citation_firstpage",""),meta.get("citation_lastpage","")
    return {"pii":pii,"title":title,"abstract":"","abstract_status":"not_provided_by_publisher",
            "journal":JOURNAL_NAME,"doi":meta.get("citation_doi",""),"year":str(year),
            "online_publication_year":meta.get("citation_publication_date","")[:4],
            "volume":re.search(r"/vol/(\d+)/",volume_url)[1],"pages":f"{first}–{last}" if first and last else first,
            "article_type":row["type"],"source_url":row["url"],"issue_url":volume_url,"license_url":"",
            "capture_method":"browser-article-dom","captured_at":captured,"extracted_at":now(),
            "source_sha256":hashlib.sha256(content.encode("utf-8")).hexdigest()}


ROWS = """Array.from(document.querySelectorAll('li.js-article-list-item')).map(e=>{
 const a=e.querySelector('a.article-content-title');return a?{pii:a.id,url:a.href,title:a.textContent.trim(),type:e.querySelector('.js-article-subtype')?.textContent.trim()||'',hasPreview:Boolean(e.querySelector('button[aria-describedby]'))}:null;
}).filter(Boolean)"""


async def collect_volume(browser, db, output, args, volume_url, title, budget):
    tab = await navigate(browser, volume_url, "li.js-article-list-item", args.timeout)
    page_number = 1; seen = set()
    while True:
        rows = await value(tab, ROWS)
        fingerprint = tuple(row["pii"] for row in rows)
        if not rows or fingerprint in seen:
            raise ExtractionError("Empty/repeated issue page")
        seen.add(fingerprint)
        with db:
            for row in rows:
                require_website_url(row["url"])
                if validate_url(row["url"]) != row["pii"]:
                    raise ExtractionError("Directory PII mismatch")
                status = "excluded" if row["title"].casefold().strip(" .:") in ADMIN else "pending"
                db.execute("INSERT OR IGNORE INTO jobs(pii,url,title,volume_url,status) VALUES (?,?,?,?,?)", (row["pii"],row["url"],row["title"],volume_url,status))
            db.execute("INSERT OR REPLACE INTO issue_pages VALUES (?,?,?,?)", (volume_url,page_number,now(),len(rows)))
        print(f"{title}, page {page_number}: {len(rows)} records", flush=True)
        controls=await value(tab,"Array.from(document.querySelectorAll('a,button,label,[role=button],input')).filter(e=>/Show all article previews|Next page/.test(e.textContent+' '+e.getAttribute('aria-label'))).map(e=>({html:e.outerHTML,visible:Boolean(e.getClientRects().length)}))")
        (output/"diagnostics"/(title.replace(' ','-')+f'-page-{page_number}-controls.json')).write_text(json.dumps(controls,ensure_ascii=False,indent=2),encoding="utf-8")
        loaded_previews={}
        pending_piis={item[0] for item in db.execute("SELECT pii FROM jobs WHERE volume_url=? AND status IN ('pending','failed','blocked') AND attempts<3",(volume_url,))}
        if args.show_all_previews and not args.discover_only and any(row["pii"] in pending_piis for row in rows):
            # Use the publisher's ordinary batch display control. The website
            # owns its network requests; no private API or fabricated response.
            clicked=await value(tab,"(()=>{const e=document.querySelector('input.js-previews-switch[data-aa-button=\"show-all-article-previews\"]');if(!e||!e.getClientRects().length)return false;if(!e.checked)e.click();return e.checked})()")
            if clicked:
                print("Publisher Show all article previews control activated",flush=True)
                await asyncio.sleep(5)
                loaded=await value(tab,"Array.from(document.querySelectorAll('li.js-article-list-item')).filter(e=>e.querySelector('.js-abstract-body-text')).map(e=>({pii:e.querySelector('a.article-content-title')?.id,html:e.outerHTML}))")
                loaded_previews={item["pii"]:item["html"] for item in loaded}
        if not args.discover_only:
            for row in rows:
                state, attempts = db.execute("SELECT status,attempts FROM jobs WHERE pii=?", (row["pii"],)).fetchone()
                if state in {"complete", "excluded", "no_abstract"} or attempts >= 3:
                    continue
                if args.max_articles and budget[0] >= args.max_articles:
                    return False
                pii = row["pii"]; selector = "button[aria-describedby="+json.dumps(pii)+"]"
                try:
                    row_selector = "a[id="+json.dumps(pii)+"]"
                    try:
                        if not row["hasPreview"]:
                            raise ExtractionError("Publisher provides no Article preview control")
                        content=loaded_previews.get(pii)
                        if content is None:
                            expanded = await value(tab,"document.querySelector("+json.dumps(selector)+")?.getAttribute('aria-expanded')")
                            if expanded != "true":
                                element = await tab.select(selector, timeout=4)
                                await element.click()
                            expression = "document.querySelector("+json.dumps(row_selector)+")?.closest('li')?.querySelector('.js-abstract-body-text')"
                            await wait_for(tab, expression, min(args.timeout,15))
                            content = await value(tab, "document.querySelector("+json.dumps(row_selector)+")?.closest('li')?.outerHTML")
                        record = parse_preview(content,volume_url,args.year,now())
                    except ExtractionError:
                        broken=await value(tab,"document.querySelector("+json.dumps(row_selector)+")?.closest('li')?.outerHTML")
                        if broken:(output/"diagnostics"/(pii+"-preview.html")).write_text(broken,encoding="utf-8")
                        content,record=await full_article_fallback(browser,output,args,row,volume_url)
                    captured=record["captured_at"]
                    raw_file = output/"raw"/(pii+".html"); raw_file.write_bytes(content.encode("utf-8"))
                    with db:
                        db.execute("INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (pii,record["title"],record["abstract"],record["journal"],record["doi"],record["year"],record["volume"],record["source_url"],captured,record["source_sha256"],str(raw_file.relative_to(output)),json.dumps(record,ensure_ascii=False)))
                        status="complete" if record["abstract"] else "no_abstract"
                        db.execute("UPDATE jobs SET status=?,error='',attempts=attempts+1 WHERE pii=?", (status,pii))
                    budget[0] += 1
                    if budget[0] % 20 == 0:
                        print(f"Saved records: {db.execute('SELECT COUNT(*) FROM articles').fetchone()[0]}", flush=True)
                    if not args.show_all_previews:
                        # Close this ordinary preview to keep the large issue DOM small.
                        if row["hasPreview"]:
                            element = await tab.select(selector, timeout=3); await element.click()
                except AccessBlocked as error:
                    with db:db.execute("UPDATE jobs SET status='blocked',error=? WHERE pii=?", (str(error),pii))
                    raise
                except Exception as error:
                    with db:db.execute("UPDATE jobs SET status='failed',attempts=attempts+1,error=? WHERE pii=?", (str(error),pii))
                    print(f"Missing {pii}: {error}",flush=True)
                if not args.show_all_previews:await asyncio.sleep(args.delay)
        next_info = await value(tab, "Array.from(document.querySelectorAll('button')).filter(e=>e.getAttribute('aria-label')==='Next page').map(e=>({disabled:e.disabled,html:e.outerHTML}))")
        if not next_info or all(item["disabled"] for item in next_info):
            with db:db.execute("UPDATE volumes SET complete=1 WHERE url=?", (volume_url,))
            return True
        # Select the observed navigation control; never infer pagination from row count.
        next_button = await tab.select('button[aria-label="Next page"]',timeout=4)
        old_first = rows[0]["pii"]; await next_button.click()
        await wait_for(tab, "document.querySelector('li.js-article-list-item a.article-content-title')?.id !== "+json.dumps(old_first)+" && document.querySelector('li.js-article-list-item a.article-content-title')", args.timeout)
        await asyncio.sleep(args.delay); page_number += 1


async def collect(args, db, output):
    if (ROOT/".runtime-nodriver").exists():sys.path.insert(0,str(ROOT/".runtime-nodriver"))
    import nodriver as driver
    class StandardSecurityConfig(driver.Config):
        def __call__(self):
            prefixes=("--user-data-dir=","--remote-debugging-host=","--remote-debugging-port=")
            harmless={"--no-first-run","--no-default-browser-check","--homepage=about:blank"}
            return [arg for arg in super().__call__() if arg in harmless or arg.startswith(prefixes)]
    executable = args.browser or ("C:/Program Files/Google/Chrome/Application/chrome.exe" if args.channel=="chrome" else "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    profile = (args.profile or (output/("browser-profile-cdp-"+args.channel))).resolve()
    config = StandardSecurityConfig(user_data_dir=str(profile),browser_executable_path=executable,sandbox=True,expert=False,headless=False,host="127.0.0.1")
    browser = await driver.Browser.create(config=config)
    try:
        await discover_year(browser,db,output,args)
        volumes = db.execute("SELECT url,title FROM volumes WHERE complete=0 OR url IN (SELECT volume_url FROM jobs WHERE status IN ('pending','failed','blocked') AND attempts<3) ORDER BY title").fetchall()
        if args.max_volumes:volumes=volumes[:args.max_volumes]
        budget=[0]
        for volume_url,title in volumes:
            done = await collect_volume(browser,db,output,args,volume_url,title,budget)
            if not args.discover_only:await validate_sample(browser,db,output,args)
            export(db,output,args.year)
            if not done:break
            await asyncio.sleep(2)
    except Exception:
        try:
            tab=browser.tabs[0]
            stamp=str(time.time_ns())
            await tab.save_screenshot(str(output/"diagnostics"/(stamp+".png")))
            body=await value(tab,"document.body?.innerText||''")
            body=re.sub(r"IP Address:.*","IP Address: [omitted]",body)
            (output/"diagnostics"/(stamp+".txt")).write_text(body,encoding="utf-8")
        except Exception:pass
        raise
    finally:
        await close_browser(browser,driver)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year",type=int,default=2024)
    parser.add_argument("--out",type=Path,default=ROOT/"website"/"2024-cdp")
    parser.add_argument("--channel",choices=["chrome","msedge"],default="chrome")
    parser.add_argument("--browser",help="Installed Chromium browser executable")
    parser.add_argument("--profile",type=Path,help="Project-owned persistent browser profile")
    parser.add_argument("--timeout",type=int,default=30)
    parser.add_argument("--delay",type=float,default=0.5)
    parser.add_argument("--max-volumes",type=int,default=0)
    parser.add_argument("--max-articles",type=int,default=0)
    parser.add_argument("--discover-only",action="store_true")
    parser.add_argument("--export-only",action="store_true")
    parser.add_argument("--show-all-previews",action="store_true",help="Use publisher's Show all article previews control")
    args=parser.parse_args()
    if not 1900<=args.year<=2100 or args.timeout<1 or args.delay<0 or args.max_volumes<0 or args.max_articles<0:
        parser.error("Invalid year, timeout, delay or limit")
    output=args.out.resolve(); output.mkdir(parents=True,exist_ok=True)
    for folder in ("raw","diagnostics","validation"):(output/folder).mkdir(exist_ok=True)
    with sqlite3.connect(output/"articles.sqlite3") as db:
        initialize(db,args.year); error=""
        try:
            if not args.export_only:asyncio.run(collect(args,db,output))
        except Exception as exc:
            error=f"{type(exc).__name__}: {exc}"; print(error,file=sys.stderr,flush=True)
        finally:export(db,output,args.year,error)
        return 2 if error or db.execute("SELECT 1 FROM jobs WHERE status IN ('failed','blocked') LIMIT 1").fetchone() else 0


if __name__=="__main__":raise SystemExit(main())
