"""Synthetic tests for the learned weights: they move with the evidence and stay bounded.
Run: python3 -m unittest discover -s pipeline/tests   (no network, no Firestore)."""
import os, random, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import learnings as L

def land_site(i, tx, comm):
    parts = {"transmission": {"pts": 30 * tx, "max": 30}, "substation": {"pts": 7, "max": 15}, "gas": {"pts": 10, "max": 20},
             "scale": {"pts": 8, "max": 15}, "fiber": {"pts": 5, "max": 10}, "water": {"pts": 5, "max": 10}}
    return {"id": f"t{i}", "score_parts": parts, "community": {"score": comm}}

def stage(i, to, reasons=(), view="land"):
    return {"type": "stage", "view": view, "site_id": f"t{i}", "event_id": f"e{i}", "ts": f"2026-10-{1 + i % 27:02d}T00:00:00Z",
            "to_stage": to, "reasons": list(reasons)}

class LearnedWeights(unittest.TestCase):
    def synth(self, n, seed=1, cite=True):
        rnd = random.Random(seed); objs, ev = {}, []
        for i in range(n):
            tx = rnd.random(); objs[f"t{i}"] = land_site(i, tx, 50 + rnd.random() * 10)
            pursue = tx > 0.5
            ev.append(stage(i, "Reviewing" if pursue else "Passed", ["Power"] if cite and not pursue else ["Price"]))
        return objs, ev

    def test_no_decisions_means_base_weights(self):
        W = L.fit_track("land", [], {})
        self.assertEqual(W["n"], 0)
        self.assertTrue(all(d == 0 for d in W["deltas"].values()))

    def test_weights_move_toward_the_evidence(self):
        objs, ev = self.synth(60)
        W = L.fit_track("land", ev, objs)
        self.assertGreater(W["deltas"]["transmission"], 0.05)
        self.assertGreater(W["deltas"]["transmission"], W["deltas"]["fiber"])
        self.assertTrue(any("Transmission" in d["text"] for d in W["drivers"]))

    def test_bounded_even_with_extreme_evidence(self):
        objs, ev = self.synth(2000, cite=True)
        W = L.fit_track("land", ev, objs)
        for k, d in W["deltas"].items():
            self.assertLessEqual(abs(d), L.CAP + 1e-9, k)

    def test_small_samples_are_shrunk(self):
        objs, ev = self.synth(4); small = L.fit_track("land", ev, objs)["deltas"]["transmission"]
        objs, ev = self.synth(80); big = L.fit_track("land", ev, objs)["deltas"]["transmission"]
        self.assertLess(abs(small), abs(big))
        self.assertLess(abs(small), L.CAP * 4 / (4 + L.PRIOR_N) + 1e-9)

    def test_on_hold_and_restricted_are_ignored(self):
        objs, ev = self.synth(10)
        ev2 = ev + [dict(stage(99, "Reviewing"), restricted=True)]
        objs["t99"] = land_site(99, 1, 50)
        ev2 = [dict(e, to_stage="On Hold") if e["site_id"] == "t0" else e for e in ev2]
        self.assertEqual(L.fit_track("land", ev2, objs)["n"], 9)

    def test_learned_score_equals_base_when_unmoved(self):
        s = land_site(1, 0.5, 60)
        f = L.land_features(s)
        base = 0.6 * (15 + 7 + 10 + 8 + 5 + 5) + 0.4 * 60
        self.assertAlmostEqual(L.learned_score(f, {}), base, places=6)
        hi = L.learned_score(f, {"transmission": 0.25})
        self.assertNotAlmostEqual(hi, base)

    def test_power_and_storage_are_separate(self):
        objs, ev = self.synth(40)
        W = L.compute_weights(ev, objs, {})
        self.assertEqual(W["storage"]["n"], 0)
        self.assertTrue(all(d == 0 for d in W["storage"]["deltas"].values()))

if __name__ == "__main__":
    unittest.main()
