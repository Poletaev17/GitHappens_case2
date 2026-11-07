#!/usr/bin/env python3
"""
CLI-прототип кредитного конвейера — кейс 2.

Использование:
    python app.py applications.csv
    python app.py applications.csv --out decisions.csv

Читает CSV, прогоняет каждую заявку через пайплайн, пишет решения.

TODO для команды:
    1. Дописать validate() — правила из 03_data_dictionary.md.
    2. Дописать bki_emulator() — 3-4 сценария.
    3. Дописать smev_emulator() — MATCH / MISMATCH.
    4. Дописать scoring() — веса из 02_scoring_rules.md.
    5. Дописать decide() — порядок правил отказа.
    6. Считать DTI по формуле из 02.
    7. Записать decisions.csv с колонками: app_id, decision, reason, score, dti.
"""
import csv
import hashlib
import sys

FIELDNAMES_OUT = ["app_id", "decision", "reason", "score", "dti", "amount_approved", "term_approved"]


def bki_emulator(passport_hash):
    """
    TODO: реализовать сценарии:
      - по hash(passport_hash) mod 10:
        0..5 → score = 600 + (hash % 200), delinquency = False
        6..8 → score = 450 + (hash % 100), delinquency = False
        9    → score = 400, delinquency = True
    Возвращает (score, delinquency).
    """
    h = int(hashlib.md5(passport_hash.encode()).hexdigest(), 16) % 10
    # ОБРАЗЕЦ
    if h <= 5:
        return (600 + h * 20, False)
    # TODO: сценарии 6..9
    return (500, False)


def smev_emulator(client_id):
    """
    TODO: MATCH для 9 из 10 client_id, MISMATCH для 10-го.
    Возвращает строку MATCH / MISMATCH / TIMEOUT.
    """
    h = int(hashlib.md5(client_id.encode()).hexdigest(), 16) % 10
    return "MISMATCH" if h == 0 else "MATCH"


def validate(app):
    """TODO: проверить age, income, employment, family, amount, term.
    Вернуть список ошибок [(field, code), ...]."""
    errors = []
    # ОБРАЗЕЦ — age
    if app.get("age"):
        age = int(app["age"])
        if age < 18 or age > 70:
            errors.append(("age", "AGE_OUT_OF_RANGE"))
    # TODO: income, employment, family, amount, term
    return errors


def score(app, bki_score, delinquency):
    """
    TODO: считать score по весам из 02_scoring_rules.md.
    Вернуть (score, components_dict).
    """
    # ОБРАЗЕЦ — только доход и занятость
    income = float(app["income"])
    s_income = 30 if income >= 150000 else 20 if income >= 80000 else 10 if income >= 40000 else 0
    s_emp = {"employed": 25, "self-employed": 20, "retired": 10,
             "student": 5, "unemployed": 0}[app["employment"]]
    # TODO: возраст, семья, БКИ
    total = s_income + s_emp
    return total, {"income": s_income, "employment": s_emp}


def compute_dti(amount, term, income):
    mr = 0.18 / 12
    payment = amount * mr / (1 - (1 + mr) ** (-term))
    return round(payment / income, 2) if income else None


def decide(app, score_total, dti, delinquency, smev_status):
    """TODO: применить правила отказа в порядке из 02."""
    if dti is not None and dti > 0.5:
        return "REFUSED", "HIGH_DTI"
    if score_total < 60:
        return "REFUSED", "BKI_DELINQUENCY"
    if smev_status == "MISMATCH":
        return "REFUSED", "SMEV_MISMATCH"
    return "APPROVED", ""


def process(app):
    errors = validate(app)
    if errors:
        return {"app_id": app["app_id"], "decision": "NEED_MORE_DATA",
                "reason": errors[0][1], "score": "", "dti": "",
                "amount_approved": "", "term_approved": ""}

    bki_score, delinquency = bki_emulator(app["passport_hash"])
    smev = smev_emulator(app["client_id"])
    score_total, _ = score(app, bki_score, delinquency)
    dti = compute_dti(float(app["amount"]), int(app["term"]), float(app["income"]))
    decision, reason = decide(app, score_total, dti, delinquency, smev)

    approved_amount = app["amount"] if decision == "APPROVED" else ""
    approved_term = app["term"] if decision == "APPROVED" else ""
    return {"app_id": app["app_id"], "decision": decision, "reason": reason,
            "score": score_total, "dti": dti,
            "amount_approved": approved_amount, "term_approved": approved_term}


def main():
    if len(sys.argv) < 2:
        print("Использование: python app.py applications.csv [--out decisions.csv]")
        sys.exit(1)

    src = sys.argv[1]
    out = "decisions.csv"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]

    with open(src, encoding="utf-8") as f:
        apps = list(csv.DictReader(f))

    results = [process(a) for a in apps]

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES_OUT)
        w.writeheader()
        for r in results:
            w.writerow(r)

    stats = {}
    for r in results:
        stats[r["decision"]] = stats.get(r["decision"], 0) + 1
    print(f"OK: {out}")
    print(f"    обработано: {len(results)}")
    print(f"    решения:    {stats}")


if __name__ == "__main__":
    main()