import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("release", Path(__file__).resolve().parents[1] / "scripts/check-release.py")
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


class ReleaseTests(unittest.TestCase):
    def test_only_authenticated_not_found_allows_publish(self):
        cases = (
            ((200, b""), (404, b""), (200, b'{"token":"test"}'), (404, b"")),
            ((403, b""),),
            ((200, b""), (403, b"")),
            ((200, b""), (200, b"")),
            ((200, b""), (404, b""), (403, b"")),
            ((200, b""), (404, b""), (200, b'{"token":"test"}'), (200, b"")),
            ((200, b""), (404, b""), (200, b'{"token":"test"}'), (503, b"")),
        )
        environment = {"GH_TOKEN": "test-token", "GITHUB_REPOSITORY": "batchstream/weir-charts", "GITHUB_ACTOR": "test"}
        for index, responses in enumerate(cases):
            with self.subTest(index=index), mock.patch.dict(os.environ, environment), mock.patch("sys.argv", ["check-release.py", "0.1.0"]), mock.patch.object(RELEASE, "request", side_effect=responses):
                if index == 0:
                    RELEASE.main()
                else:
                    with self.assertRaises(RuntimeError):
                        RELEASE.main()


if __name__ == "__main__":
    unittest.main()
