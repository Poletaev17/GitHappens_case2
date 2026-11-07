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
BASE_MIN_INCOME = 30_000
MIN_SCORE = 60
MAX_DTI = 0.5
MAX_AMOUNT_TO_INCOME = 10
BKI_REFUSAL_THRESHOLD = 500
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


def normalize_product_value(value):
    return str(value or "").strip().replace("\\_", "_")


def load_products(filename="product.csv"):
    required = {
        "product_id", "name", "min_amount", "max_amount", "min_term",
        "max_term", "min_age", "max_age", "min_income", "rate"
    }
    with open(filename, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {normalize_product_value(name) for name in (reader.fieldnames or [])}
        missing = required - headers
        if missing:
            raise ValueError(f"В {filename} отсутствуют поля: {', '.join(sorted(missing))}")
        products = []
        ids = set()
        for lineno, record in enumerate(reader, start=2):
            if None in record:
                raise ValueError(f"В {filename}, строка {lineno}: лишние значения")
            product = {normalize_product_value(k): normalize_product_value(v)
                       for k, v in record.items()}
            try:
                for field in ("min_amount", "max_amount", "min_income", "rate"):
                    value = float(product[field])
                    if not math.isfinite(value):
                        raise ValueError(field)
                for field in ("min_term", "max_term", "min_age", "max_age"):
                    int(product[field])
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"В {filename}, строка {lineno}: неверное числовое поле") from exc
            pid = product.get("product_id", "")
            if not pid or not product.get("name") or pid in ids:
                raise ValueError(f"В {filename}, строка {lineno}: пустое имя или повторяющийся ID")
            if (float(product["min_amount"]) <= 0 or
                float(product["min_amount"]) > float(product["max_amount"]) or
                int(product["min_term"]) < 1 or
                int(product["min_term"]) > int(product["max_term"]) or
                int(product["min_age"]) < 18 or
                int(product["max_age"]) > 70 or
                int(product["min_age"]) > int(product["max_age"]) or
                float(product["min_income"]) <= 0 or
                float(product["rate"]) < 0):
                raise ValueError(f"В {filename}, строка {lineno}: несовместимые границы")
            ids.add(pid)
            products.append(product)
    if not products:
        raise ValueError(f"В {filename} нет кредитных продуктов")
    return products


def product_candidates(app, products):
    age = int(app["age"])
    amount = float(app["amount"])
    term = int(app["term"])
    requested_id = normalize_product_value(app.get("product_id"))
    return [
        p for p in products
        if (not requested_id or p["product_id"] == requested_id)
        and int(p["min_age"]) <= age <= int(p["max_age"])
        and float(p["min_amount"]) <= amount <= float(p["max_amount"])
        and int(p["min_term"]) <= term <= int(p["max_term"])
    ]


def select_product(app, products):
    income = float(app["income"])
    eligible = [p for p in product_candidates(app, products)
                if income >= float(p["min_income"])]
    return min(eligible, key=lambda p: (float(p["rate"]), p["product_id"])) if eligible else None


def get_product_rejection_reason(app, products):
    candidates = product_candidates(app, products)
    if not candidates:
        return "PRODUCT_NOT_AVAILABLE"
    if not any(float(app["income"]) >= float(p["min_income"]) for p in candidates):
        return "LOW_INCOME"
    return "PRODUCT_NOT_AVAILABLE"


def product_for_low_income(app, products):
    options = product_candidates(app, products)
    return min(options, key=lambda p: float(p["min_income"])) if options else None

def validate(app):
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
    if employment == "unemployed":
        return "REFUSED", "INVALID_EMPLOYMENT"
    if not employment:
        return "NEED_MORE_DATA", "MISSING_EMPLOYMENT"
    if employment not in EMPLOYMENT:
        return "NEED_MORE_DATA", "VALIDATION_ERROR"

    income_raw = str(app.get("income") or "").strip()
    if not income_raw:
        return "NEED_MORE_DATA", "MISSING_INCOME"
    try:
        income = float(income_raw)
        amount = float(app.get("amount", ""))
        term = int(app.get("term", ""))
    except (TypeError, ValueError):
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if not all(math.isfinite(x) for x in (income, amount)):
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if income <= 0 or amount <= 0 or term <= 0:
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if str(app.get("family") or "").strip() not in FAMILY:
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if not str(app.get("app_id") or "").strip() or not str(app.get("client_id") or "").strip():
        return "NEED_MORE_DATA", "VALIDATION_ERROR"
    if not re.fullmatch(r"(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{64})",
                        str(app.get("passport_hash") or "").strip()):
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


def compute_dti(amount, term, income, rate=RATE, round_digits=2):
    if (not all(math.isfinite(x) for x in (amount, income, rate))
            or income <= 0 or term <= 0 or rate < 0):
        return None
    monthly_rate = rate / 12
    payment = amount / term if monthly_rate == 0 else (
        amount * monthly_rate / (1 - (1 + monthly_rate) ** (-term))
    )
    dti = payment / income
    return round(dti, round_digits) if round_digits is not None else dti


def decide(app, score_total, dti, bki_score, delinquency, smev, product=None):
    age = int(app["age"])
    if not 18 <= age <= 70:
        return "REFUSED", "AGE_OUT_OF_RANGE"
    if app["employment"] == "unemployed":
        return "REFUSED", "INVALID_EMPLOYMENT"
    if not str(app.get("income") or "").strip():
        return "NEED_MORE_DATA", "MISSING_INCOME"
    income = float(app["income"])
    required_income = float(product["min_income"]) if product else BASE_MIN_INCOME
    if income < required_income:
        return "REFUSED", "LOW_INCOME"
    if dti is not None and dti > MAX_DTI:
        return "REFUSED", "HIGH_DTI"
    if bki_score < BKI_REFUSAL_THRESHOLD or delinquency:
        return "REFUSED", "BKI_DELINQUENCY"
    if float(app["amount"]) > MAX_AMOUNT_TO_INCOME * income:
        return "REFUSED", "AMOUNT_LIMIT"
    if smev == "MISMATCH":
        return "REFUSED", "SMEV_MISMATCH"
    if smev != "MATCH":
        return "NEED_MORE_DATA", "EXTERNAL_TIMEOUT"
    if score_total < MIN_SCORE:
        return "REFUSED", "LOW_SCORE"
    return "APPROVED", ""


def csv_bki_input_error(app):
    raw = str(app.get("bki_score") or "").strip()
    if not raw:
        return "MISSING_BKI"
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return "VALIDATION_ERROR"
    if not 300 <= number <= 850:
        return "VALIDATION_ERROR"
    delinquency = str(app.get("delinquency") or "").strip().lower()
    if delinquency and delinquency not in ("true", "false", "1", "0", "yes", "no"):
        return "VALIDATION_ERROR"
    return None


def csv_bki_value(app):
    delinquency = str(app.get("delinquency") or "").strip().lower()
    return {
        "score": int(app["bki_score"]),
        "delinquency": delinquency in ("true", "1", "yes"),
        "requests_90d": 1
    }

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


def ensure_csv_header(path, fields):
    import os
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


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
    safe_client = {field: client.get(field, "") for field in CLIENT_FIELDS}
    if not re.fullmatch(r"(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{64})",
                        str(safe_client.get("passport_hash") or "").strip()):
        safe_client["passport_hash"] = ""
    rows.append(safe_client)
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
            reason = get_product_rejection_reason(app, products)
            if reason == "LOW_INCOME":
                product = product_for_low_income(app, products)
            result = make_result(app_id, "REFUSED", reason, product=product)
        else:
            exact_dti = compute_dti(
                float(app["amount"]), int(app["term"]),
                float(app["income"]), float(product["rate"]), round_digits=None
            )
            dti = round(exact_dti, 2)
            if exact_dti > MAX_DTI:
                result = make_result(app_id, "REFUSED", "HIGH_DTI", dti=dti, product=product)
            elif source == "csv" and (bki_error := csv_bki_input_error(app)):
                result = make_result(app_id, "NEED_MORE_DATA", bki_error, dti=dti, product=product)
            else:
                if source == "csv":
                    bki_call = lambda: csv_bki_value(app)
                else:
                    bki_call = lambda: bki_emulator(app["passport_hash"])
                bki = call_external(
                    "BKI", f"{run_id[:8]}-{index:04d}-BKI",
                    app_id, run_id, bki_call, external_log
                )
                if bki is None:
                    result = make_result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT", dti=dti, product=product)
                else:
                    bki_value = int(bki["score"])
                    delinquency = bool(bki["delinquency"])
                    if not 300 <= bki_value <= 850:
                        result = make_result(app_id, "NEED_MORE_DATA", "VALIDATION_ERROR", dti=dti, product=product)
                    else:
                        total, parts = score(app, bki_value, delinquency)
                        early_decision, early_reason = decide(
                            app, total, exact_dti, bki_value, delinquency,
                            "MATCH", product
                        )
                        if early_reason in ("BKI_DELINQUENCY", "AMOUNT_LIMIT", "LOW_SCORE"):
                            result = make_result(app_id, early_decision, early_reason, total, dti, product)
                        else:
                            if source == "csv":
                                smev_call = lambda: {"status": app.get("smev_status") or "NOT_CALLED"}
                            else:
                                smev_call = lambda: smev_emulator(app["client_id"])
                            smev = call_external(
                                "SMEV", f"{run_id[:8]}-{index:04d}-SMEV",
                                app_id, run_id, smev_call, external_log
                            )
                            if smev is None:
                                decision, reason = "NEED_MORE_DATA", "EXTERNAL_TIMEOUT"
                            else:
                                status = smev.get("status", "NOT_CALLED")
                                if status not in ("MATCH", "MISMATCH", "TIMEOUT", "NOT_CALLED"):
                                    decision, reason = "NEED_MORE_DATA", "VALIDATION_ERROR"
                                else:
                                    decision, reason = decide(
                                        app, total, exact_dti, bki_value,
                                        delinquency, status, product
                                    )
                            result = make_result(app_id, decision, reason, total, dti, product)

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
    else:
        ensure_csv_header(scoring_out, SCORING_FIELDS)

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
    "LOW_INCOME": "Отказ: доход ниже min_income выбранного продукта (product.csv).",
    "PRODUCT_MIN_INCOME": "Отказ: доход ниже установленного порога продукта.",
    "PRODUCT_NOT_AVAILABLE": "Отказ: нет продукта, подходящего по ID, возрасту, сумме и сроку.",
    "AGE_OUT_OF_RANGE": "Отказ: возраст должен быть от 18 до 70 лет.",
    "INVALID_EMPLOYMENT": "Отказ: статус занятости «безработный» не подходит под условия продукта.",
    "HIGH_DTI": "Отказ: DTI по ставке продукта превышает 50% дохода.",
    "BKI_DELINQUENCY": "Отказ: рейтинг БКИ ниже 500 или есть просрочка.",
    "AMOUNT_LIMIT": "Отказ: запрошенная сумма превышает 10 месячных доходов.",
    "SMEV_MISMATCH": "Отказ: данные клиента не совпали при проверке СМЭВ.",
    "LOW_SCORE": "Отказ: итоговый скоринговый балл ниже минимального порога 60.",
    "EXTERNAL_TIMEOUT": "Не хватает результата внешней проверки: сервис не ответил. Повторите попытку позже.",
    "MISSING_AGE": "Не хватает данных: заполните поле «возраст» (age).",
    "MISSING_EMPLOYMENT": "Не хватает данных: заполните поле «занятость» (employment).",
    "MISSING_INCOME": "Не хватает данных: заполните поле «доход» (income).",
    "MISSING_BKI": "Не хватает данных: заполните поле «кредитный рейтинг» (bki_score).",
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
    digest = str(app.get("passport_hash") or "").strip()
    if digest and not re.fullmatch(r"(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{64})", digest):
        problems.append("passport_hash (16 или 64 hex-символа)")
    return problems

def print_application_result(result, app):
    app_id = result["app_id"]
    decision = result["decision"]
    reason = result["reason"]
    labels = {"APPROVED": "ОДОБРЕНО", "REFUSED": "ОТКАЗ", "NEED_MORE_DATA": "НУЖНЫ ДАННЫЕ"}
    print(f"\nЗаявка {app_id}: {labels.get(decision, decision)}")
    if result.get("product_name"):
        print(
            f"  Выбранный продукт: {result['product_name']} "
            f"(ID: {result.get('product_id', '')})"
        )
    else:
        print("  Выбранный продукт: не определён (нет подходящего продукта или недостаточно данных).")
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
    parser.add_argument("input", help="входной файл заявок (например, application.csv)")
    parser.add_argument("--out", default="decisions.csv")
    parser.add_argument("--products", default="product.csv", help="каталог кредитных продуктов")
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
    for output_path, fields in (
        (args.clients_out, CLIENT_FIELDS),
        (args.decisions_out, DECISION_FIELDS),
        (args.scoring_out, SCORING_FIELDS),
        (args.audit_out, AUDIT_FIELDS),
    ):
        ensure_csv_header(output_path, fields)
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
