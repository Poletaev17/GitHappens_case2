#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import math
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from collections import Counter

OUT_FIELDS = ["app_id", "product_id", "product_name", "rate", "min_income_required", "decision", "reason", "score", "dti"]
CLIENT_FIELDS = ["client_id", "age", "income", "employment", "family", "passport_hash"]
DECISION_FIELDS = ["decision_id", "app_id", "decision", "reason", "amount_approved", "term_approved", "product_id", "product_name", "rate", "min_income_required", "created_at"]
SCORING_FIELDS = ["scoring_id", "app_id", "income_points", "employment_points", "age_points", "family_points", "bki_points", "score", "dti", "created_at"]
AUDIT_FIELDS = ["event_id", "run_id", "app_id", "action", "actor", "at", "decision", "reason", "score", "dti", "product_id", "rate", "min_income_required", "components", "external_source", "metrics_calculated"]
RATE = 0.18
EMPLOYMENT = {"employed", "self-employed", "retired", "student", "unemployed"}
FAMILY = {"married", "children", "single"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def md5_mod_10(value):
    return int(hashlib.md5(str(value).encode("utf-8")).hexdigest(), 16) % 10


def bki_emulator(passport_hash):
    h = md5_mod_10(passport_hash)
    if h <= 5:
        score, delinquency = 600 + 20 * h, False
    elif h <= 8:
        score, delinquency = 450 + 50 * (h - 6), False
    else:
        score, delinquency = 400, True
    return {"score": score, "delinquency": delinquency, "requests_90d": 1}


def smev_emulator(client_id):
    return {"status": "MISMATCH" if md5_mod_10(client_id) == 0 else "MATCH"}


def load_products(filename="products.csv"):
    with open(filename, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        products = list(reader)

    required = {
        "product_id", "name", "min_amount", "max_amount",
        "min_term", "max_term", "rate", "min_age", "max_age", "min_income"
    }
    if not products or not required.issubset(products[0].keys()):
        raise ValueError("В products.csv отсутствуют нужные столбцы")

    return products


def select_product(app, products):
    """
    Подбирает продукт по его собственным ограничениям из products.csv:
    возраст, минимальный доход, сумма и срок.
    Если клиент не указал product_id и подходят несколько продуктов,
    выбирается продукт с самой низкой ставкой.
    """
    age = int(app["age"])
    income = float(app["income"])
    amount = float(app["amount"])
    term = int(app["term"])
    requested_id = str(app.get("product_id") or "").strip()

    suitable = []
    for product in products:
        if requested_id and product["product_id"] != requested_id:
            continue

        matches_other_rules = (
            int(product["min_age"]) <= age <= int(product["max_age"])
            and float(product["min_amount"]) <= amount <= float(product["max_amount"])
            and int(product["min_term"]) <= term <= int(product["max_term"])
        )
        income_is_enough = income >= float(product["min_income"])

        if matches_other_rules and income_is_enough:
            suitable.append(product)

    if not suitable:
        return None

    return min(suitable, key=lambda p: float(p["rate"]))


def get_product_rejection_reason(app, products):
    """
    Если продукт не подобрался только из-за дохода, возвращает
    PRODUCT_MIN_INCOME. Порог берётся именно из строки продукта в products.csv.
    """
    age = int(app["age"])
    income = float(app["income"])
    amount = float(app["amount"])
    term = int(app["term"])
    requested_id = str(app.get("product_id") or "").strip()

    for product in products:
        if requested_id and product["product_id"] != requested_id:
            continue

        matches_other_rules = (
            int(product["min_age"]) <= age <= int(product["max_age"])
            and float(product["min_amount"]) <= amount <= float(product["max_amount"])
            and int(product["min_term"]) <= term <= int(product["max_term"])
        )
        if matches_other_rules and income < float(product["min_income"]):
            return "PRODUCT_MIN_INCOME"

    return "PRODUCT_NOT_AVAILABLE"
def validate(app):
    income_raw = str(app.get("income") or "").strip()
    if not income_raw:
        return "NEED_MORE_DATA", "MISSING_INCOME"

    age_raw = str(app.get("age") or "").strip()
    if not age_raw:
        return "NEED_MORE_DATA", "MISSING_AGE"
    try:
        age = int(age_raw)
    except (TypeError, ValueError):
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if not 18 <= age <= 70:
        return "REFUSED", "AGE_OUT_OF_RANGE"

    employment = str(app.get("employment") or "").strip()
    if not employment:
        return "NEED_MORE_DATA", "MISSING_EMPLOYMENT"
    if employment == "unemployed":
        return "REFUSED", "INVALID_EMPLOYMENT"
    if employment not in EMPLOYMENT:
        return "NEED_MORE_DATA", "VALIDATION_ERROR"

    try:
        income = float(income_raw)
        amount = float(app.get("amount", ""))
        term = int(app.get("term", ""))
    except (TypeError, ValueError):
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if not all(math.isfinite(x) for x in (income, amount)) or income <= 0:
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if amount < 10000 or amount > 5_000_000 or not 3 <= term <= 60:
        return "NEED_MORE_DATA", "VALIDATION_ERROR"

    family = str(app.get("family") or "").strip()
    if family not in FAMILY:
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if not str(app.get("app_id") or "").strip() or not str(app.get("client_id") or "").strip():
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    passport_hash = str(app.get("passport_hash") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{16}", passport_hash):
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    return None


def score(app, bki_score, delinquency):
    income, age = float(app["income"]), int(app["age"])
    s_income = 30 if income >= 150000 else 20 if income >= 80000 else 10 if income >= 40000 else 0
    s_emp = {"employed": 25, "self-employed": 20, "retired": 10, "student": 5, "unemployed": 0}[app["employment"]]
    s_age = 20 if 25 <= age <= 50 else 10 if age <= 24 or age <= 60 else 5
    s_family = 15 if app["family"] in ("married", "children") else 10
    s_bki = -20 if bki_score < 500 or delinquency else 10 if bki_score >= 700 else 5 if bki_score >= 600 else 0
    parts = {"income": s_income, "employment": s_emp, "age": s_age, "family": s_family, "bki": s_bki}
    return max(0, sum(parts.values())), parts


def compute_dti(amount, term, income, rate=RATE):
    if income <= 0 or term <= 0:
        return None
    monthly_rate = rate / 12
    payment = amount / term if monthly_rate == 0 else (
        amount * monthly_rate / (1 - (1 + monthly_rate) ** (-term))
    )
    return round(payment / income, 2)


def decide(app, score_total, dti, bki_score, delinquency, smev):
    age, income = int(app["age"]), float(app["income"])
    if age < 18 or age > 70: return "REFUSED", "AGE_OUT_OF_RANGE"
    if app["employment"] == "unemployed": return "REFUSED", "INVALID_EMPLOYMENT"
    # Минимальный доход определяется индивидуально для продукта в products.csv.
    if dti is not None and dti > 0.5: return "REFUSED", "HIGH_DTI"
    if bki_score < 500 or delinquency: return "REFUSED", "BKI_DELINQUENCY"
    if float(app["amount"]) > 10 * income: return "REFUSED", "AMOUNT_LIMIT"
    if smev == "MISMATCH": return "REFUSED", "SMEV_MISMATCH"
    if score_total < 60: return "REFUSED", "LOW_SCORE"
    return "APPROVED", ""


def append_jsonl(path, event):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")



def append_csv(path, fields, row):
    import os
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def save_client(path, client):
    import os
    client_id = str(client.get("client_id") or "")
    if not client_id:
        return
    rows = []
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    rows = [row for row in rows if row.get("client_id") != client_id]
    rows.append({field: client.get(field, "") for field in CLIENT_FIELDS})
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CLIENT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def call_external(service, request_id, app_id, run_id, func, log_path, timeout=3, attempts=2):
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(func)
        error_type = ""
        try:
            result, status = future.result(timeout=timeout), "SUCCESS"
        except FutureTimeout:
            result, status = None, "TIMEOUT"
            error_type = "TimeoutError"
            future.cancel()
        except Exception as exc:
            result, status = None, "ERROR"
            error_type = type(exc).__name__
        else:
            error_type = ""
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        append_jsonl(log_path, {
            "timestamp": now_iso(), "run_id": run_id, "app_id": app_id,
            "service": service, "request_id": request_id, "attempt": attempt,
            "status": status, "latency_ms": round((time.monotonic() - started) * 1000),
            "result": result if status == "SUCCESS" else {},
            "error_type": error_type
        })
        if status == "SUCCESS": return result
        if status == "ERROR": break
        if attempt < attempts: time.sleep(0.5)
    return None


def make_result(app_id, decision, reason, total="", dti="", product=None):
    product = product or {}
    return {
        "app_id": app_id,
        "product_id": product.get("product_id", ""),
        "product_name": product.get("name", ""),
        "rate": product.get("rate", ""),
        "min_income_required": product.get("min_income", ""),
        "decision": decision,
        "reason": reason,
        "score": total,
        "dti": dti
    }


def process(app, products, source="emulator", external_log="external.log",
            audit_log="decision_audit.log", run_id=None, index=1,
            clients_out="Client.csv", decisions_out="Decision.csv",
            scoring_out="ScoringResult.csv", audit_out="AuditEvent.csv"):
    run_id = run_id or uuid.uuid4().hex
    app_id = str(app.get("app_id") or "UNKNOWN")
    total = dti = ""
    parts = {}
    product = None

    early = validate(app)
    if early:
        result = make_result(app_id, *early)
    else:
        product = select_product(app, products)

        if product is None:
            rejection_reason = get_product_rejection_reason(app, products)
            result = make_result(app_id, "REFUSED", rejection_reason)
        else:
            if source == "csv":
                smev_call = lambda: {"status": app.get("smev_status") or "NOT_CALLED"}

                def bki_call():
                    if app.get("bki_score", "") == "":
                        raise ValueError("bki_score отсутствует в CSV")
                    val = int(app["bki_score"])
                    return {"score": val, "delinquency": val < 500, "requests_90d": 1}
            else:
                smev_call = lambda: smev_emulator(app["client_id"])
                bki_call = lambda: bki_emulator(app["passport_hash"])

            smev = call_external(
                "SMEV", f"{run_id[:8]}-{index:04d}-SMEV",
                app_id, run_id, smev_call, external_log
            )

            if smev is None:
                result = make_result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT", product=product)
            elif smev["status"] == "MISMATCH":
                result = make_result(app_id, "REFUSED", "SMEV_MISMATCH", product=product)
            elif smev["status"] != "MATCH":
                result = make_result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT", product=product)
            else:
                bki = call_external(
                    "BKI", f"{run_id[:8]}-{index:04d}-BKI",
                    app_id, run_id, bki_call, external_log
                )

                if bki is None:
                    result = make_result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT", product=product)
                else:
                    total, parts = score(app, bki["score"], bki["delinquency"])
                    dti = compute_dti(
                        float(app["amount"]), int(app["term"]),
                        float(app["income"]), float(product["rate"])
                    )
                    decision, reason = decide(
                        app, total, dti, bki["score"],
                        bki["delinquency"], smev["status"]
                    )
                    result = make_result(
                        app_id, decision, reason, total, dti, product
                    )

    save_client(clients_out, {
        "client_id": app.get("client_id", ""),
        "age": app.get("age", ""),
        "income": app.get("income", ""),
        "employment": app.get("employment", ""),
        "family": app.get("family", ""),
        "passport_hash": app.get("passport_hash", "")
    })

    created_at = now_iso()
    append_csv(decisions_out, DECISION_FIELDS, {
        "decision_id": "dec_" + uuid.uuid4().hex[:12],
        "app_id": app_id,
        "decision": result["decision"],
        "reason": result["reason"],
        "amount_approved": app.get("amount", "") if result["decision"] == "APPROVED" else "",
        "term_approved": app.get("term", "") if result["decision"] == "APPROVED" else "",
        "product_id": result["product_id"],
        "product_name": result["product_name"],
        "rate": result["rate"],
        "min_income_required": result.get("min_income_required", ""),
        "created_at": created_at
    })

    if total != "":
        append_csv(scoring_out, SCORING_FIELDS, {
            "scoring_id": "scr_" + uuid.uuid4().hex[:12],
            "app_id": app_id,
            "income_points": parts.get("income", ""),
            "employment_points": parts.get("employment", ""),
            "age_points": parts.get("age", ""),
            "family_points": parts.get("family", ""),
            "bki_points": parts.get("bki", ""),
            "score": total,
            "dti": dti,
            "created_at": created_at
        })

    audit_event = {
        "event_id": "evt_" + uuid.uuid4().hex[:12],
        "run_id": run_id,
        "app_id": app_id,
        "action": "decision_made",
        "actor": "system",
        "at": created_at,
        "decision": result["decision"],
        "reason": result["reason"],
        "score": result["score"],
        "dti": result["dti"],
        "product_id": result["product_id"],
        "rate": result["rate"],
        "min_income_required": result.get("min_income_required", ""),
        "components": parts,
        "external_source": source,
        "metrics_calculated": bool(result["score"] != "" and result["dti"] != "")
    }
    append_csv(audit_out, AUDIT_FIELDS, {
        **audit_event,
        "components": json.dumps(parts, ensure_ascii=False)
    })
    append_jsonl(audit_log, audit_event)
    return result


REASON_MESSAGES = {
    "LOW_INCOME": "Отказ: доход ниже минимального порога.",
    "PRODUCT_MIN_INCOME": "Отказ: доход ниже минимального дохода, установленного для выбранного кредитного продукта в products.csv.",
    "PRODUCT_NOT_AVAILABLE": "Отказ: нет кредитного продукта, подходящего по возрасту, доходу, сумме и сроку.",
    "AGE_OUT_OF_RANGE": "Отказ: возраст должен быть от 18 до 70 лет.",
    "INVALID_EMPLOYMENT": "Отказ: статус занятости «безработный» не подходит под условия продукта.",
    "HIGH_DTI": "Отказ: долговая нагрузка (DTI) превышает допустимые 50% дохода.",
    "BKI_DELINQUENCY": "Отказ: кредитная история не прошла проверку (низкий балл или просрочка).",
    "AMOUNT_LIMIT": "Отказ: запрошенная сумма превышает 10 месячных доходов.",
    "SMEV_MISMATCH": "Отказ: данные клиента не совпали при проверке СМЭВ.",
    "LOW_SCORE": "Отказ: итоговый скоринговый балл ниже минимального порога 60.",
    "EXTERNAL_TIMEOUT": "Не хватает результата внешней проверки: сервис не ответил. Повторите попытку позже.",
    "MISSING_AGE": "Не хватает данных: заполните поле «возраст» (age).",
    "MISSING_EMPLOYMENT": "Не хватает данных: заполните поле «занятость» (employment).",
    "MISSING_INCOME": "Не хватает данных: заполните поле «доход» (income).",
    "VALIDATION_ERROR": "Данные отсутствуют или заполнены некорректно. Проверьте обязательные поля и форматы значений.",
}

def validation_details(app):
    problems = []
    def missing(key):
        return str(app.get(key) or "").strip() == ""
    if missing("age"):
        problems.append("age (возраст)")
    elif not str(app.get("age")).strip().isdigit():
        problems.append("age (возраст: требуется целое число)")
    else:
        age = int(app["age"])
        if not 18 <= age <= 70:
            problems.append("age (возраст вне диапазона 18–70)")
    if missing("employment"):
        problems.append("employment (занятость)")
    elif str(app.get("employment")).strip() not in EMPLOYMENT:
        problems.append("employment (неизвестный статус занятости)")
    if missing("income"):
        problems.append("income (доход)")
    else:
        try:
            if float(app["income"]) <= 0:
                problems.append("income (доход должен быть больше нуля)")
        except (TypeError, ValueError):
            problems.append("income (требуется число)")
    for key, label in (("amount", "amount (сумма кредита)"), ("term", "term (срок кредита)")):
        try:
            value = float(app.get(key, ""))
            if not math.isfinite(value):
                raise ValueError
            if key == "amount" and not 10000 <= value <= 5000000:
                problems.append(label + " (допустимо от 10 000 до 5 000 000)")
            if key == "term" and (not value.is_integer() or not 3 <= value <= 60):
                problems.append(label + " (целое число от 3 до 60 месяцев)")
        except (TypeError, ValueError):
            problems.append(label + " (отсутствует или требуется число)")
    if str(app.get("family") or "").strip() not in FAMILY:
        problems.append("family (семейное положение: married / children / single)")
    for key in ("app_id", "client_id", "passport_hash"):
        if missing(key):
            problems.append(key + " (обязательное поле)")
    return problems

def print_application_result(result, app):
    app_id = result["app_id"]
    decision = result["decision"]
    reason = result["reason"]
    labels = {"APPROVED": "ОДОБРЕНО", "REFUSED": "ОТКАЗ", "NEED_MORE_DATA": "НУЖНЫ ДАННЫЕ"}
    print(f"\nЗаявка {app_id}: {labels.get(decision, decision)}")
    if decision == "APPROVED":
        print("  Причина: заявка прошла проверки и набрала достаточный скоринговый балл.")
    else:
        print(f"  Причина [{reason}]: {REASON_MESSAGES.get(reason, 'Требуется дополнительная проверка заявки.')}")
        if reason == "VALIDATION_ERROR":
            problems = validation_details(app)
            if problems:
                print("  Проверьте: " + "; ".join(problems) + ".")
    if result["score"] != "":
        print(f"  Скоринговый балл: {result['score']}; DTI: {result['dti']}")

def main():
    parser = argparse.ArgumentParser(description="Учебный кредитный конвейер")
    parser.add_argument("input", help="applications.csv")
    parser.add_argument("--out", default="decisions.csv")
    parser.add_argument("--products", default="products.csv", help="каталог кредитных продуктов")
    parser.add_argument("--external-source", choices=("emulator", "csv"), default="emulator")
    parser.add_argument("--external-log", default="external.log")
    parser.add_argument("--audit-log", default="decision_audit.log")
    parser.add_argument("--clients-out", default="Client.csv")
    parser.add_argument("--decisions-out", default="Decision.csv")
    parser.add_argument("--scoring-out", default="ScoringResult.csv")
    parser.add_argument("--audit-out", default="AuditEvent.csv")
    args = parser.parse_args()
    run_id = uuid.uuid4().hex
    products = load_products(args.products)
    print(f"Загружено кредитных продуктов: {len(products)}")
    with open(args.input, newline="", encoding="utf-8-sig") as f:
        apps = list(csv.DictReader(f))
    results = []
    print(f"Обработка файла: {args.input}\nЗаявок: {len(apps)}")
    for i, app in enumerate(apps, start=1):
        result = process(
            app, products, args.external_source,
            args.external_log, args.audit_log, run_id, i,
            args.clients_out, args.decisions_out,
            args.scoring_out, args.audit_out
        )
        results.append(result)
        print_application_result(result, app)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader(); writer.writerows(results)
    print("\n--- ИТОГ ОБРАБОТКИ ---")
    print(f"run_id: {run_id}")
    print(f"external_source: {args.external_source}")
    print(f"OK: {args.out}; обработано: {len(results)}; решения: {dict(Counter(x['decision'] for x in results))}")
    print("Файлы сущностей: " + ", ".join([
        args.clients_out, args.decisions_out, args.scoring_out, args.audit_out
    ]))

if __name__ == "__main__": main()
