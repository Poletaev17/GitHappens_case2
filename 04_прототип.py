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

OUT_FIELDS = ["app_id", "decision", "reason", "score", "dti"]
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


def validate(app):
    # По обязательному сценарию app_0004 приоритет у отсутствующего дохода,
    # даже если одновременно не заполнен возраст.
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


def compute_dti(amount, term, income):
    if income <= 0 or term <= 0:
        return None
    mr = RATE / 12
    payment = amount / term if mr == 0 else amount * mr / (1 - (1 + mr) ** (-term))
    return round(payment / income, 2)


def decide(app, score_total, dti, bki_score, delinquency, smev):
    age, income = int(app["age"]), float(app["income"])
    if age < 18 or age > 70: return "REFUSED", "AGE_OUT_OF_RANGE"
    if app["employment"] == "unemployed": return "REFUSED", "INVALID_EMPLOYMENT"
    if income < 30000: return "REFUSED", "LOW_INCOME"
    if dti is not None and dti > 0.5: return "REFUSED", "HIGH_DTI"
    if bki_score < 500 or delinquency: return "REFUSED", "BKI_DELINQUENCY"
    if float(app["amount"]) > 10 * income: return "REFUSED", "AMOUNT_LIMIT"
    if smev == "MISMATCH": return "REFUSED", "SMEV_MISMATCH"
    if score_total < 60: return "REFUSED", "LOW_SCORE"
    return "APPROVED", ""


def append_jsonl(path, event):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


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


def make_result(app_id, decision, reason, total="", dti=""):
    return {"app_id": app_id, "decision": decision, "reason": reason, "score": total, "dti": dti}


def process(app, source="emulator", external_log="external.log", audit_log="decision_audit.log", run_id=None, index=1):
    run_id = run_id or uuid.uuid4().hex
    app_id = str(app.get("app_id") or "UNKNOWN")
    total = dti = ""
    parts = {}
    early = validate(app)
    if early:
        result = make_result(app_id, *early)
    elif float(app["income"]) < 30000:
        result = make_result(app_id, "REFUSED", "LOW_INCOME")
    else:
        if source == "csv":
            smev_call = lambda: {"status": app.get("smev_status") or "NOT_CALLED"}
            def bki_call():
                if app.get("bki_score", "") == "": raise ValueError("bki_score отсутствует в CSV")
                val = int(app["bki_score"])
                return {"score": val, "delinquency": val < 500, "requests_90d": 1}
        else:
            smev_call = lambda: smev_emulator(app["client_id"])
            bki_call = lambda: bki_emulator(app["passport_hash"])
        smev = call_external("SMEV", f"{run_id[:8]}-{index:04d}-SMEV", app_id, run_id, smev_call, external_log)
        if smev is None:
            result = make_result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT")
        elif smev["status"] == "MISMATCH":
            result = make_result(app_id, "REFUSED", "SMEV_MISMATCH")
        elif smev["status"] != "MATCH":
            result = make_result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT")
        else:
            bki = call_external("BKI", f"{run_id[:8]}-{index:04d}-BKI", app_id, run_id, bki_call, external_log)
            if bki is None:
                result = make_result(app_id, "NEED_MORE_DATA", "EXTERNAL_TIMEOUT")
            else:
                total, parts = score(app, bki["score"], bki["delinquency"])
                dti = compute_dti(float(app["amount"]), int(app["term"]), float(app["income"]))
                decision, reason = decide(app, total, dti, bki["score"], bki["delinquency"], smev["status"])
                result = make_result(app_id, decision, reason, total, dti)
    append_jsonl(audit_log, {"event_id": "evt_" + uuid.uuid4().hex[:12], "run_id": run_id,
        "app_id": app_id, "action": "decision_made", "actor": "system", "at": now_iso(),
        "decision": result["decision"], "reason": result["reason"], "score": result["score"],
        "dti": result["dti"], "components": parts, "external_source": source,
        "metrics_calculated": bool(result["score"] != "" and result["dti"] != "")})
    return result



REASON_MESSAGES = {
    "LOW_INCOME": "Отказ: доход ниже минимального порога 30 000 рублей.",
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
    parser.add_argument("--external-source", choices=("emulator", "csv"), default="emulator")
    parser.add_argument("--external-log", default="external.log")
    parser.add_argument("--audit-log", default="decision_audit.log")
    args = parser.parse_args()
    run_id = uuid.uuid4().hex
    with open(args.input, newline="", encoding="utf-8-sig") as f:
        apps = list(csv.DictReader(f))
    results = []
    print(f"Обработка файла: {args.input}\nЗаявок: {len(apps)}")
    for i, app in enumerate(apps, start=1):
        result = process(app, args.external_source, args.external_log, args.audit_log, run_id, i)
        results.append(result)
        print_application_result(result, app)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader(); writer.writerows(results)
    print("\n--- ИТОГ ОБРАБОТКИ ---")
    print(f"run_id: {run_id}")
    print(f"external_source: {args.external_source}")
    print(f"OK: {args.out}; обработано: {len(results)}; решения: {dict(Counter(x['decision'] for x in results))}")

if __name__ == "__main__": main()
