import unittest
from pathlib import Path

from scraper import ExtractionError
from website_collect_cdp import parse_preview,parse_editorial_without_abstract


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.content = (Path(__file__).parent/"fixtures"/"issue-preview-contractarmor.html").read_text(encoding="utf-8")
        self.url = "https://www.sciencedirect.com/journal/procedia-computer-science/vol/231/suppl/C"

    def test_real_publisher_preview_preserves_source_and_cover_year(self):
        article = parse_preview(self.content,self.url,2024,"capture")
        self.assertEqual(article["pii"],"S1877050923021634")
        self.assertEqual(article["title"],"ContractArmor: Attack Surface Generator for Smart Contracts")
        self.assertEqual(article["doi"],"10.1016/j.procs.2023.12.151")
        self.assertEqual(article["year"],"2024")
        self.assertEqual(article["volume"],"231")
        self.assertTrue(article["abstract"].startswith("This paper presents"))
        self.assertTrue(article["abstract"].endswith("demonstrated."))
        self.assertNotIn("Article preview",article["abstract"])

    def test_closed_preview_or_recommendation_is_not_an_abstract(self):
        content=self.content.replace('js-abstract-body-text','recommendation-text')
        with self.assertRaises(ExtractionError):parse_preview(content,self.url,2024,"capture")

    def test_mismatched_pii_is_rejected(self):
        content=self.content.replace('id="S1877050923021634"','id="other"',1)
        with self.assertRaises(ExtractionError):parse_preview(content,self.url,2024,"capture")


class EditorialEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.content=(Path(__file__).parent/"fixtures"/"editorial-without-abstract.html").read_text(encoding="utf-8")
        self.row={"type":"Editorial","url":"https://www.sciencedirect.com/science/article/pii/S187705092401010X",
                  "title":"Keynote 1: Understanding the Chemistries of Lithium-Ion Batteries to Explain the Recent Implementation of Gigafactories in Morocco"}
        self.volume="https://www.sciencedirect.com/journal/procedia-computer-science/vol/236/suppl/C"

    def test_actual_empty_editorial_retains_title_without_inventing_abstract(self):
        record=parse_editorial_without_abstract(self.content,self.row,self.volume,2024,"capture")
        self.assertEqual(record["abstract"],"")
        self.assertEqual(record["abstract_status"],"not_provided_by_publisher")
        self.assertEqual(record["pii"],"S187705092401010X")

    def test_research_article_missing_abstract_cannot_use_editorial_exception(self):
        with self.assertRaises(ExtractionError):
            parse_editorial_without_abstract(self.content,{**self.row,"type":"Research article"},self.volume,2024,"capture")

    def test_wrong_journal_cannot_be_classified_as_success(self):
        with self.assertRaises(ExtractionError):
            parse_editorial_without_abstract(self.content.replace('Procedia Computer Science','other journal'),self.row,self.volume,2024,"capture")


if __name__=="__main__":unittest.main()
