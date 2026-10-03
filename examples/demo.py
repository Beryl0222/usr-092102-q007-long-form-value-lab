"""端到端叙事演示：经典课文长视频长期价值实验。

运行：python3 -m examples.demo
展示完整闭环：
  质疑（表面提升？）→ 分桶实验 → 五类信号各自计时窗口 → 非自然来源单独标记
  → 迟到数据只修正未封账窗口 → 反谣言降权不被长期指标重新放大 → 熔断暂停/恢复无跨组污染
  → 公平性监测 → 可复算的扩量决策 → 创作者解释/申诉 → 隐私安全外部报告 → 审计复算
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.long_lab.audit import recompute_decision
from src.long_lab.catalog import ContentCatalog
from src.long_lab.content import ContentService
from src.long_lab.detection import detect_provenance
from src.long_lab.events import EventStore
from src.long_lab.experiments import ExperimentService, audit_contamination
from src.long_lab.governance import GovernanceService
from src.long_lab.identity import pseudonymize
from src.long_lab.objective import DEFAULT_WEIGHTS, ObjectiveRegistry
from src.long_lab.signals import RiskGateBlocked, SignalPipeline
from src.long_lab.timekeeping import Clock
from src.long_lab.transparency import TransparencyService

EXP = "exp-classic-text"


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="longlab-demo-"))
    clock = Clock(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))
    store = EventStore(tmp / "events.jsonl", clock=clock)
    content = ContentService(store)
    catalog = ContentCatalog()
    pipeline = SignalPipeline(store, catalog)
    experiments = ExperimentService(store)

    def refresh():
        nonlocal catalog
        catalog = ContentCatalog(); catalog.load_many(store.read_all())
        pipeline.catalog = catalog

    # 1) 目标函数 v1：收藏/回访/讨论入模，点击弱权重
    obj = ObjectiveRegistry(store).publish("obj-long-value", DEFAULT_WEIGHTS,
                                           "收藏、回访、有效讨论的长期价值 v1")

    # 2) 内容与创作者关系：经典课文长视频 + 一个疑似同控制的账号
    t0 = clock.now()
    content.publish_version("cv-beiying-v1", "c-beiying", "creator-laoke",
                            "背影·课文精讲（长视频）", ["语文", "经典课文"], False, 2700,
                            published_at=(t0 - timedelta(days=90)).isoformat())
    content.declare_control("creator-laoke", ["creator-alt"], "control")
    refresh()

    # 3) 开轮 50/50
    experiments.open_experiment(
        EXP, "经典课文长视频长期价值实验", "salt-2026-09",
        {"control": [0], "treatment": [1]}, obj.version, start_at=t0.isoformat())

    organic_users = [pseudonymize(f"reader-{i}") for i in range(240)]
    alt = pseudonymize("alt-account")

    # 4) 流量：先确定性分桶（暂停/恢复语义因此成立），对照组以点击为主；
    #    实验组产生大量真实延迟价值信号
    assignments = {u: experiments.assign(EXP, u).payload["arm"] for u in organic_users}
    for i, u in enumerate(organic_users):
        arm = assignments[u]
        eid = f"ex-{i:03d}"
        segment = ("low" if i < 16 else "medium" if i < 144 else "high")
        expo = pipeline.record_exposure(
            eid, "cv-beiying-v1", u, EXP, arm, t0.isoformat(), segment).payload
        if arm == "control":
            pipeline.record_signal(expo, "click", (t0 + timedelta(minutes=20)).isoformat())
        else:
            save_ts = t0 + timedelta(hours=2)
            pipeline.record_signal(expo, "save", save_ts.isoformat())
            # 跨日看完：次日、进度 96%
            pipeline.record_signal(
                expo, "cross_day_complete", (t0 + timedelta(days=1, hours=1)).isoformat(),
                progress=0.96)
            if i % 4 == 1:
                pipeline.record_signal(
                    expo, "effective_discussion", (t0 + timedelta(hours=10)).isoformat(),
                    quality_score=0.82)

    # 5) 试图“制造提升”的三种行为被分别标记
    camp_expo = pipeline.record_exposure(
        "ex-camp", "cv-beiying-v1", pseudonymize("camp-user"), EXP, "treatment",
        t0.isoformat(), "medium", delivery_channel="operational_placement",
        campaign_id="918-campaign").payload
    pipeline.record_signal(camp_expo, "save", (t0 + timedelta(minutes=10)).isoformat(),
                           provenance="operational_placement")

    recip_expo = pipeline.record_exposure(
        "ex-recip", "cv-beiying-v1", alt, EXP, "treatment", t0.isoformat(), "medium").payload
    provenance = detect_provenance(recip_expo, catalog, "creator-laoke",
                                   viewer_creator_id="creator-alt")
    pipeline.record_signal(recip_expo, "save", (t0 + timedelta(minutes=10)).isoformat(),
                           provenance=provenance)

    fan_expo = pipeline.record_exposure(
        "ex-fan", "cv-beiying-v1", pseudonymize("fan-1"), EXP, "treatment",
        t0.isoformat(), "high").payload
    pipeline.record_signal(fan_expo, "save", (t0 + timedelta(minutes=10)).isoformat(),
                           provenance="fan_concentrated")

    # 6) 迟到数据：窗口关闭后、24h 宽限内到达 → 只产生纠正事件，不改写历史
    clock.advance(hours=20)
    late_expo = store.read_aggregate("exposure", "ex-000")[0].payload
    pipeline.record_signal(late_expo, "click", (t0 + timedelta(minutes=40)).isoformat())
    corrections = [e for e in store.read_all() if e.event_type == "WINDOW_PARTIALLY_CORRECTED"]
    print("迟到点击（宽限内）纠正事件数：", len(corrections))

    # 7) 反谣言降权：有害内容不得在实验组被重新放大
    content.publish_version("cv-rumor-v1", "c-rumor", "creator-r", "热点解读（待核）",
                            ["热点"], False, 600, published_at=t0.isoformat())
    refresh()
    content.apply_risk_action("cv-rumor-v1", "downrank", "疑似谣言，反谣言系统降权",
                              "anti_rumor", clock.now().isoformat())
    refresh()
    try:
        pipeline.record_exposure("ex-rumor-t", "cv-rumor-v1", pseudonymize("rumor-watch-1"),
                                 EXP, "treatment", clock.now().isoformat(), "medium")
    except RiskGateBlocked as e:
        print("风险门栏拦截：", e)

    # 8) 紧急止损演练 + 恢复（模拟规则变更暂停；恢复不换盐 → 无跨组污染）
    experiments.pause(EXP, "rule_change")
    try:
        experiments.assign(EXP, pseudonymize("during-pause"))
    except Exception as e:  # noqa: BLE001
        print("暂停期间分桶被拒：", e)
    clock.advance(hours=4)
    experiments.resume(EXP)
    for i, u in enumerate(organic_users[:20]):
        first = experiments.assign(EXP, u).payload["arm"]
        again = experiments.assign(EXP, u).payload["arm"]
        assert first == again
    assert audit_contamination(store) == []

    # 9) 关窗、封账，然后做公平性快照与决策
    clock.advance(days=8)
    pipeline.close_due_windows()
    clock.advance(hours=25)
    pipeline.finalize_due_windows()
    refresh()
    # 封账后迟到数据必须被拒
    expired_expo = store.read_aggregate("exposure", "ex-002")[0].payload
    try:
        pipeline.record_signal(expired_expo, "click", (t0 + timedelta(minutes=40)).isoformat())
    except Exception as e:  # noqa: BLE001
        print("封账后迟到数据被拒：", type(e).__name__)

    gov = GovernanceService(store, catalog, obj.objective_id)
    gov.snapshot_fairness(EXP, t0.isoformat(), clock.now().isoformat())
    decision = gov.decide(EXP, "dec-2026-09-28", reviewer="治理值班人")
    print("\n=== 策略决策 ===")
    print("动作：", decision.payload["action"])
    print("理由：", decision.payload["rationale"])
    print("复算指纹：", decision.payload["manifest_hash"][:16], "…")

    # 10) 创作者看到可理解的信号贡献，并对互刷标记发起申诉
    transparency = TransparencyService(store, catalog, DEFAULT_WEIGHTS)
    explanation = transparency.creator_explanation("creator-laoke")
    print("\n=== 创作者视角（节选）===")
    row = explanation["contents"][0]
    print("长期价值分：", row["long_term_value_score"])
    for c in row["signal_contributions"][:4]:
        print(f"  - {c['signal']} × {c['count']}｜{c['source']}｜计入长期价值：{c['counts_toward_long_term_value']}")
    # 申诉：互刷系误判（窗口已封账 → 只记录裁决、不篡改历史账）
    appeal = transparency.decide_appeal(
        "appeal-001", "cv-beiying-v1", "creator-laoke", "accepted",
        "核实非同控制账号，属正常收藏", "复核员",
        window_id="ex-recip:save", reclassify_to="organic")
    print("申诉结果：correction_allowed =", appeal.payload["correction_allowed"],
          "（封账后只记录，不追溯改账）")

    # 11) 外部报告：k 匿名、取整、无机密
    report = transparency.external_report(EXP, decision)
    print("\n=== 外部报告（节选）===")
    print(json.dumps(report["分组结果"], ensure_ascii=False, indent=2))
    print("被抑制小人群：", report["被抑制小人群"])

    # 12) 事后复算：为什么当初决定扩大
    result = recompute_decision(store, "dec-2026-09-28")
    print("\n=== 审计复算 ===")
    print("指纹校验：", result["manifest_hash_verified"],
          "｜重算 lift =", result["recomputed"]["lift"],
          "｜决策时事件数 =", result["events_at_decision"])
    print("事件日志：", store.path)


if __name__ == "__main__":
    main()
