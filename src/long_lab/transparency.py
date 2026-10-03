"""透明化层：

- 创作者视角：看到自己内容的“可理解信号贡献”，每类信号用自然语言解释其含义，
  互刷/运营/粉丝回访单独成项并说明不计入长期价值；不含任何其他用户信息。
- 申诉：创作者可对被再分类信号或风险处置申诉；仅未封账窗口允许直接纠正，
  已封账窗口只能记录裁决并影响后续版本，不篡改历史账。
- 外部报告：k-匿名抑制小人群、整数取整、比例四舍五入到百分点，剔除用户伪 ID
  与个体明细；模型侧只输出权重维度名，不输出内部特征/阈值实现细节。
"""

from __future__ import annotations

from collections import defaultdict

from .catalog import ContentCatalog
from .events import Event, EventStore
from .metrics import replay

SIGNAL_HUMAN_NAMES = {
    "click": "普通点击",
    "save": "收藏",
    "save_open": "收藏后再次打开",
    "cross_day_complete": "跨日看完",
    "effective_discussion": "有效讨论",
}
PROVENANCE_HUMAN_NAMES = {
    "organic": "自然反馈",
    "controlled_reciprocal": "关联账号互刷（不计入长期价值）",
    "operational_placement": "运营活动带来（单独统计，不计入长期价值）",
    "fan_concentrated": "粉丝集中回访（单独统计，不计入长期价值）",
}

REPORT_K = 10          # 组合人群少于 10 名去重用户则抑制
RATE_ROUND_DP = 2      # 比率保留两位小数（百分点粒度）
COUNT_ROUND_BASE = 100  # 曝光量按百位取整


class TransparencyService:
    def __init__(self, store: EventStore, catalog: ContentCatalog, weights: dict[str, float]) -> None:
        self.store = store
        self.catalog = catalog
        self.weights = weights

    # ---- 创作者可理解的信号贡献 ----
    def creator_explanation(self, creator_id: str) -> dict:
        own = {
            vid: cv for vid, cv in self.catalog.versions.items()
            if cv.creator_id == creator_id
        }
        if not own:
            return {"creator_id": creator_id, "contents": [],
                    "note": "未查询到该创作者名下内容版本"}
        # 汇总该创作者所有版本上的信号（窗口读模型已重放再分类，展示的是生效来源）
        from .signals import WindowReader
        windows = WindowReader(self.store).all()
        bucket: dict[str, dict] = defaultdict(lambda: defaultdict(int))
        appeals: dict[str, list[str]] = defaultdict(list)
        for win in windows.values():
            if win.signal and win.content_version_id in own:
                bucket[win.content_version_id][(win.kind, win.provenance)] += 1
        for evt in self.store.read_all():
            if evt.event_type == "APPEAL_DECIDED" and evt.payload["creator_id"] == creator_id:
                appeals[evt.payload["content_version_id"]].append(
                    f"申诉 {evt.payload['status']}：{evt.payload['reason']}"
                )

        contents = []
        for vid, cv in own.items():
            organic_value = sum(
                n * self.weights.get(kind, 0.0)
                for (kind, prov), n in bucket[vid].items() if prov == "organic"
            )
            contributions = []
            for (kind, prov), n in sorted(bucket[vid].items(), key=lambda x: -x[1]):
                contributions.append({
                    "signal": SIGNAL_HUMAN_NAMES.get(kind, kind),
                    "source": PROVENANCE_HUMAN_NAMES.get(prov, prov),
                    "count": n,
                    "counts_toward_long_term_value": prov == "organic",
                    "explanation": _explain(kind, prov),
                })
            risk = self.catalog.risk.get(vid)
            contents.append({
                "content_version_id": vid,
                "title": cv.title,
                "superseded": cv.superseded_by is not None,
                "long_term_value_score": round(organic_value, 4),
                "signal_contributions": contributions,
                "risk_status": risk.action if risk else "none",
                "appeals": appeals.get(vid, []),
            })
        return {"creator_id": creator_id, "contents": contents}

    # ---- 申诉 ----
    def decide_appeal(
        self, appeal_id: str, content_version_id: str, creator_id: str, status: str,
        reason: str, reviewer: str, *, signal_event_id: str = "",
        reclassify_to: str = "", window_id: str = "",
    ) -> Event:
        from .signals import SignalPipeline, WindowReader
        correction_allowed = False
        if status == "accepted" and window_id and reclassify_to:
            win = WindowReader(self.store).get(window_id)
            if win is None:
                raise ValueError(f"窗口不存在：{window_id}")
            if win.finalized:
                # 已封账：历史账不得篡改，裁决仅记录在案
                correction_allowed = False
                reason = f"{reason}（窗口已封账，不追溯改账，裁决仅记录在案）"
            else:
                SignalPipeline(self.store, self.catalog).reclassify(window_id, reclassify_to, reason)
                correction_allowed = True
        payload: dict = {
            "appeal_id": appeal_id,
            "content_version_id": content_version_id,
            "creator_id": creator_id,
            "status": status,
            "correction_allowed": correction_allowed,
            "reason": reason,
            "reviewer": reviewer,
        }
        if signal_event_id:
            payload["signal_event_id"] = signal_event_id
        return self.store.append("APPEAL_DECIDED", appeal_id, payload,
                                 summary=f"申诉裁决 {status}：{reason}")

    # ---- 隐私安全的外部报告 ----
    def external_report(self, experiment_id: str, decision_event: Event) -> dict:
        metrics = replay(self.store, self.catalog)[experiment_id]
        rows = []
        for arm in ("control", "treatment"):
            a = metrics.arms[arm]
            rate = round(
                sum(n for (k, prov), n in a.signals.items() if prov == "organic")
                / a.exposures, RATE_ROUND_DP,
            ) if a.exposures else 0.0
            rows.append({
                "arm": "对照组" if arm == "control" else "实验组",
                "曝光量(按百位取整)": _round_count(a.exposures),
                "每曝光长期价值反馈率": rate,
                "运营投放占比": round(a.operational_exposures / a.exposures, RATE_ROUND_DP)
                    if a.exposures else 0.0,
            })

        fairness = {}
        suppressed: list[str] = []
        for group_name, stype, seg_defs in (
            ("使用时长", "usage_time", (("low", "低使用时长"), ("medium", "中使用时长"), ("high", "高使用时长"))),
            ("主题", "topic", (("niche", "小众主题"), ("mainstream", "主流主题"))),
            ("作者资历", "author_tenure", (("new", "新作者"), ("established", "成熟作者"))),
        ):
            fairness[group_name] = {}
            for sval, sname in seg_defs:
                # k-匿名：该组合维度下任一组去重用户不足 K，则整行抑制
                too_small = any(
                    _users_matching(metrics.arms[arm], stype, sval) < REPORT_K
                    for arm in ("control", "treatment")
                )
                if too_small:
                    suppressed.append(f"{group_name}:{sname}")
                    continue
                vals = {}
                for arm, arm_name in (("control", "对照组"), ("treatment", "实验组")):
                    expo = metrics.arms[arm].segment_exposures.get(stype, {}).get(sval, 0)
                    org = metrics.arms[arm].segment_organic.get(stype, {}).get(sval, 0)
                    vals[arm_name] = {
                        "曝光占比": round(expo / metrics.arms[arm].exposures, RATE_ROUND_DP)
                            if metrics.arms[arm].exposures else 0.0,
                        "长期价值反馈率": round(org / expo, RATE_ROUND_DP) if expo else 0.0,
                    }
                fairness[group_name][sname] = vals

        snap = decision_event.payload["metrics_snapshot"]
        return {
            "报告": "长内容价值实验外部摘要",
            "实验": experiment_id,
            "决策": {"动作": _decision_cn(decision_event.payload["action"]),
                     "说明": decision_event.payload["rationale"]},
            "分组结果": rows,
            "机会公平(已做k匿名抑制)": fairness,
            "被抑制小人群": sorted(set(suppressed)),
            "口径与隐私说明": [
                "仅含聚合指标，不含任何用户标识或个体浏览轨迹；",
                f"组合人群去重人数 < {REPORT_K} 的分群已整行抑制；",
                "曝光量按百位取整、比率保留两位小数；",
                "不披露模型内部特征、权重数值与风控阈值实现细节；",
                "运营投放、互刷与粉丝集中回访均单独统计，不计入长期价值反馈率。",
            ],
            "复算入口": {
                "manifest_hash": decision_event.payload["manifest_hash"],
                "objective_version": decision_event.payload["objective_version"],
                "说明": "审查人员可凭此指纹在审计日志上复算，外部无法据此还原个体数据。",
            },
            "价值提升": snap.get("lift"),
        }


def _round_count(n: int) -> int:
    return int(round(n / COUNT_ROUND_BASE) * COUNT_ROUND_BASE)


def _users_matching(arm_metrics, stype: str, sval: str) -> int:
    idx = {"usage_time": 0, "topic": 1, "author_tenure": 2}[stype]
    users: set[str] = set()
    for combo, us in arm_metrics.combo_users.items():
        if combo[idx] == sval:
            users |= us
    return len(users)


def _decision_cn(action: str) -> str:
    return {"hold": "维持观察", "scale": "扩大流量", "rollback": "回滚"}[action]


def _explain(kind: str, provenance: str) -> str:
    base = {
        "click": "短点击只作弱参考，容易被标题党抬高。",
        "save": "收藏表明你预期日后还会用到它。",
        "save_open": "收藏后真的回来打开，是延迟满足的直接证据。",
        "cross_day_complete": "隔了一天还愿意看完，是长内容留存价值的最强信号之一。",
        "effective_discussion": "有质量、未被删除的讨论，代表内容引发了认真交流。",
    }[kind]
    if provenance != "organic":
        base += " 该反馈来自非自然渠道，已从长期价值评分中剔除并单独展示。"
    return base
