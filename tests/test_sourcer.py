import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sourcer  # noqa: E402
from crustdata_api import Crustdata  # noqa: E402


def prime_role(**kw):
    r = {"name": "Lockheed Martin", "company_website": "https://www.lockheedmartin.com",
         "title": "Senior Software Engineer", "start_date": "2016-01-01T00:00:00",
         "end_date": "2021-03-01T00:00:00", "description": "Real-time C++ for radar, TS/SCI"}
    r.update(kw)
    return r


def current_role(**kw):
    r = {"name": "Helix Dynamics", "company_website": "https://helixdynamics.io", "title": "Staff Engineer",
         "start_date": "2021-04-01T00:00:00", "is_default": True, "company_headcount_range": "51-200",
         "company_type": "Privately Held", "seniority_level": "Senior",
         "company_professional_network_industry": "Defense and Space Manufacturing"}
    r.update(kw)
    return r


def profile(past=None, current=None, pid=1, country="United States"):
    return {
        "crustdata_person_id": pid,
        "basic_profile": {"name": f"Person {pid}", "headline": "Engineer",
                          "location": {"country": country, "state": "Colorado"}},
        "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.test/in/p{pid}"}},
        "experience": {"employment_details": {
            "past": [prime_role()] if past is None else past,
            "current": [current_role()] if current is None else current}},
    }


class QualifyTests(unittest.TestCase):
    def test_passes(self):
        rec = sourcer.qualify(profile())
        self.assertEqual(rec["prime"], "Lockheed Martin")
        self.assertEqual(rec["current_company"], "Helix Dynamics")
        self.assertEqual(rec["prime_years"], 5)
        self.assertEqual(rec["state"], "Colorado")

    def test_prime_matched_by_name_when_domain_missing(self):
        rec = sourcer.qualify(profile(past=[prime_role(name="Raytheon Missiles", company_website="")]))
        self.assertEqual(rec["prime"], "RTX / Raytheon")

    def test_reject_engineer_title_on_a_different_role(self):
        # prime role is a manager, the engineer title belongs to a non-prime role
        past = [prime_role(title="Program Manager"),
                prime_role(name="Some Startup", company_website="https://some.io", title="Engineer")]
        self.assertIsNone(sourcer.qualify(profile(past=past)))

    def test_reject_sales_engineer(self):
        self.assertIsNone(sourcer.qualify(profile(past=[prime_role(title="Sales Engineer")])))

    def test_reject_prime_role_without_end_date(self):
        self.assertIsNone(sourcer.qualify(profile(past=[prime_role(end_date=None)])))

    def test_reject_back_at_a_prime(self):
        cur = current_role(name="Boeing", company_website="https://boeing.com")
        self.assertIsNone(sourcer.qualify(profile(current=[cur])))

    def test_reject_large_current_employer(self):
        self.assertIsNone(sourcer.qualify(profile(current=[current_role(company_headcount_range="10001+")])))

    def test_reject_nonprofit_and_founders(self):
        self.assertIsNone(sourcer.qualify(profile(current=[current_role(company_type="Nonprofit")])))
        self.assertIsNone(sourcer.qualify(profile(current=[current_role(seniority_level="CXO")])))

    def test_reject_no_real_current_job(self):
        cur = current_role(title="Board Advisor")
        self.assertIsNone(sourcer.qualify(profile(current=[cur])))

    def test_reject_outside_us(self):
        self.assertIsNone(sourcer.qualify(profile(country="Canada")))


class ScoreTests(unittest.TestCase):
    def test_keyword_score_ranks_relevant_first(self):
        good = sourcer.qualify(profile(pid=1))
        weak = sourcer.qualify(profile(pid=2, past=[prime_role(title="Mechanical Engineer", description="CAD")]))
        ranked = sourcer.score_all([weak, good], "real-time C++ radar engineer")
        self.assertEqual(ranked[0]["person_id"], 1)
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertLessEqual(ranked[0]["score"], 100)

    def test_claude_score_is_parsed_and_falls_back(self):
        rec = sourcer.qualify(profile())
        old = sourcer.ask_claude
        try:
            sourcer.ask_claude = lambda *a, **k: 'Sure: {"score": 91, "reason": "radar C++"}'
            self.assertEqual(sourcer.claude_score(rec, "role")["score"], 91)
            sourcer.ask_claude = lambda *a, **k: "not json"
            self.assertIsNone(sourcer.claude_score(rec, "role"))
            sourcer.ask_claude = lambda *a, **k: None
            out = sourcer.score_all([rec], "C++ radar", use_claude=True)
            self.assertGreater(out[0]["score"], 0)  # keyword fallback
        finally:
            sourcer.ask_claude = old


class ApiTests(unittest.TestCase):
    def test_search_then_enrich_use_documented_endpoints(self):
        calls = []

        def transport(url, body, headers, timeout):
            calls.append((url, body, headers))
            if url.endswith("/person/search"):
                return {"profiles": [profile(pid=1), profile(pid=1), profile(pid=2, current=[current_role(company_headcount_range="10001+")])],
                        "next_cursor": None}
            urls = body["professional_network_profile_urls"]
            person = copy.deepcopy(profile(pid=1))
            person["skills"] = {"professional_network_skills": ["C++", "Radar"]}
            return [{"matched_on": urls[0], "match_status": "matched", "matches": [{"person_data": person}]}]

        client = Crustdata(api_key="test", transport=transport)
        rows = sourcer.search(client, 10)
        self.assertEqual([r["person_id"] for r in rows], [1])  # deduped, large employer dropped
        kept = sourcer.enrich_shortlist(client, rows)
        self.assertEqual(kept[0]["skills"], ["C++", "Radar"])
        self.assertTrue(calls[0][0].endswith("/person/search"))
        self.assertTrue(calls[1][0].endswith("/person/enrich"))
        self.assertEqual(calls[0][2]["x-api-version"], "2025-11-01")
        self.assertIn("professional_network_profile_urls", calls[1][1])

    def test_enrich_drops_people_who_moved_back_to_a_prime(self):
        rows = [sourcer.qualify(profile(pid=1))]
        moved = profile(pid=1, current=[current_role(name="Northrop Grumman", company_website="https://northropgrumman.com")])

        def transport(url, body, headers, timeout):
            return [{"matched_on": body["professional_network_profile_urls"][0], "match_status": "matched",
                     "matches": [{"person_data": moved}]}]

        self.assertEqual(sourcer.enrich_shortlist(Crustdata(api_key="t", transport=transport), rows), [])

    def test_filters_use_real_fields(self):
        f = sourcer.build_filters()
        fields = [c["field"] for c in f["conditions"]]
        self.assertIn("experience.employment_details.past.company_website_domain", fields)
        self.assertIn("basic_profile.location.country", fields)
        self.assertIn("experience.employment_details.current.company_headcount_range", fields)


class OutputTests(unittest.TestCase):
    def test_state_dedupe(self):
        rows = [sourcer.qualify(profile(pid=1)), sourcer.qualify(profile(pid=2))]
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "s", "seen.json")
            self.assertEqual(len(sourcer.only_new(rows, state)), 2)
            sourcer.save_state(rows[:1], state)
            self.assertEqual([r["person_id"] for r in sourcer.only_new(rows, state)], [2])

    def test_summary_merges_buckets_of_one(self):
        rows = [sourcer.qualify(profile(pid=i)) for i in range(3)]
        rows.append(sourcer.qualify(profile(pid=9, past=[prime_role(name="Boeing", company_website="https://boeing.com")])))
        sourcer.score_all(rows, "C++")
        s = sourcer.summary(rows)
        self.assertEqual(s["candidates"], 4)
        self.assertEqual(s["from_prime"], {"Lockheed Martin": 3, "other": 1})

    def test_slack_text_and_outputs(self):
        rows = sourcer.score_all([sourcer.qualify(profile(pid=1))], "C++ radar")
        self.assertIn("1 defense-prime alumni", sourcer.slack_text(rows))
        with tempfile.TemporaryDirectory() as d:
            sourcer.write_outputs(rows, d)
            self.assertTrue(os.path.exists(os.path.join(d, "shortlist.csv")))
            self.assertEqual(json.load(open(os.path.join(d, "shortlist.json")))["summary"]["candidates"], 1)


if __name__ == "__main__":
    unittest.main()
