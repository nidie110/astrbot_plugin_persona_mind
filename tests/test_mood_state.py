"""Offline state round trips and rolling-window invariants; no real bot or model."""
import json
import unittest
from unittest.mock import patch

from test_v2_regressions import load_plugin

_, engine, _ = load_plugin()
Mood, Affect = engine.Mood, engine.Affect
CoreEvent = engine.affect.CoreEvent


class MoodStateTests(unittest.TestCase):
    def setUp(self):
        self.now = 10_000.0
        self.clock = patch.object(engine.affect.time, "time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def mood(self):
        return Mood(core=Affect(ts=self.now), _core_ts=self.now, _last_decay=self.now)

    def restored(self, mood):
        other = self.mood()
        other.load_state(json.loads(json.dumps(mood.dump_state())))
        return other

    def test_rebase_then_positive_impulse_does_not_reverse_above_100(self):
        m = self.mood()
        m.rebase({"P": 17.5})
        m.core.P += 30.0
        m.rebase({"P": 82.5})
        self.assertEqual(m.core.P, 112.5)
        m.apply({"P": 10}, umo="A")
        self.assertAlmostEqual(m.core.P, 116.5)
        m.rebase({"P": 17.5})
        self.assertAlmostEqual(m.core.P, 51.5)
        self.assertLessEqual(m.effective("A")["P"], 100)

    def test_rebase_then_negative_impulse_does_not_reverse_below_zero(self):
        m = self.mood()
        m.rebase({"P": 82.5})
        m.core.P -= 30.0
        m.rebase({"P": 17.5})
        m.apply({"P": -10}, umo="A")
        self.assertAlmostEqual(m.core.P, -16.5)
        m.rebase({"P": 82.5})
        self.assertAlmostEqual(m.core.P, 48.5)
        self.assertGreaterEqual(m.effective("A")["P"], 0)

    def test_roundtrip_keeps_out_of_display_range_and_full_precision(self):
        m = self.mood()
        m.rebase({"P": 82.5})
        m.core.P += 30.0123456789
        other = self.restored(m)
        self.assertAlmostEqual(other.core.P, m.core.P, places=10)
        self.assertEqual(other.core.base, m.core.base)
        other.rebase({"P": 17.5})
        self.assertAlmostEqual(other.core.P, 47.5123456789, places=10)

    def test_restart_with_same_relation_creates_no_mood(self):
        m = self.mood()
        base = engine.base_from_field(90, 70, 30, 80)
        m.rebase(base)
        before = m.effective("A")
        other = self.restored(m)
        other.rebase(base)
        self.assertEqual(other.effective("A"), before)

    def test_positive_negative_quotas_are_independent_across_expiry(self):
        m = self.mood()
        for i in range(3):
            m.apply(engine.OCC_EFFECTS["Joy"], umo=f"J{i}")
        self.now += 60
        m.decay()
        for i in range(6):
            m.apply(engine.OCC_EFFECTS["Anger"], umo=f"A{i}")
        positive = sum(max(0, ev.granted.get("P", 0)) for ev in m._core_hist)
        negative = sum(min(0, ev.granted.get("P", 0)) for ev in m._core_hist)
        self.assertAlmostEqual(positive, 12)
        self.assertAlmostEqual(negative, -12)
        self.now += 541
        m.decay()
        before = m.core.P
        m.apply(engine.OCC_EFFECTS["Anger"], umo="new")
        self.assertEqual(m.core.P, before)
        self.now += 60
        m.decay()
        before = m.core.P
        m.apply(engine.OCC_EFFECTS["Anger"], umo="newer")
        self.assertAlmostEqual(m.core.P - before, -4)

    def test_legacy_overfull_history_cannot_reverse_or_amplify_input(self):
        for history, want in ((24.0, 4.0), (-24.0, -4.0), (24.0, 0.0)):
            with self.subTest(history=history, want=want):
                m = self.mood()
                m._core_hist = [CoreEvent(self.now, "old", 1, {"P": history}, {})]
                granted = m._core_quota({"P": want})["P"]
                self.assertEqual(granted, 0)

    def test_restart_preserves_quota_and_duplicate_discount(self):
        m = self.mood()
        m.apply({"P": 10}, umo="A")
        other = self.restored(m)
        for obj in (m, other):
            before = obj.core.P
            obj.apply({"P": 10}, umo="A")
            self.assertAlmostEqual(obj.core.P - before, 2.0)
        self.assertEqual(m.dump_state(), other.dump_state())
        for i in range(3):
            m.apply({"P": 10}, umo=f"B{i}")
        other = self.restored(m)
        before = other.core.P
        other.apply({"P": 10}, umo="unused")
        self.assertEqual(other.core.P, before)

    def test_only_live_valid_history_is_restored_in_time_order(self):
        data = self.mood().dump_state()
        data["core_hist"] = [
            {"ts": self.now - 10, "umo": "A", "sign": 1, "granted": {"P": 4}, "actual": {"P": 4}},
            {"ts": self.now - 20, "umo": "B", "sign": -1, "granted": {"P": -4}, "actual": {"P": -4}},
            {"ts": self.now - 600, "umo": "expired", "sign": 1, "granted": {"P": 4}},
            {"ts": self.now + 10, "umo": "future", "sign": 1, "granted": {"P": 4}},
            {"ts": "bad", "granted": {"P": 4}},
            {"ts": self.now, "granted": {"P": float("nan")}},
            {"ts": self.now, "granted": []}, None,
        ]
        m = self.mood()
        m.load_state(data)
        self.assertEqual([ev.umo for ev in m._core_hist], ["B", "A"])
        self.now += 601
        self.assertEqual(m.dump_state()["core_hist"], [])

    def test_legacy_absolute_state_uses_saved_or_callers_baseline(self):
        m = self.mood()
        m.core.base = {"P": 65.0, "Ar": 45.0, "D": 50.0}
        m.load_state({"core": {"P": 70.0, "ts": self.now}})
        m.rebase({"P": 60.0})
        self.assertEqual(m.core.P, 65.0)
        m.load_state({"core": {"P": 73.0, "base": {"P": 69.0}, "ts": self.now}})
        self.assertEqual(m.core.base["P"], 69.0)
        self.assertEqual(m.core.P, 73.0)

    def test_restart_does_not_reintroduce_high_frequency_rounding(self):
        values = []
        for step in (1, 10, 60, 7200):
            self.now = 10_000.0
            m = self.mood()
            m.core.P = 70.0
            m.scenes["A"] = {"P": 10.0, "Ar": 0.0, "D": 0.0, "ts": self.now}
            for _ in range(7200 // step):
                self.now += step
                m.decay()
                m = self.restored(m)
            values.append((m.core.P, m.scenes["A"]["P"]))
        for core, scene in values:
            self.assertAlmostEqual(core, 65.0, places=8)
            self.assertAlmostEqual(scene, 0.625, places=8)

    def test_offset_boundary_and_saturated_history_survive_restart(self):
        m = self.mood()
        m.core.P = m.core.base["P"] + 60.0
        m.apply({"P": 10}, umo="A")
        self.assertEqual(m.core.P - m.core.base["P"], 60.0)
        self.assertEqual(m._core_hist[-1].granted["P"], 4.0)
        self.assertEqual(m._core_hist[-1].actual["P"], 0.0)
        other = self.restored(m)
        self.assertEqual(other.core.P, m.core.P)
        self.assertEqual(other._core_hist[-1].granted["P"], 4.0)

    def test_negative_clamp_disables_both_layers_without_using_quota(self):
        m = self.mood()
        before = m.effective("A")
        m.apply({"P": 10}, umo="A", clamp=-2)
        self.assertEqual(m.effective("A"), before)
        self.assertEqual(m._core_hist, [])

    def test_new_impulse_is_applied_after_elapsed_decay(self):
        m = self.mood()
        m.core.P = 70.0
        m.scenes["A"] = {"P": 10.0, "Ar": 0.0, "D": 0.0, "ts": self.now}
        self.now += 3600
        m.apply({"P": 10}, umo="A")
        self.assertAlmostEqual(m.core.P, 60 + 10 * 0.5 ** 0.5 + 4)
        self.assertAlmostEqual(m.scenes["A"]["P"], 12.5)
        before = m.core.P
        m.decay()
        self.assertEqual(m.core.P, before)

    def test_many_other_events_cannot_evict_live_quota(self):
        m = self.mood()
        m._core_hist = [CoreEvent(self.now, "full", 1, {"P": 12}, {"P": 12})]
        m._core_hist.extend(CoreEvent(self.now, f"tiny{i}", 1, {"D": 0.001}, {"D": 0.001})
                            for i in range(2000))
        m.apply({"D": 0.001}, umo="another")
        other = self.restored(m)
        before = other.core.P
        other.apply({"P": 10}, umo="new")
        self.assertEqual(other.core.P, before)


if __name__ == "__main__":
    unittest.main()
