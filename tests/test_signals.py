import unittest
from datetime import timedelta

from src.long_lab.signals import (
    LateDataRejected,
    LedgerFinalized,
    RiskGateBlocked,
    SignalError,
    WindowExpired,
)

from .lab_factory import (
    add_signal,
    close_and_finalize_all,
    make_exposure,
    make_world,
    open_experiment,
    publish_classic,
    users,
)

EXP = "exp-classic-text"


class WindowRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = make_world()
        open_experiment(self.w)
        self.vid = publish_classic(self.w)
        self.u = users(1)[0]
        self.t0 = self.w.clock.now()

    def _expo(self, eid="e1", arm="treatment"):
        return make_exposure(self.w, eid, self.u, arm, self.vid, self.t0)

    def test_click_window_one_hour(self) -> None:
        expo = self._expo()
        add_signal(self.w, expo, "click", self.t0 + timedelta(minutes=50))
        expo2 = make_exposure(self.w, "e2", users(2)[1], "treatment", self.vid, self.t0)
        with self.assertRaises(WindowExpired):
            add_signal(self.w, expo2, "click", self.t0 + timedelta(hours=2))

    def test_save_then_save_open_24h(self) -> None:
        expo = self._expo()
        save_ts = self.t0 + timedelta(hours=2)
        add_signal(self.w, expo, "save", save_ts)
        # 24 小时内打开，有效
        add_signal(self.w, expo, "save_open", save_ts + timedelta(hours=20),
                   anchor_ts=save_ts)
        # 超过 24 小时无效（新曝光）
        expo2 = make_exposure(self.w, "e3", users(3)[2], "treatment", self.vid, self.t0)
        with self.assertRaises(WindowExpired):
            add_signal(self.w, expo2, "save_open", save_ts + timedelta(hours=25),
                       anchor_ts=save_ts)

    def test_cross_day_complete_requires_next_day_and_progress(self) -> None:
        expo = self._expo("e4")
        # 当天看完不算跨日
        with self.assertRaises(WindowExpired):
            add_signal(self.w, expo, "cross_day_complete",
                       self.t0 + timedelta(hours=3), progress=1.0)
        # 次日进度不足
        with self.assertRaises(WindowExpired):
            add_signal(self.w, expo, "cross_day_complete",
                       self.t0 + timedelta(days=1), progress=0.5)
        # 次日且进度 >= 0.9，有效
        add_signal(self.w, expo, "cross_day_complete",
                   self.t0 + timedelta(days=1, hours=2), progress=0.95)

    def test_effective_discussion_quality_and_deletion(self) -> None:
        expo = self._expo("e5")
        with self.assertRaises(WindowExpired):
            add_signal(self.w, expo, "effective_discussion",
                       self.t0 + timedelta(hours=2), quality_score=0.3)
        with self.assertRaises(SignalError):
            add_signal(self.w, expo, "effective_discussion",
                       self.t0 + timedelta(hours=2), quality_score=0.9, deleted=True)
        add_signal(self.w, expo, "effective_discussion",
                   self.t0 + timedelta(hours=2), quality_score=0.8)


class ProvenanceTest(unittest.TestCase):
    def test_reciprocal_operational_fan_signals_are_tagged_separately(self) -> None:
        from src.long_lab.detection import detect_provenance
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        w.content.declare_control("creator-laoke", ["creator-alt"], "control")
        w.refresh_catalog()
        t0 = w.clock.now()

        organic = make_exposure(w, "o1", users(10)[0], "treatment", vid, t0)
        self.assertEqual(detect_provenance(organic, w.catalog, "creator-laoke"), "organic")
        # 同一控制关系下的账号互刷
        recip = make_exposure(w, "o2", users(10)[1], "treatment", vid, t0)
        self.assertEqual(
            detect_provenance(recip, w.catalog, "creator-laoke",
                              viewer_creator_id="creator-alt"),
            "controlled_reciprocal")
        # 运营投放
        camp = make_exposure(w, "o3", users(10)[2], "treatment", vid, t0,
                             channel="operational_placement", campaign="camp-918")
        self.assertEqual(detect_provenance(camp, w.catalog, "creator-laoke"),
                         "operational_placement")
        # 粉丝集中回访
        fan = make_exposure(w, "o4", users(10)[3], "treatment", vid, t0)
        self.assertEqual(detect_provenance(fan, w.catalog, "creator-laoke",
                                           is_known_fan=True), "fan_concentrated")

    def test_reclassify_moves_count_out_of_organic(self) -> None:
        from src.long_lab.metrics import replay
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        t0 = w.clock.now()
        expo = make_exposure(w, "r1", users(11)[0], "treatment", vid, t0)
        add_signal(w, expo, "save", t0 + timedelta(hours=1))
        wid = f"r1:save"
        w.pipeline.reclassify(wid, "controlled_reciprocal", "风控识别为同控制组互刷")
        m = replay(w.store, w.catalog)[EXP]
        self.assertEqual(m.arms["treatment"].signals[("save", "organic")], 0)
        self.assertEqual(m.arms["treatment"].signals[("save", "controlled_reciprocal")], 1)


class LateDataAndFinalizationTest(unittest.TestCase):
    def test_late_signal_within_grace_corrects_unfinalized_window(self) -> None:
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        t0 = w.clock.now()
        make_exposure(w, "l1", users(12)[0], "treatment", vid, t0)
        # 点击窗口 1h 关闭；推进到关闭后、24h 宽限内，迟到点击到达
        w.advance(hours=20)
        expo = w.store.read_aggregate("exposure", "l1")[0].payload
        add_signal(w, expo, "click", t0 + timedelta(minutes=40))
        corrections = [e for e in w.store.read_all() if e.event_type == "WINDOW_PARTIALLY_CORRECTED"]
        self.assertEqual(len(corrections), 1)
        self.assertTrue(corrections[0].payload["applied"])

    def test_late_signal_after_finalization_rejected(self) -> None:
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        t0 = w.clock.now()
        make_exposure(w, "l2", users(13)[0], "treatment", vid, t0)
        close_and_finalize_all(w)  # 已过 7 天窗 + 24h 宽限
        expo = w.store.read_aggregate("exposure", "l2")[0].payload
        with self.assertRaises((LedgerFinalized, LateDataRejected)):
            add_signal(w, expo, "cross_day_complete",
                       t0 + timedelta(days=2), progress=0.95)

    def test_reclassify_after_finalization_rejected(self) -> None:
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        t0 = w.clock.now()
        expo = make_exposure(w, "l3", users(14)[0], "treatment", vid, t0)
        add_signal(w, expo, "save", t0 + timedelta(hours=1))
        close_and_finalize_all(w)
        with self.assertRaises(LedgerFinalized):
            w.pipeline.reclassify("l3:save", "fan_concentrated", "封账后改标")


class RiskGateTest(unittest.TestCase):
    def test_removed_content_blocked_everywhere_downrank_blocks_treatment_natural(self) -> None:
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        t0 = w.clock.now()
        # 先有反谣言降权
        w.content.apply_risk_action(vid, "downrank", "疑似谣言待核，反谣言系统降权",
                                    "anti_rumor", t0.isoformat())
        w.refresh_catalog()
        with self.assertRaises(RiskGateBlocked):
            make_exposure(w, "g1", users(20)[0], "treatment", vid, t0)
        # 对照组自然曝光保留（处置本身在两个臂一致生效，避免实验组变相放大）
        make_exposure(w, "g2", users(20)[1], "control", vid, t0)
        # 升级为下架后，任何曝光都拦截
        w.content.apply_risk_action(vid, "remove", "谣言核实成立，下架",
                                    "anti_rumor", (t0 + timedelta(hours=1)).isoformat())
        w.refresh_catalog()
        with self.assertRaises(RiskGateBlocked):
            make_exposure(w, "g3", users(20)[2], "control", vid,
                          t0 + timedelta(hours=2))

    def test_historical_risk_timeline_does_not_retroactively_gate(self) -> None:
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        t0 = w.clock.now()
        # 降权发生在曝光之后：曝光当时不应被拦
        make_exposure(w, "g4", users(21)[0], "treatment", vid, t0)
        w.content.apply_risk_action(vid, "downrank", "事后处置",
                                    "moderation", (t0 + timedelta(hours=1)).isoformat())
        w.refresh_catalog()
        with self.assertRaises(RiskGateBlocked):
            make_exposure(w, "g5", users(21)[1], "treatment", vid,
                          t0 + timedelta(hours=2))


if __name__ == "__main__":
    unittest.main()
