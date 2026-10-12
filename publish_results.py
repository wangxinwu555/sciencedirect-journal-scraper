"""Build a static, shareable view of captured ScienceDirect titles and abstracts."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scraper import Document, clean, safe_csv, validate_url

ROOT=Path(__file__).resolve().parent


def build(source, destination, template, year):
    rows=json.loads((source/'articles.json').read_text(encoding='utf-8'))
    articles=[dict(row) for row in rows if row.get('abstract')]
    if not articles:raise ValueError('No captured abstracts to publish')
    seen=set()
    for article in articles:
        pii=validate_url(article['source_url'])
        if pii!=article['pii'] or pii in seen:raise ValueError('Duplicate or mismatched article identifier')
        if str(article['year'])!=str(year):raise ValueError('Article outside selected cover year')
        seen.add(pii)
        raw=(source/'raw'/(pii+'.html')).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=article['source_sha256']:
            raise ValueError('Source evidence hash differs: '+pii)
        if not article.get('authors_display'):
            doc=Document();doc.feed(raw.decode('utf-8'))
            authors=[n.attrs.get('content','') for n in doc.root.walk() if n.tag=='meta' and n.attrs.get('name')=='citation_author']
            article['authors_display']=', '.join(clean(author) for author in authors if author)
        article['abstract_status']='provided'
    destination.mkdir(parents=True,exist_ok=True)
    data=json.dumps(articles,ensure_ascii=False,indent=2).encode('utf-8')
    (destination/'articles.json').write_bytes(data)
    fields=['title','abstract','authors_display','article_type','journal','doi','pii','year','volume','pages','source_url','issue_url','capture_method','captured_at','source_sha256']
    with (destination/'articles.csv').open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        writer.writerows({k:safe_csv(v) for k,v in row.items() if k in fields} for row in articles)
    # Only article DOM evidence is public. Browser profiles, diagnostics, run
    # databases and machine-specific paths never enter the publication bundle.
    with zipfile.ZipFile(destination/'source-evidence.zip','w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for article in articles:
            pii=article['pii'];archive.write(source/'raw'/(pii+'.html'),'raw/'+pii+'.html')
        archive.writestr('README.txt','ScienceDirect captured article DOM fragments.\nMatch raw/PII.html SHA-256 to source_sha256 in articles.json.\nOriginal titles and abstracts retain their original authors, publishers and licensing terms.\nNo browser profiles, credentials or diagnostic screenshots are included.\n')
    volumes=Counter(article['volume'] for article in articles)
    captures=[article['captured_at'] for article in articles]
    manifest={'journal':'Procedia Computer Science','year':year,'articles':len(articles),
              'volumes':[{'volume':volume,'articles':volumes[volume]} for volume in sorted(volumes,key=int)],
              'article_types':dict(Counter(article['article_type'] for article in articles)),
              'source':'ScienceDirect website article and issue DOM',
              'scope':'Captured titles with publisher abstract text',
              'captured_at_start':min(captures),'captured_at_end':max(captures),
              'published_at':datetime.now(timezone.utc).isoformat(),
              'data_sha256':hashlib.sha256(data).hexdigest(),
              'evidence_sha256':hashlib.sha256((destination/'source-evidence.zip').read_bytes()).hexdigest(),
              'source_evidence_verified':len(articles)}
    (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    volume_range=f'{min(volumes,key=int)}–{max(volumes,key=int)}'
    page=template.read_text(encoding='utf-8').replace('{{COUNT}}',f'{len(articles):,}').replace('{{VOLUMES}}',str(len(volumes))).replace('{{RANGE}}',volume_range).replace('{{YEAR}}',str(year))
    (destination/'index.html').write_text(page,encoding='utf-8')
    (destination.parent/'.nojekyll').touch()
    print(json.dumps({'output':str(destination),'articles':len(articles),'volumes':len(volumes),'source_evidence_verified':len(articles),'data_sha256':manifest['data_sha256']},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'website'/'2024-cdp')
    parser.add_argument('--out',type=Path,default=ROOT/'docs'/'batch-2024')
    parser.add_argument('--year',type=int,default=2024)
    args=parser.parse_args()
    build(args.source.resolve(),args.out.resolve(),ROOT/'templates'/'batch.html',args.year)


if __name__=='__main__':main()
