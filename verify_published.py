"""Verify published article data against its original captured DOM evidence."""
import argparse
import csv
import hashlib
import json
import zipfile
from pathlib import Path

from scraper import parse_article,safe_csv,validate_url
from website_collect_cdp import parse_preview


def verify(folder):
    raw_json=(folder/'articles.json').read_bytes()
    records=json.loads(raw_json)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(raw_json).hexdigest()==manifest['data_sha256'],'Data hash mismatch'
    assert hashlib.sha256((folder/'source-evidence.zip').read_bytes()).hexdigest()==manifest['evidence_sha256'],'Evidence archive hash mismatch'
    assert len(records)==manifest['articles'],'Published count mismatch'
    piies=set()
    with zipfile.ZipFile(folder/'source-evidence.zip') as archive:
        for row in records:
            pii=validate_url(row['source_url'])
            assert pii==row['pii'] and pii not in piies,'Invalid/duplicate PII'
            piies.add(pii)
            source=archive.read('raw/'+pii+'.html')
            assert hashlib.sha256(source).hexdigest()==row['source_sha256'],'Source hash mismatch: '+pii
            content=source.decode('utf-8')
            if row['capture_method']=='website-issue-preview-dom':
                article=parse_preview(content,row['issue_url'],int(row['year']),row['captured_at'])
                title,abstract=article['title'],article['abstract']
            else:
                article=parse_article(content,row['source_url'])
                title,abstract=article.title,article.abstract
            assert (title,abstract)==(row['title'],row['abstract']),'Text differs from source: '+pii
            assert abstract,'Published row has no abstract'
    with (folder/'articles.csv').open(encoding='utf-8-sig',newline='') as file:
        csv_rows=list(csv.DictReader(file))
    assert len(csv_rows)==len(records),'CSV count mismatch'
    for actual,expected in zip(csv_rows,records):
        for key in actual:assert actual[key]==str(safe_csv(expected.get(key,''))),f'CSV mismatch: {expected["pii"]}/{key}'
    print(json.dumps({'verified':True,'articles':len(records),'source_fragments':len(piies),'volumes':len({row['volume'] for row in records})},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',type=Path,default=Path(__file__).resolve().parent/'docs'/'batch-2024')
    verify(parser.parse_args().path)
