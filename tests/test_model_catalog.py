"""Reject malformed native catalog metadata before saving provider settings."""
from copy import deepcopy
import unittest

from orchestra_kit.model_catalog import validate_catalog


VALID_CATALOG = {"models": [{
    "slug": "example-model", "display_name": "Example",
    "supported_reasoning_levels": [{"effort": "low", "description": "Fast"}],
    "shell_type": "shell_command", "visibility": "list",
    "supported_in_api": True, "priority": 1, "support_verbosity": False,
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "experimental_supported_tools": [], "base_instructions": "Example instructions",
}]}


class CatalogMetadataTests(unittest.TestCase):
    def test_rejects_invalid_native_metadata_types_and_values(self):
        invalid = [
            ("shell_type", {}), ("shell_type", "unknown"),
            ("visibility", 123), ("visibility", "unknown"),
            ("supported_in_api", "yes"), ("supported_in_api", 1),
            ("priority", "first"), ("priority", True), ("priority", 1.5),
            ("priority", 2**31), ("priority", -(2**31)-1),
            ("support_verbosity", "sometimes"),
            ("truncation_policy", []),
            ("truncation_policy", {"mode": "unknown", "limit": 1}),
            ("truncation_policy", {"mode": "tokens", "limit": 2**63}),
            ("truncation_policy", {"mode": "tokens", "limit": -(2**63)-1}),
            ("truncation_policy", {"mode": "tokens", "limit": True}),
            ("truncation_policy", {"mode": "tokens", "limit": "large"}),
            ("experimental_supported_tools", {}),
            ("experimental_supported_tools", [17]),
            ("supported_reasoning_levels", None),
            ("supported_reasoning_levels", [{"effort": "low"}]),
        ]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                catalog = deepcopy(VALID_CATALOG)
                catalog["models"][0][field] = value
                with self.assertRaisesRegex(ValueError, field):
                    validate_catalog(catalog)

    def test_accepts_native_variants_without_rewriting_metadata(self):
        for shell in ("default", "local", "shell_command", "unified_exec", "disabled"):
            catalog = deepcopy(VALID_CATALOG)
            catalog["models"][0].update(shell_type=shell, priority=-1,
                visibility="hide", truncation_policy={"mode": "bytes", "limit": 0},
                experimental_supported_tools=["clock"], extra={"vendor": "example"})
            self.assertEqual(validate_catalog(catalog), catalog)
