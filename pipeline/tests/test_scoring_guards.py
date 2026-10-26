"""Floodplain penalty, reported (unverified) law items, manual sentiment, and enrich keeping prior fields."""
import datetime as dt, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import floodplain, scrape, community, edks


def infra_rec(pct=None):
    r = {"id": "t1", "acres": 400, "acres_suspect": False, "description": "",
         "power": {"lines_by_class": [{"class": 345, "kv": 345, "mi": 1.0, "owner": "x"}],
                   "subs_by_class": [{"class": 345, "kv": 345, "mi": 2.0, "name": "s"}]},
         "gasline": {"mi": 1.0, "operator": "x", "type": "Interstate"},
         "gas": {"provider": "", "line_in": None, "main_in": None, "psi": None}, "water": {"provider": "", "line_in": None, "main_in": None},
         "sewer": {"provider": ""}, "telecom": {"provider": "", "capability": "", "redundant": False}, "electric": []}
    if pct is not None:
        r["floodplain"] = {"pct": pct, "zones": ["AE"], "method": "site polygon (400 ac)"}
    return r


class Floodplain(unittest.TestCase):
    def test_penalty_scale(self):
        self.assertEqual(floodplain.penalty(0), 0)
        self.assertEqual(floodplain.penalty(4.9), 0)
        self.assertAlmostEqual(floodplain.penalty(10), 12)
        self.assertEqual(floodplain.penalty(92.4), 100)

    def test_score_drops(self):
        base = infra_rec(); scrape.score(base)
        mid = infra_rec(20); scrape.score(mid)
        bad = infra_rec(92.4); scrape.score(bad)
        self.assertGreater(base["score"], 40)
        self.assertAlmostEqual(mid["score"], max(0, base["score"] - 24), delta=1)
        self.assertLessEqual(bad["score"], 15)
        self.assertTrue(bad["score_parts"]["floodplain"]["penalty"])
        self.assertEqual(bad["score_parts"]["floodplain"]["max"], 0)

    def test_over_half_always_poor(self):
        r = infra_rec(51); scrape.score(r)
        self.assertLessEqual(r["score"], 15)

    def test_unknown_pct_no_penalty(self):
        r = infra_rec(); r["floodplain"] = {"pct": None, "note": "FEMA down"}; scrape.score(r)
        b = infra_rec(); scrape.score(b)
        self.assertEqual(r["score"], b["score"])


def site(**kw):
    s = {"id": "m-x", "county": "Geary", "manual": True, "score": 60,
         "annexation": {"status": "unincorporated", "place_base": None, "nearest_base": None}}
    s.update(kw); return s


class Community(unittest.TestCase):
    def setUp(self):
        community.LAWS, community.EDC = {}, {}
        self.today = dt.date(2026, 10, 26)

    def rep(self, st="active"):
        return [{"jurisdiction": "county:geary", "reg_status": st, "verified": False, "title": "County moratorium",
                 "date": "2026-10-21", "source": "email from named official", "official": "A. Official"}]

    def test_reported_counts_two_thirds_and_labelled(self):
        s0 = site(); community.score_site(s0, {}, self.today)
        s1 = site(reported_laws=self.rep()); community.score_site(s1, {}, self.today)
        self.assertEqual(s0["community"]["laws"]["score"] - s1["community"]["laws"]["score"], 40)
        self.assertTrue(any("reported, unverified" in w for w in s1["community"]["laws"]["why"]))
        self.assertEqual(len(s1["community"]["reported"]), 1)

    def test_verified_not_double_counted(self):
        ver = {"kind": "regulatory", "reg_status": "active", "jurisdiction": "county:geary", "date": "2026-09-01", "id": "v1", "url": "u"}
        s1 = site(reported_laws=self.rep()); community.score_site(s1, {"county:geary": [ver]}, self.today)
        s2 = site(); community.score_site(s2, {"county:geary": [ver]}, self.today)
        self.assertEqual(s1["community"]["laws"]["score"], s2["community"]["laws"]["score"])

    def test_site_only_issue_does_not_hit_other_sites(self):
        i = community.reported_issues(site(reported_laws=self.rep()))[0]
        other = site(id="m-other"); community.score_site(other, {"county:geary": [i]}, self.today)
        plain = site(id="m-other2"); community.score_site(plain, {}, self.today)
        self.assertEqual(other["community"]["laws"]["score"], plain["community"]["laws"]["score"])

    def test_manual_sentiment_moves_score(self):
        a = site(); community.score_site(a, {}, self.today)
        b = site(sentiment="negative"); community.score_site(b, {}, self.today)
        c = site(sentiment="positive"); community.score_site(c, {}, self.today)
        self.assertEqual(a["community"]["news"]["score"] - b["community"]["news"]["score"], 20)
        self.assertEqual(c["community"]["news"]["score"] - a["community"]["news"]["score"], 10)
        self.assertLess(b["overall"], a["overall"])

    def test_flood_cap_overall(self):
        s = site(score=100, floodplain={"pct": 80}); community.score_site(s, {}, self.today)
        self.assertLessEqual(s["overall"], 30)
        self.assertIn("floodplain", s["overall_cap"])


class EnrichKeep(unittest.TestCase):
    def test_failed_tracker_keeps_outreach(self):
        new = [{"id": "a", "score": 10, "community": {"score": 50}}]
        prev = {"a": {"outreach": {"next_step": {"text": "keep me"}}}}
        n = edks.keep_prior(new, prev, {"community": True, "tracker": False})
        self.assertEqual(n, 1)
        self.assertEqual(new[0]["outreach"]["next_step"]["text"], "keep me")
        self.assertEqual(new[0]["stale"], ["tracker"])

    def test_failed_community_keeps_and_reblends(self):
        new = [{"id": "a", "score": 0, "floodplain": {"pct": 90}}]
        prev = {"a": {"community": {"score": 50}, "outreach": {"x": 1}, "overall": 72}}
        edks.keep_prior(new, prev, {"community": False, "tracker": False})
        self.assertEqual(new[0]["community"]["score"], 50)
        self.assertEqual(new[0]["overall"], 20)
        self.assertEqual(new[0]["outreach"], {"x": 1})

    def test_all_ok_changes_nothing(self):
        new = [{"id": "a", "score": 10}]
        self.assertEqual(edks.keep_prior(new, {"a": {"outreach": {}}}, {"community": True, "tracker": True}), 0)
        self.assertNotIn("outreach", new[0])


if __name__ == "__main__":
    unittest.main()
