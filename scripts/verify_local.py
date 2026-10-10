"""
Local self-check for the HROne assignment. NOT part of the application.

What it does
  1. starts the API (uvicorn) against a throw-away database called `attendance_verify`
  2. seeds awkward data directly into MongoDB (no-log employee, future joiner, mid-month joiner, ties,
     weekend records, missing weekdays, half days, legacy documents)
  3. compares the four analytics endpoints with a slow, independent pure-Python implementation
  4. checks business rules, status codes, concurrency and /admin/explain (IXSCAN, no COLLSCAN)

Run from the repository root (MongoDB must be running):
    python scripts/verify_local.py
Optional: set MONGO_URI first if MongoDB is not on localhost:27017.
"""
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "attendance_verify"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRES = ("PRESENT", "WFH", "ON_DUTY")
MONTH, M_START, M_END = "2026-07", date(2026, 7, 1), date(2026, 7, 31)

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS", name)
    else:
        failed += 1
        print("  FAIL", name, extra)


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode()


def ms(y, mo, d, h, mi, s=0, milli=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + milli


def rhu(frac, places):
    """exact round-half-up of a Fraction"""
    scale = 10 ** places
    return math.floor(frac * scale + Fraction(1, 2)) / scale


def close(a, b):
    if a is None or b is None:
        return a is b
    return abs(a - b) < 1e-9


# --------------------------------------------------------------------------- seed data
def build_seed():
    rnd = random.Random(7)
    emps = [
        ("EMP0001", "Eng", "2026-01-05"), ("EMP0002", "Eng", "2026-07-15"),   # mid-month joiner
        ("EMP0003", "Eng", "2026-01-05"),                                      # never has logs
        ("EMP0004", "Eng", "2026-08-01"),                                      # joins after the month
        ("EMP0005", "Ops", "2026-01-05"), ("EMP0006", "Sales", "2026-01-05"),
        ("EMP0007", "Sales", "2026-01-05"), ("EMP0008", "Sales", "2026-02-02"),
        ("EMP0009", "Sales", "2026-02-02"), ("EMP0010", "Ops", "2026-03-02"),
    ]
    employees = [{"emp_code": c, "name": f"Name {c}", "email": f"{c}@x.com", "department": d,
                  "shift_start": "09:30", "shift_end": "18:30", "joined_on": j,
                  "created_at": datetime(2026, 1, 1, tzinfo=UTC)} for c, d, j in emps]
    docs = []
    for code, dept, joined in emps:
        if code in ("EMP0003", "EMP0004"):
            continue
        for day in range(1, 32):
            d = date(2026, 7, day)
            if d.isoformat() < joined:
                continue
            weekend = d.weekday() >= 5
            if (weekend and rnd.random() > 0.15) or (not weekend and rnd.random() < 0.12):
                continue  # most weekends empty; some weekdays have no record at all
            status = rnd.choice(["PRESENT"] * 5 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])
            if status in PRES:
                pin = datetime(2026, 7, day, 4, 0, tzinfo=UTC) + timedelta(minutes=rnd.randint(-20, 90))
                hours = round(rnd.uniform(2, 10), 2) if rnd.random() < 0.9 else None
                pout = pin + timedelta(hours=hours) if hours else None
                doc = {"status": status, "punch_in": pin, "punch_out": pout, "work_hours": hours,
                       "late_minutes": rnd.choice([0, 0, 0, 11, 25, 40]),
                       "overtime_minutes": rnd.choice([0, 0, 30, 45]) if hours else 0,
                       "half_day": bool(hours and hours < 4.5), "history": []}
                if rnd.random() < 0.15:  # legacy documents: no half_day / history
                    del doc["half_day"], doc["history"]
            else:
                doc = {"status": status, "punch_in": None, "punch_out": None, "work_hours": None,
                       "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
            docs.append({"emp_code": code, "date": d.isoformat(), **doc})
    # force ties in the leaderboard: EMP0006 and EMP0007 (Sales) both total 50 late minutes
    for code in ("EMP0006", "EMP0007"):
        for x in docs:
            if x["emp_code"] == code:
                x["late_minutes"] = 0
        mine = [x for x in docs if x["emp_code"] == code and x["status"] in PRES][:2]
        mine[0]["late_minutes"], mine[1]["late_minutes"] = 20, 30
    # a log whose employee does not exist must be ignored
    docs.append({"emp_code": "EMP0099", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 77,
                 "punch_in": datetime(2026, 7, 6, 5, 0, tzinfo=UTC), "punch_out": None, "work_hours": None,
                 "overtime_minutes": 0, "half_day": False, "history": []})
    return employees, docs


# --------------------------------------------------------------------------- oracle (slow, simple)
def weight(d):
    if d["status"] in PRES and date.fromisoformat(d["date"]).weekday() < 5:
        return Fraction(1, 2) if d.get("half_day") else Fraction(1)
    return Fraction(0)


def in_month(d):
    return M_START.isoformat() <= d["date"] <= M_END.isoformat()


def oracle_monthly(emps, docs, code):
    e = next(x for x in emps if x["emp_code"] == code)
    first = max(M_START, date.fromisoformat(e["joined_on"]))
    wd = sum(1 for i in range((M_END - first).days + 1) if (first + timedelta(i)).weekday() < 5) if first <= M_END else 0
    mine = [d for d in docs if d["emp_code"] == code and in_month(d)]
    pres = sum((weight(d) for d in mine), Fraction(0))
    return {"working_days": wd, "present_days": float(pres),
            "leave_days": sum(d["status"] == "LEAVE" for d in mine),
            "late_count": sum((d.get("late_minutes") or 0) > 0 for d in mine),
            "total_late_minutes": sum(d.get("late_minutes") or 0 for d in mine),
            "total_overtime_minutes": sum(d.get("overtime_minutes") or 0 for d in mine),
            "attendance_pct": rhu(pres / wd * 100, 2) if wd else None}


def oracle_summary(emps, docs):
    out = []
    for dept in sorted({e["department"] for e in emps}):
        codes = {e["emp_code"] for e in emps if e["department"] == dept and e["joined_on"] <= M_END.isoformat()}
        if not codes:
            continue
        mine = [d for d in docs if d["emp_code"] in codes and in_month(d)]
        hours = [Fraction(str(d["work_hours"])) for d in mine if d["status"] in PRES and d.get("work_hours") is not None]
        out.append({"department": dept, "headcount": len(codes),
                    "present_days": float(sum((weight(d) for d in mine), Fraction(0))),
                    "avg_work_hours": rhu(sum(hours) / len(hours), 2) if hours else None,
                    "late_count": sum((d.get("late_minutes") or 0) > 0 for d in mine),
                    "total_late_minutes": sum(d.get("late_minutes") or 0 for d in mine),
                    "leave_count": sum(d["status"] == "LEAVE" for d in mine),
                    "on_duty_count": sum(d["status"] == "ON_DUTY" for d in mine)})
    return out


def oracle_leaderboard(emps, docs, limit, dept=None):
    by_code = {e["emp_code"]: e for e in emps}
    totals = {}
    for d in docs:
        late = d.get("late_minutes") or 0
        if in_month(d) and late > 0 and d["emp_code"] in by_code:
            if dept is None or by_code[d["emp_code"]]["department"] == dept:
                t = totals.setdefault(d["emp_code"], [0, 0])
                t[0] += late
                t[1] += 1
    rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for code, (total, count) in rows:
        rank = 1 + sum(1 for _, (t2, _) in rows if t2 > total)
        if rank <= limit:
            out.append({"rank": rank, "emp_code": code, "name": by_code[code]["name"],
                        "department": by_code[code]["department"], "total_late_minutes": total, "late_count": count})
    return out


def oracle_trend(emps, docs, dept, d_from, d_to):
    codes = {e["emp_code"] for e in emps if e["department"] == dept}
    rows, rates = [], []
    for i in range((d_to - d_from).days + 1):
        day = d_from + timedelta(i)
        hc = sum(1 for e in emps if e["department"] == dept and e["joined_on"] <= day.isoformat())
        recs = [d for d in docs if d["emp_code"] in codes and d["date"] == day.isoformat()]
        present = sum((Fraction(1, 2) if d.get("half_day") else Fraction(1) for d in recs if d["status"] in PRES), Fraction(0))
        working = day.weekday() < 5
        rate = present / hc if working and hc > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({"date": day.isoformat(), "is_working_day": working, "headcount": hc,
                     "present_count": float(present),
                     "late_count": sum((d.get("late_minutes") or 0) > 0 for d in recs),
                     "attendance_rate": rhu(rate, 4) if rate is not None else None,
                     "moving_avg_7d": rhu(sum(window) / len(window), 4) if window else None})
    return rows


def same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return close(a, b)
    return a == b


def first_diff(a, b):
    if isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b)):
            if not same(x, y):
                return f"row {i}: got {x}  expected {y}"
        return f"lengths {len(a)} vs {len(b)}"
    return f"got {a} expected {b}"


# --------------------------------------------------------------------------- the checks
def analytics_checks(emps, docs):
    print("\nAnalytics vs independent oracle")
    for code in ("EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005", "EMP0006"):
        st, body = call("GET", f"/analytics/employees/{code}/monthly?month={MONTH}")
        exp = oracle_monthly(emps, docs, code)
        got = {k: v for k, v in body.items() if k not in ("emp_code", "month")} if st == 200 else body
        check(f"monthly {code}", st == 200 and same(got, exp), first_diff(got, exp))
    st, body = call("GET", f"/analytics/departments/summary?month={MONTH}")
    exp = oracle_summary(emps, docs)
    check("department summary", st == 200 and same(body.get("items"), exp), first_diff(body.get("items"), exp))
    st, body = call("GET", f"/analytics/departments/summary?month={MONTH}&department=Eng")
    exp = [r for r in oracle_summary(emps, docs) if r["department"] == "Eng"]
    check("department summary (department=Eng)", st == 200 and same(body.get("items"), exp))
    eng = next(r for r in oracle_summary(emps, docs) if r["department"] == "Eng")
    check("headcount counts the no-log employee, not the future joiner", eng["headcount"] == 3, eng)
    for limit, dept in ((10, None), (1, None), (2, None), (50, "Sales"), (1, "Sales")):
        q = f"/analytics/leaderboard/late?month={MONTH}&limit={limit}" + (f"&department={dept}" if dept else "")
        st, body = call("GET", q)
        exp = oracle_leaderboard(emps, docs, limit, dept)
        check(f"leaderboard limit={limit} dept={dept}", st == 200 and same(body.get("items"), exp),
              first_diff(body.get("items"), exp))
    for dept, a, b in (("Eng", date(2026, 7, 1), date(2026, 7, 31)), ("Sales", date(2026, 6, 25), date(2026, 7, 12)),
                       ("Ops", date(2026, 7, 4), date(2026, 7, 4)), ("Eng", date(2026, 7, 10), date(2026, 8, 5))):
        st, body = call("GET", f"/analytics/departments/{dept}/trend?from={a}&to={b}")
        exp = oracle_trend(emps, docs, dept, a, b)
        check(f"trend {dept} {a}..{b} ({len(exp)} rows)", st == 200 and same(body.get("items"), exp),
              first_diff(body.get("items"), exp))
    check("trend unknown department 404", call("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02")[0] == 404)
    check("trend range > 92 days 422", call("GET", "/analytics/departments/Eng/trend?from=2026-01-01&to=2026-06-01")[0] == 422)
    check("trend to < from 422", call("GET", "/analytics/departments/Eng/trend?from=2026-07-05&to=2026-07-01")[0] == 422)
    check("monthly unknown employee 404", call("GET", f"/analytics/employees/EMP7777/monthly?month={MONTH}")[0] == 404)
    check("bad month 422", call("GET", "/analytics/departments/summary?month=2026-13")[0] == 422)


def rules_checks():
    print("\nBusiness rules, status codes, concurrency")
    mk = lambda code, **kw: call("POST", "/employees", {"emp_code": code, "name": "T", "email": "t@x.com",
                                                        "department": "QA", "joined_on": "2026-01-05", **kw})
    check("create employee 201", mk("EMP5001")[0] == 201)
    check("duplicate employee 409", mk("EMP5001")[0] == 409)
    check("bad emp_code 422", mk("BAD1")[0] == 422)
    for i, (h, mi, s, milli, exp) in enumerate([(9, 40, 0, 0, 0), (9, 40, 1, 0, 10), (10, 15, 59, 0, 45), (9, 40, 0, 900, 0)]):
        code = f"EMP51{i:02d}"
        mk(code)
        st, b = call("POST", "/attendance/punch-in", {"emp_code": code, "punched_at": ms(2026, 8, 3, h, mi, s, milli)})
        check(f"late rule {h}:{mi:02d}:{s:02d}.{milli:03d} -> {exp}", st == 201 and b["late_minutes"] == exp and b["punch_in"] % 1000 == 0, b)
    mk("EMP5200", shift_start="22:00", shift_end="06:00")
    st, b = call("POST", "/attendance/punch-in", {"emp_code": "EMP5200", "punched_at": ms(2026, 8, 4, 2, 0)})
    check("overnight punch-in after midnight belongs to previous day", st == 201 and b["date"] == "2026-08-03" and b["late_minutes"] == 240, b)
    st, b = call("POST", "/attendance/punch-out", {"emp_code": "EMP5200", "punched_at": ms(2026, 8, 4, 6, 40)})
    check("overnight overtime 40", st == 200 and b["overtime_minutes"] == 40, b)
    check("punch-in unknown employee 404", call("POST", "/attendance/punch-in", {"emp_code": "EMP7777"})[0] == 404)
    check("punch-in seconds-looking timestamp 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP5001", "punched_at": 1783312500})[0] == 422)
    check("punch-in status ABSENT 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP5001", "status": "ABSENT"})[0] == 422)

    # concurrency: 12 identical punch-ins -> exactly one 201
    mk("EMP5300")
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda _: call("POST", "/attendance/punch-in", {"emp_code": "EMP5300", "punched_at": ms(2026, 8, 3, 9, 30)})[0], range(12)))
    check("12 parallel punch-ins -> one 201, rest 409", codes.count(201) == 1 and codes.count(409) == 11, codes)
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda _: call("POST", "/attendance/punch-out", {"emp_code": "EMP5300", "punched_at": ms(2026, 8, 3, 18, 45)})[0], range(12)))
    check("12 parallel punch-outs -> one 200", codes.count(200) == 1 and codes.count(409) == 11, codes)
    st, rec = call("GET", "/attendance?emp_code=EMP5300")
    r = rec["items"][0]
    check("work_hours 9.25, overtime 15 -> 0, not half day", r["work_hours"] == 9.25 and r["overtime_minutes"] == 0 and r["half_day"] is False, r)

    # regularization + concurrent regularizations never lose history
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda i: call("PATCH", "/attendance/EMP5300/2026-08-03",
                            {"status": "WFH" if i % 2 else "ON_DUTY", "reason": "parallel edit", "regularized_by": f"u{i}"})[0], range(8)))
    st, rec = call("GET", "/attendance?emp_code=EMP5300")
    check("parallel PATCH: history entries == successful PATCHes", len(rec["items"][0]["history"]) == codes.count(200) and codes.count(200) >= 1 and set(codes) <= {200, 409, 422}, (codes, rec["items"][0]["history"]))
    st, b = call("PATCH", "/attendance/EMP5300/2026-08-03", {"punch_in": ms(2026, 8, 3, 10, 5), "reason": "biometric glitch", "regularized_by": "hr"})
    check("PATCH recomputes late_minutes=35 and records changes", st == 200 and b["late_minutes"] == 35 and "late_minutes" in b["history"][-1]["changes"] and isinstance(b["history"][-1]["changes"]["punch_in"]["from"], int), b)
    check("PATCH no-op 422", call("PATCH", "/attendance/EMP5300/2026-08-03", {"punch_in": ms(2026, 8, 3, 10, 5), "reason": "same again", "regularized_by": "hr"})[0] == 422)
    check("PATCH unknown date 404", call("PATCH", "/attendance/EMP5300/2026-08-20", {"status": "LEAVE", "reason": "approved", "regularized_by": "hr"})[0] == 404)
    st, b = call("PATCH", "/attendance/EMP5300/2026-08-03", {"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"})
    check("PATCH to LEAVE clears punches", st == 200 and b["punch_in"] is None and b["punch_out"] is None and b["work_hours"] is None, b)
    check("list pagination total reflects filter", call("GET", "/attendance?emp_code=EMP5300&page_size=1")[1]["total"] == 1)
    check("list date_from > date_to 422", call("GET", "/attendance?date_from=2026-08-09&date_to=2026-08-01")[0] == 422)


def explain_checks():
    print("\n/admin/explain (needs IXSCAN and no COLLSCAN)")
    cases = {
        "attendance_list": "",
        "attendance_list ": "?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31",
        "employee_monthly": f"?emp_code=EMP0001&month={MONTH}",
        "department_summary": f"?month={MONTH}",
        "late_leaderboard": f"?month={MONTH}&limit=5",
        "department_trend": "?department=Eng&from=2026-07-01&to=2026-07-31",
    }
    for name, qs in cases.items():
        st, body = call("GET", f"/admin/explain/{name.strip()}{qs}")
        text = json.dumps(body)
        check(f"explain {name.strip()} {qs}", st == 200 and "IXSCAN" in text and "COLLSCAN" not in text,
              (st, "COLLSCAN" in text, "IXSCAN" in text))
    check("explain missing parameter 422", call("GET", "/admin/explain/employee_monthly")[0] == 422)


def main():
    db = MongoClient(MONGO_URI, tz_aware=True)[DB_NAME]
    db.client.drop_database(DB_NAME)
    emps, docs = build_seed()
    db.employees.insert_many([dict(e) for e in emps])
    db.attendance_logs.insert_many([dict(d) for d in docs])  # seeded with only the default _id index, like the grader

    env = {**os.environ, "MONGO_URI": MONGO_URI, "MONGO_DB": DB_NAME}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=root, env=env)
    try:
        for _ in range(60):
            try:
                if call("GET", "/health")[0] == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            sys.exit("API did not become healthy within 30 s")
        analytics_checks(emps, docs)
        rules_checks()
        explain_checks()
    finally:
        proc.terminate()
        db.client.drop_database(DB_NAME)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
