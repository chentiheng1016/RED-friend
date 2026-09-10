from __future__ import annotations

import json
import multiprocessing as mp
import os
import tempfile
import time
import unittest
from unittest import mock


def _concurrent_register_worker(path: str, email: str, color: str) -> None:
    """Subprocess worker for the lost-update test. Run register_employee
    against a shared registry file; if locked_json is doing its job all
    writes survive, otherwise some get clobbered."""
    os.environ["RED_EMPLOYEE_REGISTRY_FILE"] = path
    os.environ.pop("RED_EMPLOYEE_REGISTRY_BACKEND", None)
    from agent_core.web_server import employee_registry as er
    # Brief sleep widens the RMW window so the race is observable on fast
    # machines — without it, registrations may serialize accidentally.
    time.sleep(0.01)
    er.register_employee(email, email.split("@")[0], color)


class _FakeDoc:
    def __init__(self, collection, doc_id):
        self._collection = collection
        self.id = doc_id

    @property
    def exists(self):
        return self.id in self._collection.data

    def get(self):
        return self

    def set(self, payload):
        self._collection.data[self.id] = dict(payload)

    def to_dict(self):
        return self._collection.data.get(self.id)


class _FakeCollection:
    def __init__(self):
        self.data = {}

    def document(self, doc_id):
        return _FakeDoc(self, doc_id)

    def stream(self):
        return [_FakeDoc(self, doc_id) for doc_id in sorted(self.data)]


class EmployeeRegistryTests(unittest.TestCase):
    def test_registry_file_can_be_overridden_for_cloud_storage_mounts(self):
        from agent_core.web_server import employee_registry

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            path = os.path.join(tmpdir, "data", "employee_registry.json")
            with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": path}):
                employee_registry.register_employee(
                    "owner@company.example",
                    "Owner",
                    "red",
                    line_user_id="U123",
                )

                self.assertTrue(os.path.exists(path))
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.assertEqual(
                    data["owner@company.example"],
                    {"name": "Owner", "color": "red", "line_user_id": "U123"},
                )
                self.assertEqual(
                    employee_registry.get_employee("owner@company.example"),
                    {"name": "Owner", "color": "red", "line_user_id": "U123"},
                )
                self.assertEqual(
                    employee_registry.get_employee_by_line_user_id("U123")["email"],
                    "owner@company.example",
                )

    def test_firestore_backend_registers_and_reads_without_full_file_rewrite(self):
        from agent_core.web_server import employee_registry

        collection = _FakeCollection()
        with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_BACKEND": "firestore"}), \
             mock.patch.object(employee_registry, "_firestore_collection", return_value=collection):
            employee_registry.register_employee(
                "owner@company.example",
                "Owner",
                "red",
                line_user_id="U123",
            )

            self.assertEqual(
                collection.data["owner@company.example"],
                {"email": "owner@company.example", "name": "Owner", "color": "red", "line_user_id": "U123"},
            )
            self.assertEqual(
                employee_registry.get_employee("owner@company.example"),
                {"name": "Owner", "color": "red", "line_user_id": "U123"},
            )
            self.assertEqual(
                employee_registry.list_employees(),
                [{"email": "owner@company.example", "name": "Owner", "color": "red", "line_user_id": "U123"}],
            )
            self.assertEqual(
                employee_registry.get_employee_by_line_user_id("U123")["email"],
                "owner@company.example",
            )


    def test_register_preserves_existing_line_user_id_when_form_omits_it(self):
        from agent_core.web_server import employee_registry

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            path = os.path.join(tmpdir, "data", "employee_registry.json")
            with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": path}):
                employee_registry.register_employee(
                    "alice@example.com",
                    "Alice",
                    "green",
                    line_user_id="U_alice",
                    telegram_user_id="12345",
                )
                # Admin re-adds to fix the display name; form leaves the
                # optional userId inputs blank. Existing bindings must survive.
                employee_registry.register_employee(
                    "alice@example.com",
                    "Alice Wong",
                    "green",
                )

                self.assertEqual(
                    employee_registry.get_employee("alice@example.com"),
                    {
                        "name": "Alice Wong",
                        "color": "green",
                        "line_user_id": "U_alice",
                        "telegram_user_id": "12345",
                    },
                )
                self.assertEqual(
                    employee_registry.get_employee_by_telegram_user_id("12345")["email"],
                    "alice@example.com",
                )

    def test_set_employee_signature_persists_and_clears(self):
        from agent_core.web_server import employee_registry

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            path = os.path.join(tmpdir, "employee_registry.json")
            with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": path}):
                employee_registry.register_employee("gm@company.example", "gm", "red")
                employee_registry.set_employee_signature(
                    "gm@company.example", "Best regards,\n\nUserS Chen  陳小明\nSales"
                )
                rec = employee_registry.get_employee("gm@company.example")
                self.assertEqual(rec["signature"], "Best regards,\n\nUserS Chen  陳小明\nSales")
                self.assertEqual(rec["color"], "red")  # 其餘欄位保留
                # 清掉（空字串）→ signature 欄移除
                employee_registry.set_employee_signature("gm@company.example", "  ")
                self.assertNotIn("signature", employee_registry.get_employee("gm@company.example"))

    def test_register_preserves_existing_signature(self):
        from agent_core.web_server import employee_registry

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            path = os.path.join(tmpdir, "employee_registry.json")
            with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": path}):
                employee_registry.register_employee("gm@company.example", "gm", "red")
                employee_registry.set_employee_signature("gm@company.example", "MY SIG")
                # admin 重新 register（沒帶 signature 參數）不該清掉自助設定的簽名
                employee_registry.register_employee("gm@company.example", "UserS Chen", "red")
                self.assertEqual(
                    employee_registry.get_employee("gm@company.example")["signature"], "MY SIG"
                )

    def test_set_signature_unknown_employee_raises(self):
        from agent_core.web_server import employee_registry

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            path = os.path.join(tmpdir, "employee_registry.json")
            with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": path}):
                with self.assertRaises(ValueError):
                    employee_registry.set_employee_signature("nobody@x.com", "sig")

    def test_register_preserves_existing_line_user_id_on_firestore(self):
        from agent_core.web_server import employee_registry

        collection = _FakeCollection()
        with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_BACKEND": "firestore"}), \
             mock.patch.object(employee_registry, "_firestore_collection", return_value=collection):
            employee_registry.register_employee(
                "alice@example.com",
                "Alice",
                "green",
                line_user_id="U_alice",
            )
            employee_registry.register_employee(
                "alice@example.com",
                "Alice Wong",
                "green",
            )

            self.assertEqual(
                collection.data["alice@example.com"],
                {
                    "email": "alice@example.com",
                    "name": "Alice Wong",
                    "color": "green",
                    "line_user_id": "U_alice",
                },
            )

    def test_register_rejects_invalid_telegram_user_id(self):
        from agent_core.web_server import employee_registry

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            path = os.path.join(tmpdir, "employee_registry.json")
            with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": path}):
                with self.assertRaises(ValueError) as cm:
                    employee_registry.register_employee(
                        "alice@example.com",
                        "Alice",
                        "green",
                        telegram_user_id="not-a-number",
                    )

        self.assertIn("telegram_user_id 格式錯誤", str(cm.exception))

    def test_register_rejects_duplicate_telegram_user_id(self):
        from agent_core.web_server import employee_registry

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            path = os.path.join(tmpdir, "employee_registry.json")
            with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": path}):
                employee_registry.register_employee(
                    "alice@example.com",
                    "Alice",
                    "green",
                    telegram_user_id="-10012345",
                )
                with self.assertRaises(ValueError) as cm:
                    employee_registry.register_employee(
                        "bob@example.com",
                        "Bob",
                        "orange",
                        telegram_user_id="-10012345",
                    )

        self.assertIn("已被 alice@example.com 使用", str(cm.exception))


class EmployeeRegistryConcurrencyTests(unittest.TestCase):
    """Cross-process race coverage — file backend only (Firestore has its own)."""

    def test_concurrent_registrations_all_persisted(self):
        """Eight subprocesses each register a different employee. With the
        locked_json refactor, all eight survive; without it, parallel writers
        clobber each other and the final count is < 8."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "employee_registry.json")
            colors = ["green", "orange", "yellow", "blue",
                      "purple", "indigo", "white", "gray"]
            ctx = mp.get_context("spawn")
            procs = []
            for i, color in enumerate(colors):
                p = ctx.Process(
                    target=_concurrent_register_worker,
                    args=(path, f"e{i}@example.com", color),
                )
                procs.append(p)
                p.start()
            for p in procs:
                p.join(timeout=15)
                self.assertEqual(p.exitcode, 0, f"worker crashed: {p}")
            with open(path) as f:
                registry = json.load(f)
            self.assertEqual(
                len(registry), len(colors),
                f"lost update! expected {len(colors)} employees, got "
                f"{len(registry)}: {sorted(registry)}"
            )


if __name__ == "__main__":
    unittest.main()
