import unittest

from src.long_lab.events import EventStore
from src.long_lab.experiments import AssignmentBlocked, ExperimentService, audit_contamination
from src.long_lab.identity import deterministic_bucket, pseudonymize
from src.long_lab.timekeeping import Clock

from .lab_factory import START, make_world, open_experiment, users


class IdentityTest(unittest.TestCase):
    def test_pseudonym_is_stable_and_opaque(self) -> None:
        self.assertEqual(pseudonymize("张三"), pseudonymize("张三"))
        self.assertNotIn("张三", pseudonymize("张三"))
        self.assertNotEqual(pseudonymize("张三"), pseudonymize("李四"))

    def test_bucket_is_deterministic(self) -> None:
        b1 = deterministic_bucket("u1", "exp", "salt", 2)
        b2 = deterministic_bucket("u1", "exp", "salt", 2)
        self.assertEqual(b1, b2)
        self.assertIn(b1, (0, 1))


class ExperimentLifecycleTest(unittest.TestCase):
    def test_pause_blocks_assignment_and_resume_never_crosses_arms(self) -> None:
        w = make_world()
        open_experiment(w)
        us = users(40)
        first = {}
        for u in us:
            evt = w.experiments.assign("exp-classic-text", u)
            first[u] = evt.payload["arm"]

        # 规则变更触发暂停：暂停期间一律拒绝分桶
        w.experiments.pause("exp-classic-text", "rule_change")
        with self.assertRaises(AssignmentBlocked):
            w.experiments.assign("exp-classic-text", users(1, "late")[0])

        # 恢复禁止换盐
        with self.assertRaises(Exception):
            w.experiments.resume("exp-classic-text", salt="different-salt")

        w.advance(hours=3)
        w.experiments.resume("exp-classic-text")
        # 老用户恢复后确定性落回原组，且记录 after_resume
        for u in us:
            evt = w.experiments.assign("exp-classic-text", u)
            self.assertEqual(evt.payload["arm"], first[u], "恢复后出现跨组污染")

        late_user = users(1, "newcomer")[0]
        new_evt = w.experiments.assign("exp-classic-text", late_user)
        self.assertTrue(new_evt.payload["after_resume"])
        self.assertEqual(audit_contamination(w.store), [])

    def test_closed_experiment_rejects_assignment(self) -> None:
        w = make_world()
        open_experiment(w)
        u = users(1)[0]
        w.experiments.assign("exp-classic-text", u)
        w.experiments.close("exp-classic-text")
        with self.assertRaises(AssignmentBlocked):
            w.experiments.assign("exp-classic-text", users(1, "x")[0])

    def test_invalid_bucket_weights_rejected(self) -> None:
        w = make_world()
        with self.assertRaises(Exception):
            w.experiments.open_experiment(
                "bad", "bad", "s", {"control": [0], "treatment": [0]}, 1,
                start_at=w.clock.now().isoformat())

    def test_event_log_replays_versions_and_is_idempotent_shape(self) -> None:
        w = make_world()
        open_experiment(w)
        n_before = len(w.store.read_all())
        w.experiments.assign("exp-classic-text", users(1)[0])
        # 重新打开同一日志文件，版本号连续不断裂
        store2 = EventStore(w.store.path, clock=Clock(START))
        self.assertEqual(len(store2.read_all()), n_before + 1)
        state = ExperimentService(store2).state("exp-classic-text")
        self.assertEqual(state.status, "open")


if __name__ == "__main__":
    unittest.main()
