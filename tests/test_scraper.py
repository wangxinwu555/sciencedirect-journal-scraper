import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scraper import ROOT, ExtractionError, export, fetch_article, parse_article, safe_csv

URL = 'https://www.sciencedirect.com/science/article/pii/S1877050924027492'


class ScraperTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / 'raw/article.html').read_bytes()
        self.record = json.loads((ROOT / 'raw/capture.json').read_text(encoding='utf-8'))
        self.article = parse_article(self.raw.decode(), URL, method='browser-dom',
                                     captured_at=self.record['captured_at'], source_bytes=self.raw)

    def test_actual_article_and_complete_abstract(self):
        a = self.article
        self.assertEqual(a.title, 'Generative AI and Large Language Models - Benefits, Drawbacks, Future and Recommendations')
        self.assertEqual(a.journal, 'Procedia Computer Science')
        self.assertEqual(a.doi, '10.1016/j.procs.2024.09.689')
        self.assertEqual(a.pages, '5458–5468')
        self.assertTrue(a.abstract.startswith('Natural language processing, with parsing and generation,'))
        self.assertTrue(a.abstract.endswith('research questions on LLMs, and recommendations.'))
        self.assertEqual(len(a.abstract.split()), 228)
        self.assertNotIn('Abstract Natural', a.abstract)
        self.assertEqual(a.source_sha256, hashlib.sha256(self.raw).hexdigest())

    def test_references_and_recommended_abstracts_are_excluded(self):
        page = self.raw.decode().replace('</body>', '<section id="references">References text</section><div class="abstract">Other recommended article</div></body>')
        self.assertEqual(parse_article(page, URL).abstract, self.article.abstract)

    def test_nested_inline_text_is_preserved(self):
        page = self.raw.decode().replace('Natural language processing', 'Natural <em>language</em> processing')
        self.assertEqual(parse_article(page, URL).abstract, self.article.abstract)

    def test_blocked_pages_and_short_search_snippets_fail(self):
        for page in ['<h1>Access denied</h1>', '<meta name="citation_title" content="Title"><meta name="description" content="Short search snippet">']:
            with self.assertRaises(ExtractionError):
                parse_article(page, URL)

    def test_wrong_article_and_non_sciencedirect_url_fail(self):
        with self.assertRaises(ExtractionError):
            parse_article(self.raw.decode(), URL.replace('S1877050924027492', 'S0000000000000000'))
        with self.assertRaises(ExtractionError):
            parse_article(self.raw.decode(), 'https://example.com/science/article/pii/S1877050924027492')

    def test_http_403_is_not_a_success(self):
        with patch('scraper.urlopen', side_effect=HTTPError(URL, 403, 'Forbidden', {}, None)):
            with self.assertRaisesRegex(ExtractionError, 'HTTP 403'):
                fetch_article(URL)

    def test_exports_are_consistent_and_page_escapes_content(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            dest = Path(folder)
            self.assertTrue(dest.resolve().is_relative_to(ROOT.resolve()))
            export([self.article], dest, evidence=ROOT / 'raw/article.html', provenance=ROOT / 'raw/capture.json')
            data = json.loads((dest / 'articles.json').read_text(encoding='utf-8'))
            with (dest / 'articles.csv').open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows, data)
            page = (dest / 'index.html').read_text(encoding='utf-8')
            self.assertIn(self.article.abstract, page)
            self.assertNotIn('{{', page)
            self.assertEqual((dest / 'evidence/article.html').read_bytes(), self.raw)
            self.article.title = '<script>alert(1)</script>'
            export([self.article], dest)
            page = (dest / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>alert(1)</script>', page)
            self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)

    def test_csv_formula_protection(self):
        self.assertEqual(safe_csv('=1+1'), "'=1+1")
        self.assertEqual(safe_csv('  @SUM(A1)'), "'  @SUM(A1)")
        self.assertEqual(safe_csv('Natural language'), 'Natural language')


if __name__ == '__main__':
    unittest.main()
