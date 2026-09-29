import copy
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("observer", Path(__file__).resolve().parents[1] / "scripts/observe-soak.py")
OBSERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OBSERVER)


class ObserverTests(unittest.TestCase):
    def test_lifecycle_evidence_fails_closed(self):
        baseline = {
            "pods": [{"name": "weir", "uid": "pod-id", "phase": "Running", "containers": [{"ready": True, "restarts": 0}]}],
            "jobUID": "job-id", "jobStatus": {"active": 1},
        }
        identities = {"pod-id"}
        OBSERVER.invariant(baseline, identities, "job-id", 1)
        cases = []
        restarted = copy.deepcopy(baseline)
        restarted["pods"][0]["containers"][0]["restarts"] = 1
        cases.append(restarted)
        unready = copy.deepcopy(baseline)
        unready["pods"][0]["containers"][0]["ready"] = False
        cases.append(unready)
        replaced = copy.deepcopy(baseline)
        replaced["pods"][0]["uid"] = "replacement"
        cases.append(replaced)
        missing = copy.deepcopy(baseline)
        missing["pods"] = []
        cases.append(missing)
        failed = copy.deepcopy(baseline)
        failed["jobStatus"] = {"failed": 1}
        cases.append(failed)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                OBSERVER.invariant(case, identities, "job-id", 1)


if __name__ == "__main__":
    unittest.main()
