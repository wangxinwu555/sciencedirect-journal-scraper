import json
import shutil
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from unittest.mock import MagicMock,patch

import website_collect as collector


@contextmanager
def output_directory():
    root=Path(__file__).resolve().parent
    target=root/("website-test-"+uuid4().hex)
    target.mkdir()
    try:yield target
    finally:
        if target.resolve().parent != root or not target.name.startswith("website-test-"):
            raise ValueError("Unsafe cleanup path")
        shutil.rmtree(target)


class WebsiteTests(unittest.TestCase):
    def test_checkbox_attempt_is_bounded_and_not_recorded_as_article_success(self):
        with sqlite3.connect(":memory:") as db:
            collector.initialize(db,2024)
            page=MagicMock()
            frame=MagicMock()
            frame.url="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/test"
            frame.get_by_role.return_value.count.return_value=1
            frame.get_by_role.return_value.is_visible.return_value=True
            page.frames=[frame]
            collector.CAPTCHA_CLICKS_REMAINING=1
            with patch.object(collector,"evidence"):
                self.assertTrue(collector.attempt_turnstile_checkbox(page,db,Path("unused"),collector.ISSUES_URL))
                self.assertFalse(collector.attempt_turnstile_checkbox(page,db,Path("unused"),collector.ISSUES_URL))
            frame.get_by_role.return_value.click.assert_called_once()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM articles").fetchone()[0],0)
            self.assertEqual(collector.CAPTCHA_CLICKS_REMAINING,0)

    def test_real_challenge_and_access_denial_are_different(self):
        self.assertEqual(collector.challenge_reason("Are you a robot? Please complete the captcha challenge"),
                         "interactive-captcha")
        self.assertEqual(collector.challenge_reason("There was a problem providing the content you requested"),
                         "access-denied")
        self.assertEqual(collector.challenge_reason("Procedia Computer Science All issues"), "")

    def test_untrusted_source_or_credentials_cannot_be_followed(self):
        for url in ["https://sciencedirect.com.evil.invalid/article", "https://user:password@www.sciencedirect.com/",
                    "http://www.sciencedirect.com/", "https://www.sciencedirect.com:8443/"]:
            with self.assertRaises(ValueError):collector.require_website_url(url)
        collector.require_website_url(collector.ISSUES_URL)

    def test_no_false_completion_when_blocked_pending_or_without_directory(self):
        with output_directory() as output,sqlite3.connect(":memory:") as db:
            collector.initialize(db,2024)
            collector.export(db,output,2024)
            report=lambda:json.loads((output/"report.json").read_text(encoding="utf-8"))
            self.assertFalse(report()["complete"])
            db.execute("INSERT INTO volumes VALUES ('https://www.sciencedirect.com/test','test',1)")
            db.execute("INSERT INTO jobs(pii,url,title,volume_url,status) VALUES ('test','test','test','test','blocked')")
            db.commit()
            collector.export(db,output,2024)
            self.assertFalse(report()["complete"])
            self.assertEqual(report()["articles_with_abstract"],0)
            db.execute("UPDATE jobs SET status='pending'");db.commit()
            collector.export(db,output,2024)
            self.assertFalse(report()["complete"])

    def test_journal_year_output_is_isolated(self):
        with sqlite3.connect(":memory:") as db:
            collector.initialize(db,2024)
            with self.assertRaises(ValueError):collector.initialize(db,2025)

    def test_completed_flags_cannot_hide_empty_or_incomplete_page_inventory(self):
        with output_directory() as output,sqlite3.connect(":memory:") as db:
            collector.initialize(db,2024)
            db.execute("INSERT INTO volumes VALUES ('volume','volume',1)");db.commit()
            collector.export(db,output,2024)
            self.assertFalse(json.loads((output/"report.json").read_text(encoding="utf-8"))["complete"])
            db.execute("INSERT INTO jobs(pii,url,title,volume_url,status) VALUES ('contents','url','Contents','volume','excluded')")
            db.execute("INSERT INTO issue_pages VALUES ('volume',1,'capture',2)");db.commit()
            collector.export(db,output,2024)
            self.assertFalse(json.loads((output/"report.json").read_text(encoding="utf-8"))["complete"])
            db.execute("UPDATE issue_pages SET row_count=1");db.commit()
            collector.export(db,output,2024)
            self.assertTrue(json.loads((output/"report.json").read_text(encoding="utf-8"))["complete"])


if __name__=="__main__":unittest.main()
