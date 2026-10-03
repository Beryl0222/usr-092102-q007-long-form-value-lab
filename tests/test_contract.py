import json
import unittest
from pathlib import Path

from src.long_lab.contracts import EVENT_AGGREGATE, PAYLOAD_CONTRACTS, validate_payload
from src.validator import validate_event

ROOT = Path(__file__).parents[1]


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_schema_and_registry_enums_are_in_sync(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        schema_events = set(schema["properties"]["event_type"]["enum"])
        registry_events = set(PAYLOAD_CONTRACTS)
        mapping_events = set(EVENT_AGGREGATE)
        self.assertEqual(schema_events, registry_events, "schema 与 payload 注册表事件枚举漂移")
        self.assertEqual(schema_events, mapping_events, "schema 与事件→聚合映射漂移")
        schema_aggs = set(schema["properties"]["aggregate_type"]["enum"])
        self.assertTrue(set(EVENT_AGGREGATE.values()) <= schema_aggs)
        # 原始五个事件必须保留（additive 演进）
        for legacy in ("SIGNAL_RECORDED", "EXPERIMENT_ASSIGNED", "RISK_ACTION_APPLIED",
                       "WINDOW_CLOSED", "POLICY_DECIDED"):
            self.assertIn(legacy, schema_events)
        for legacy in ("content_version", "signal_window", "experiment_assignment", "policy_decision"):
            self.assertIn(legacy, schema_aggs)

    def test_payload_validation_accepts_wellformed_and_rejects_bad_domain(self) -> None:
        good = {
            "objective_id": "o1", "objective_version": 1,
            "weights": {"save": 1.0}, "notes": "",
        }
        self.assertEqual(validate_payload("OBJECTIVE_VERSIONED", good), [])
        bad = dict(good)
        bad["objective_version"] = "v1"  # type: ignore[assignment]
        self.assertTrue(validate_payload("OBJECTIVE_VERSIONED", bad))
        bad2 = {**good, "weights": {"save": 1.0}}
        bad2_signal = {
            "window_id": "w", "exposure_id": "e", "content_version_id": "c",
            "user_pseudo": "u", "experiment_id": "x", "arm": "middle",  # 非法组
            "signal_kind": "click", "signal_ts": "2026-09-01T10:00:00+00:00",
            "provenance": "organic",
        }
        errs = validate_payload("SIGNAL_RECORDED", bad2_signal)
        self.assertTrue(any("arm" in e for e in errs))

    def test_provenance_and_signal_kinds_are_distinct_and_stable(self) -> None:
        from src.long_lab.contracts import PROVENANCE, SIGNAL_KINDS, WINDOW_DEFINITIONS
        self.assertEqual(set(SIGNAL_KINDS), set(WINDOW_DEFINITIONS))
        self.assertIn("organic", PROVENANCE)
        self.assertIn("controlled_reciprocal", PROVENANCE)
        self.assertIn("operational_placement", PROVENANCE)
        self.assertIn("fan_concentrated", PROVENANCE)


if __name__ == "__main__":
    unittest.main()
