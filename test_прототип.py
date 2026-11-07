import contextlib
import csv
import io
import json
import os
import runpy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


BASE_DIR = Path(__file__).resolve().parent
PROTOTYPE = BASE_DIR / "04_прототип.py"

PRODUCTS_FILE = BASE_DIR / "product.csv"
MODULE = runpy.run_path(str(PROTOTYPE), run_name="module_under_test")
PRODUCTS = MODULE["load_products"](str(PRODUCTS_FILE))


def make_application(**overrides):
    app = {
        "app_id": "app_test_0001",
        "client_id": "cli_test_0001",
        "product_id": "prod_standard",
        "age": "35",
        "income": "150000",
        "employment": "employed",
        "family": "married",
        "amount": "100000",
        "term": "12",
        "passport_hash": "0123456789abcdef",
        "smev_status": "MATCH",
        "bki_score": "750",
        "delinquency": "false",
    }
    app.update(overrides)
    return app


class ProductSelectionTests(unittest.TestCase):
    def test_product_file_contains_three_current_products(self):
        self.assertEqual(len(PRODUCTS), 3)
        self.assertEqual(
            {p["product_id"] for p in PRODUCTS},
            {"prod_standard", "prod_express", "prod_premium"},
        )

    def test_standard_product_is_selected(self):
        app = make_application()
        product = MODULE["select_product"](app, PRODUCTS)
        self.assertIsNotNone(product)
        self.assertEqual(product["product_id"], "prod_standard")
        self.assertEqual(float(product["rate"]), 0.18)
        self.assertEqual(float(product["min_income"]), 30000)

    def test_express_product_uses_its_own_minimum_income(self):
        app = make_application(
            age="25", income="20000", amount="50000", term="12",
            product_id="prod_express",
        )
        product = MODULE["select_product"](app, PRODUCTS)
        self.assertIsNotNone(product)
        self.assertEqual(product["product_id"], "prod_express")
        self.assertEqual(float(product["min_income"]), 20000)

    def test_premium_product_uses_its_own_minimum_income(self):
        app = make_application(
            age="35", income="150000", amount="600000", term="24",
            product_id="prod_premium",
        )
        product = MODULE["select_product"](app, PRODUCTS)
        self.assertIsNotNone(product)
        self.assertEqual(product["product_id"], "prod_premium")
        self.assertEqual(float(product["min_income"]), 150000)

    def test_income_below_premium_minimum_returns_low_income(self):
        app = make_application(
            age="35", income="149999", amount="600000", term="24",
            product_id="prod_premium",
        )
        self.assertIsNone(MODULE["select_product"](app, PRODUCTS))
        self.assertEqual(
            MODULE["get_product_rejection_reason"](app, PRODUCTS),
            "LOW_INCOME",
        )

    def test_income_equal_to_product_minimum_is_allowed(self):
        app = make_application(
            income="30000", amount="50000", term="12",
            product_id="prod_standard",
        )
        product = MODULE["select_product"](app, PRODUCTS)
        self.assertIsNotNone(product)
        self.assertEqual(product["product_id"], "prod_standard")

    def test_wrong_amount_range_is_not_misreported_as_low_income(self):
        app = make_application(
            income="200000", amount="100000", term="24",
            product_id="prod_premium",
        )
        self.assertIsNone(MODULE["select_product"](app, PRODUCTS))
        self.assertEqual(
            MODULE["get_product_rejection_reason"](app, PRODUCTS),
            "PRODUCT_NOT_AVAILABLE",
        )

    def test_without_requested_product_selects_lowest_rate_that_fits(self):
        app = make_application(
            income="200000", amount="600000", term="24", product_id="",
        )
        product = MODULE["select_product"](app, PRODUCTS)
        self.assertIsNotNone(product)
        self.assertEqual(product["product_id"], "prod_premium")
        self.assertEqual(float(product["rate"]), 0.15)


class ProcessingTests(unittest.TestCase):
    def run_in_temp_dir(self, app, source="csv"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        result = MODULE["process"](
            app,
            PRODUCTS,
            source=source,
            external_log=str(root / "external.log"),
            audit_log=str(root / "decision_audit.log"),
            run_id="test_run_001",
            index=1,
            clients_out=str(root / "Client.csv"),
            decisions_out=str(root / "Decision.csv"),
            scoring_out=str(root / "ScoringResult.csv"),
            audit_out=str(root / "AuditEvent.csv"),
        )
        return result, root

    def test_good_application_is_approved_with_selected_product(self):
        result, _ = self.run_in_temp_dir(make_application())
        self.assertEqual(result["decision"], "APPROVED")
        self.assertEqual(result["product_id"], "prod_standard")
        self.assertEqual(result["product_name"], "Стандартный кредит")
        self.assertEqual(float(result["rate"]), 0.18)
        self.assertEqual(float(result["min_income_required"]), 30000)

    def test_missing_income_requests_more_data(self):
        result, _ = self.run_in_temp_dir(make_application(income=""))
        self.assertEqual(
            (result["decision"], result["reason"]),
            ("NEED_MORE_DATA", "MISSING_INCOME"),
        )

    def test_income_below_product_minimum_causes_low_income_refusal(self):
        app = make_application(
            product_id="prod_premium", income="149999",
            amount="600000", term="24",
        )
        result, _ = self.run_in_temp_dir(app)
        self.assertEqual(
            (result["decision"], result["reason"]),
            ("REFUSED", "LOW_INCOME"),
        )
        # Даже при отказе по доходу в результате сохраняется определённый продукт.
        self.assertEqual(result["product_id"], "prod_premium")
        self.assertEqual(result["product_name"], "Премиальный кредит")

    def test_bki_refusal(self):
        result, _ = self.run_in_temp_dir(make_application(bki_score="450"))
        self.assertEqual(
            (result["decision"], result["reason"]),
            ("REFUSED", "BKI_DELINQUENCY"),
        )

    def test_high_dti_refusal_uses_selected_product_rate(self):
        app = make_application(amount="900000", income="30000", term="12")
        result, _ = self.run_in_temp_dir(app)
        self.assertEqual(result["decision"], "REFUSED")
        self.assertEqual(result["reason"], "HIGH_DTI")
        self.assertEqual(result["product_id"], "prod_standard")

    def test_four_entity_csv_files_are_written(self):
        result, root = self.run_in_temp_dir(make_application())
        expected = {
            "Client.csv": "client_id",
            "Decision.csv": "decision_id",
            "ScoringResult.csv": "scoring_id",
            "AuditEvent.csv": "event_id",
        }
        for filename, id_column in expected.items():
            path = root / filename
            self.assertTrue(path.exists(), filename)
            with path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 1, filename)
            self.assertTrue(rows[0][id_column], filename)

        with (root / "Decision.csv").open(encoding="utf-8-sig", newline="") as file:
            decision_row = next(csv.DictReader(file))
        self.assertEqual(decision_row["product_id"], result["product_id"])
        self.assertEqual(decision_row["product_name"], result["product_name"])
        self.assertEqual(decision_row["min_income_required"], "30000")

    def test_audit_does_not_contain_passport_hash(self):
        app = make_application()
        _, root = self.run_in_temp_dir(app)
        audit_text = (root / "decision_audit.log").read_text(encoding="utf-8")
        self.assertNotIn(app["passport_hash"], audit_text)

    def test_bki_emulator_is_deterministic(self):
        emulator = MODULE["bki_emulator"]
        self.assertEqual(
            emulator("0123456789abcdef"),
            emulator("0123456789abcdef"),
        )

    def test_smev_emulator_is_deterministic(self):
        emulator = MODULE["smev_emulator"]
        self.assertEqual(
            emulator("cli_test_0001"),
            emulator("cli_test_0001"),
        )

    def test_timeout_does_not_return_success(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = str(Path(directory) / "external.log")

            def slow_service():
                time.sleep(0.05)
                return {"status": "MATCH"}

            result = MODULE["call_external"](
                "SMEV", "request_test", "app_test_0001", "run_test_001",
                slow_service, log_path, timeout=0.005, attempts=2,
            )
            self.assertIsNone(result)
            rows = [
                json.loads(line)
                for line in Path(log_path).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["status"] == "TIMEOUT" for row in rows))


class ConsoleOutputTests(unittest.TestCase):
    def test_print_result_includes_selected_product_name_and_id(self):
        app = make_application()
        result = {
            "app_id": app["app_id"],
            "decision": "APPROVED",
            "reason": "",
            "score": 90,
            "dti": 0.05,
            "product_id": "prod_standard",
            "product_name": "Стандартный кредит",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE["print_application_result"](result, app)
        printed = output.getvalue()
        self.assertIn("Стандартный кредит", printed)
        self.assertIn("prod_standard", printed)
        self.assertIn("Выбранный продукт", printed)

    def test_print_result_explains_when_product_is_not_determined(self):
        app = make_application()
        result = {
            "app_id": app["app_id"],
            "decision": "NEED_MORE_DATA",
            "reason": "MISSING_INCOME",
            "score": "",
            "dti": "",
            "product_id": "",
            "product_name": "",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE["print_application_result"](result, app)
        self.assertIn("Выбранный продукт: не определён", output.getvalue())


class InputFileTests(unittest.TestCase):
    def test_main_reads_application_csv(self):
        # Проверяем фактический запуск через файл с новым именем application.csv.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application_file = root / "application.csv"
            with application_file.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=list(make_application().keys()),
                )
                writer.writeheader()
                writer.writerow(make_application())

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    with patch(
                        "sys.argv",
                        [
                            "04_прототип.py",
                            "application.csv",
                            "--products", str(PRODUCTS_FILE),
                            "--external-source", "csv",
                        ],
                    ):
                        MODULE["main"]()
            finally:
                os.chdir(old_cwd)

            self.assertIn("Обработка файла: application.csv", output.getvalue())
            self.assertIn("Выбранный продукт: Стандартный кредит", output.getvalue())
            self.assertTrue((root / "decisions.csv").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
