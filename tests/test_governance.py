import unittest
from datetime import timedelta

from src.long_lab.audit import AuditError, recompute_decision
from src.long_lab.events import EventStore
from src.long_lab.metrics import replay

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


def generate_traffic(world, *, per_arm, vid="cv-laoke-001",
                     treatment_organic_kinds=("save", "save_open", "cross_day_complete"),
                     control_organic_kinds=("click",),
                     segment="medium", channel="natural",
                     provenance="organic", start_index=0):
    """按给定模式批量造曝光+信号，返回使用过的最大编号。"""
    t0 = world.clock.now()
    i = start_index
    for arm, kinds in (("control", control_organic_kinds), ("treatment", treatment_organic_kinds)):
        for j in range(per_arm):
            u = users(1, f"gen-{i:05d}")[0]; i += 1
            eid = f"gen-{i:05d}"
            expo = make_exposure(world, eid, u, arm, vid, t0, segment=segment, channel=channel)
            kind = kinds[j % len(kinds)]
            ts = t0 + timedelta(minutes=10)
            kw = {"provenance": provenance}
            if kind == "cross_day_complete":
                ts = t0 + timedelta(days=1)
                kw["progress"] = 0.95
            elif kind == "effective_discussion":
                kw["quality_score"] = 0.8
            elif kind == "save_open":
                save_ts = t0 + timedelta(minutes=5)
                add_signal(world, expo, "save", save_ts, provenance=provenance)
                kw["anchor_ts"] = save_ts
            add_signal(world, expo, kind, ts, **kw)
    return i


class GovernanceDecisionTest(unittest.TestCase):
    def test_genuine_organic_lift_scales_and_is_recomputable(self) -> None:
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        generate_traffic(w, per_arm=80)
        close_and_finalize_all(w)
        gov = w.governance()
        decision = gov.decide(EXP, "dec-1", reviewer="审查员甲")
        self.assertEqual(decision.payload["action"], "scale", decision.payload["rationale"])

        # 复算：指纹校验通过，动作一致
        result = recompute_decision(w.store, "dec-1")
        self.assertTrue(result["manifest_hash_verified"])
        self.assertEqual(result["recorded_action"], "scale")
        self.assertGreater(result["recomputed"]["lift"], 0)

        # 合法的事后追加不影响复算，但会被列在 later_events 中
        w.content.apply_risk_action(vid, "label", "事后加标签", "moderation")
        result2 = recompute_decision(w.store, "dec-1")
        self.assertTrue(result2["manifest_hash_verified"])

        # 直接篡改日志文件中的历史 payload，复算必须失败
        import json
        path = w.store.path
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            d = json.loads(line)
            if d["event_type"] == "SIGNAL_RECORDED":
                d["payload"]["provenance"] = "controlled_reciprocal"
                lines[i] = json.dumps(d, ensure_ascii=False)
                break
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(AuditError):
            recompute_decision(EventStore(path, clock=w.clock), "dec-1")

    def test_operational_inflation_cannot_fake_value(self) -> None:
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        # 对照组自然点击；实验组全部由运营投放驱动的收藏——不进有机价值
        generate_traffic(
            w, per_arm=80,
            treatment_organic_kinds=("save",),
            control_organic_kinds=("save",),
            channel="natural",
        )
        # 再给实验组追加大量运营来源信号（非有机），使非自然占比差异超门栏
        t0 = w.clock.now()
        for k in range(120):
            u = users(1, f"camp-{k}")[0]
            expo = make_exposure(w, f"camp-e-{k}", u, "treatment", vid, t0,
                                 channel="operational_placement", campaign="c1")
            add_signal(w, expo, "save", t0 + timedelta(minutes=10),
                       provenance="operational_placement")
        close_and_finalize_all(w)
        decision = w.governance().decide(EXP, "dec-op", reviewer="审查员乙")
        self.assertEqual(decision.payload["action"], "rollback")
        self.assertIn("非自然", decision.payload["rationale"])

    def test_harm_resurgence_triggers_kill_switch_and_rollback(self) -> None:
        w = make_world()
        open_experiment(w)
        vid = publish_classic(w)
        t0 = w.clock.now()
        # 对照组少量风险内容曝光，实验组大量重新放大已标签内容 → 熔断
        other = "cv-risky"
        w.content.publish_version(other, "c-risk", "creator-r", "争议视频",
                                  ["争议"], False, 600,
                                  published_at=(t0 - timedelta(days=60)).isoformat())
        w.refresh_catalog()
        w.content.apply_risk_action(other, "label", "反谣言标记", "anti_rumor", t0.isoformat())
        w.refresh_catalog()
        for k in range(100):
            arm = "treatment" if k % 5 else "control"
            make_exposure(w, f"h-{k}", users(1, f"h-{k}")[0], arm, other, t0)
        for k in range(80):
            make_exposure(w, f"hb-{k}", users(1, f"hb-{k}")[0], "control", vid, t0)
            make_exposure(w, f"hg-{k}", users(1, f"hg-{k}")[0], "treatment", vid, t0)
        trigger = w.governance().check_harm(EXP)
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.payload["trigger"], "harm_surface_rate")
        # 止损自动暂停分桶
        self.assertEqual(w.experiments.state(EXP).status, "paused")
        close_and_finalize_all(w)
        decision = w.governance().decide(EXP, "dec-kill")
        self.assertEqual(decision.payload["action"], "rollback")

    def test_insufficient_evidence_holds(self) -> None:
        w = make_world()
        open_experiment(w)
        publish_classic(w)
        generate_traffic(w, per_arm=10)
        close_and_finalize_all(w)
        decision = w.governance().decide(EXP, "dec-hold")
        self.assertEqual(decision.payload["action"], "hold")
        self.assertIn("证据不足", decision.payload["rationale"])


class FairnessTest(unittest.TestCase):
    def test_new_author_opportunity_gap_blocks_scale(self) -> None:
        from src.long_lab.governance import fairness_report
        w = make_world()
        open_experiment(w)
        vid_old = publish_classic(w)
        # 新作者内容（首次发布就在今天）
        vid_new = "cv-newbie"
        w.content.publish_version(vid_new, "c-new", "creator-new", "新人冷门讲解",
                                  ["语文"], True, 1800,
                                  published_at=w.clock.now().isoformat())
        w.refresh_catalog()
        t0 = w.clock.now()
        # 两组成熟作者内容曝光相近
        for k in range(80):
            make_exposure(w, f"oc-{k}", users(1, f"oc-{k}")[0], "control", vid_old, t0)
            make_exposure(w, f"ot-{k}", users(1, f"ot-{k}")[0], "treatment", vid_old, t0)
        # 新作者内容：对照组有曝光，实验组几乎不给 → 机会差异
        for k in range(40):
            make_exposure(w, f"nc-{k}", users(1, f"nc-{k}")[0], "control", vid_new, t0)
        for k in range(2):
            make_exposure(w, f"nt-{k}", users(1, f"nt-{k}")[0], "treatment", vid_new, t0)
        metrics = replay(w.store, w.catalog)[EXP]
        report = fairness_report(metrics)
        self.assertTrue(any("author_tenure=new" in v or "新" in v for v in report.violations),
                        report.violations)
        # 治理快照可落事件
        snap = w.governance().snapshot_fairness(EXP, t0.isoformat(), w.clock.now().isoformat())
        self.assertIn("violations", snap.payload["segments"])

    def test_genuine_lift_is_held_when_new_author_gap_exists(self) -> None:
        """有机提升真实且显著，但新作者机会差异超门栏 → 不允许扩量。"""
        w = make_world()
        open_experiment(w)
        vid_old = publish_classic(w)
        vid_new = "cv-newbie"
        w.content.publish_version(vid_new, "c-new", "creator-new", "新人冷门讲解",
                                  ["语文"], True, 1800,
                                  published_at=w.clock.now().isoformat())
        w.refresh_catalog()
        t0 = w.clock.now()
        # 成熟内容：control 点击 vs treatment 收藏+跨日看完（显著有机提升）
        for k in range(70):
            ec = make_exposure(w, f"oc-{k}", users(1, f"oc-{k}")[0], "control", vid_old, t0)
            add_signal(w, ec, "click", t0 + timedelta(minutes=10))
            et = make_exposure(w, f"ot-{k}", users(1, f"ot-{k}")[0], "treatment", vid_old, t0)
            add_signal(w, et, "save", t0 + timedelta(hours=1))
            add_signal(w, et, "cross_day_complete", t0 + timedelta(days=1), progress=0.95)
        # 新作者：control 有 30 次曝光，treatment 仅 2 次 → 机会差异
        for k in range(30):
            en = make_exposure(w, f"nc-{k}", users(1, f"nc-{k}")[0], "control", vid_new, t0)
            add_signal(w, en, "click", t0 + timedelta(minutes=10))
        for k in range(2):
            make_exposure(w, f"nt-{k}", users(1, f"nt-{k}")[0], "treatment", vid_new, t0)
        close_and_finalize_all(w)
        decision = w.governance().decide(EXP, "dec-fair")
        self.assertEqual(decision.payload["action"], "hold")
        self.assertIn("公平性", decision.payload["rationale"])
        # 提升本身是真实的（价值分确实上去），只是被公平性门栏拦下
        self.assertGreater(decision.payload["metrics_snapshot"]["lift"], 0.03)


if __name__ == "__main__":
    unittest.main()
