import unittest

from module_catalog import MODULE_DEFINITIONS, MODULE_KEYS, resolve_module_dependencies


class ModuleCatalogTests(unittest.TestCase):
    def test_only_implemented_modules_are_licensable(self):
        self.assertEqual(
            set(MODULE_KEYS),
            {"accounting", "cafe", "customers", "gamenet", "inventory", "reports", "settings"},
        )
        self.assertTrue(all(item["status"] == "available" for item in MODULE_DEFINITIONS.values()))

    def test_every_operational_module_requires_accounting(self):
        for key in ("cafe", "customers", "gamenet", "inventory", "reports"):
            with self.subTest(module=key):
                self.assertEqual(
                    resolve_module_dependencies([key]),
                    ["accounting", key],
                )

    def test_accounting_and_settings_remain_standalone_core_modules(self):
        self.assertEqual(resolve_module_dependencies(["accounting"]), ["accounting"])
        self.assertEqual(resolve_module_dependencies(["settings"]), ["settings"])

    def test_dependency_resolution_is_sorted_and_deduplicated(self):
        self.assertEqual(
            resolve_module_dependencies(["cafe", "accounting", "cafe"]),
            ["accounting", "cafe"],
        )

    def test_unknown_or_unimplemented_module_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ماژول ناشناخته"):
            resolve_module_dependencies(["hotel"])

    def test_registry_is_immutable(self):
        with self.assertRaises(TypeError):
            MODULE_DEFINITIONS["hotel"] = {"status": "available"}


if __name__ == "__main__":
    unittest.main()
