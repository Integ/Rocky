"""Check a usable configuration against the supplied fictional specification."""

import re
import tomllib
import unittest
from pathlib import Path
from urllib.parse import urlsplit


class GuideConfigurationTest(unittest.TestCase):
    def test_contains_a_complete_parseable_configuration(self):
        text = Path("guide.md").read_text(encoding="utf-8")
        examples = re.findall(r"(?ms)^```toml\s*\n(.*?)^```\s*$", text)
        complete = []
        for example in examples:
            try:
                value = tomllib.loads(example)
                address = value["api"]["endpoint"]
                if not isinstance(address, str):
                    raise ValueError("The endpoint must be a URL string")
                endpoint = urlsplit(address)
                positive = [
                    value["api"]["timeout_seconds"],
                    value["deploy"]["health_timeout_seconds"],
                ]
                valid = (
                    endpoint.scheme == "https"
                    and endpoint.hostname
                    and endpoint.port == 8443
                    and all(type(number) is int and number > 0 for number in positive)
                    and value["defaults"]["env"] in {"staging", "production"}
                    and value["defaults"]["output"] in {"table", "json"}
                    and type(value["deploy"]["wait_for_health"]) is bool
                    and value["log"]["level"] in {"debug", "info", "warn", "error"}
                )
                complete.append(bool(valid))
            except (KeyError, TypeError, ValueError):
                complete.append(False)
        self.assertTrue(
            any(complete), "No complete, valid TOML example matching atlas-reference.md"
        )


if __name__ == "__main__":
    unittest.main()
