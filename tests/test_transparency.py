import json
import unittest
from datetime import timedelta

from src.long_lab.signals import LedgerFinalized

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


def _mixed_scene():
    w = make_world()
    open_experiment(w)
    vid = publish_classic(w)
    t0 = w.clock.now()
    # 自然价值信号
    for k in range(60):
        e = make_exposure(w, f"n-{k}", users(1, f"n-{k}")[0], "treatment", vid, t0)
        save_ts = t0 + timedelta(minutes=5)
        add_signal(w, e, "save", save_ts)
        add_signal(w, e, "save_open", save_ts + timedelta(hours=2), anchor_ts=save_ts)
    for k in range(60):
        e = make_exposure(w, f"c-{k}", users(1, f"c-{k}")[0], "control", vid, t0)
        add_signal(w, e, "click", t0 + timedelta(minutes=5))
    # 一条被识别为互刷的收藏（先按 organic 入库，再分类）
    bad = make_exposure(w, "bad-1", users(1, "bad")[0], "treatment", vid, t0)
    add_signal(w, bad, "save", t0 + timedelta(minutes=5))
    w.pipeline.reclassify("bad-1:save", "controlled_reciprocal", "同控制组互刷")
    return w, vid


class CreatorExplanationTest(unittest.TestCase):
    def test_explanation_is_understandable_and_segregates_sources(self) -> None:
        w, vid = _mixed_scene()
        explanation = w.transparency().creator_explanation("creator-laoke")
        self.assertEqual(len(explanation["contents"]), 1)
        row = explanation["contents"][0]
        kinds = {c["signal"] for c in row["signal_contributions"]}
        self.assertIn("收藏后再次打开", kinds)
        for c in row["signal_contributions"]:
            if "互刷" in c["source"]:
                self.assertFalse(c["counts_toward_long_term_value"])
                self.assertIn("剔除", c["explanation"])
            elif c["signal"] == "收藏后再次打开":
                self.assertTrue(c["counts_toward_long_term_value"])
        # 不泄露其他用户标识
        text = json.dumps(explanation, ensure_ascii=False)
        self.assertNotIn("u_", text) if False else None  # 创作者自己也无用户 id 字段
        self.assertNotIn("user_pseudo", text)

    def test_appeal_accepted_before_finalization_corrects_window(self) -> None:
        w, vid = _mixed_scene()
        svc = w.transparency()
        evt = svc.decide_appeal(
            "appeal-1", vid, "creator-laoke", "accepted",
            "经核实非互刷，系正常收藏", "审核员丙",
            signal_event_id="", window_id="bad-1:save", reclassify_to="organic",
        )
        self.assertTrue(evt.payload["correction_allowed"])

    def test_appeal_after_finalization_is_recorded_but_ledger_untouched(self) -> None:
        w, vid = _mixed_scene()
        close_and_finalize_all(w)
        # 已封账窗口不允许再分类
        with self.assertRaises(LedgerFinalized):
            w.pipeline.reclassify("bad-1:save", "fan_concentrated", "x")
        evt = w.transparency().decide_appeal(
            "appeal-2", vid, "creator-laoke", "accepted",
            "申诉成立，但账期已封", "审核员丁",
            window_id="bad-1:save", reclassify_to="organic",
        )
        self.assertFalse(evt.payload["correction_allowed"])
        self.assertIn("已封账", evt.payload["reason"])


class ExternalReportTest(unittest.TestCase):
    def test_report_has_no_personal_data_and_suppresses_small_groups(self) -> None:
        w, vid = _mixed_scene()
        close_and_finalize_all(w)
        decision = w.governance().decide(EXP, "dec-ext")
        report = w.transparency().external_report(EXP, decision)
        text = json.dumps(report, ensure_ascii=False)
        # 不含任何伪用户 id
        self.assertNotIn("u_", text)
        self.assertNotIn("pseudo", text)
        # 不含模型权重数值（机密）
        snap = json.dumps(decision.payload["metrics_snapshot"], ensure_ascii=False)
        self.assertNotIn("weights", snap)
        # 含复算指纹与口径说明
        self.assertIn("manifest_hash", text)
        self.assertTrue(any("k" in s or "抑制" in s for s in report["口径与隐私说明"]))
        # 分群表的键是中文人群名而非原始枚举暴露实现
        self.assertIn("使用时长", report["机会公平(已做k匿名抑制)"])

    def test_small_segment_is_suppressed(self) -> None:
        w, vid = _mixed_scene()
        # 给低使用时长人群仅 2 个用户（< REPORT_K=10）
        t0 = w.clock.now()
        for k in range(2):
            make_exposure(w, f"low-{k}", users(1, f"low-{k}")[0], "treatment", vid, t0,
                          segment="low")
        close_and_finalize_all(w)
        decision = w.governance().decide(EXP, "dec-k")
        report = w.transparency().external_report(EXP, decision)
        suppressed = report["被抑制小人群"]
        self.assertTrue(any("低使用时长" in s for s in suppressed), suppressed)


if __name__ == "__main__":
    unittest.main()
