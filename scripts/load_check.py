import json, os, random, time, urllib.request
from datetime import date, datetime, timedelta, timezone
from pymongo import MongoClient

db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)["attendance_load"]
db.client.drop_database("attendance_load")
rnd = random.Random(1)
depts = ["Eng", "Ops", "Sales", "HR", "Finance"]
emps = [{"emp_code": f"EMP{i:04d}", "name": f"N{i}", "email": f"n{i}@x.com", "department": depts[i % 5],
         "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05",
         "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)} for i in range(1, 1001)]
db.employees.insert_many(emps)
docs = []
for e in emps:
    for d in range(100):  # 1000 employees x 100 days = 100,000 records
        day = date(2026, 5, 1) + timedelta(days=d)
        pin = datetime(day.year, day.month, day.day, 4, 0, tzinfo=timezone.utc) + timedelta(minutes=rnd.randint(-20, 60))
        docs.append({"emp_code": e["emp_code"], "date": day.isoformat(), "status": "PRESENT", "punch_in": pin,
                     "punch_out": pin + timedelta(hours=9), "work_hours": 9.0, "late_minutes": rnd.choice([0, 0, 15, 40]),
                     "overtime_minutes": 0, "half_day": False, "history": []})
db.attendance_logs.insert_many(docs)
print("seeded", db.attendance_logs.count_documents({}))
print("Now start the app with MONGO_DB=attendance_load, then press Enter")
input()
def get(p):
    t = time.time()
    with urllib.request.urlopen("http://127.0.0.1:8000" + p, timeout=60) as r:
        body = r.read()
    return round(time.time() - t, 2), body.decode()
for p in ["/attendance?page=1", "/attendance?emp_code=EMP0007&date_from=2026-06-01&date_to=2026-06-30",
          "/analytics/employees/EMP0007/monthly?month=2026-06", "/analytics/departments/summary?month=2026-06",
          "/analytics/leaderboard/late?month=2026-06", "/analytics/departments/Eng/trend?from=2026-05-01&to=2026-07-30"]:
    print(get(p)[0], "s", p)
for n, q in [("attendance_list", ""), ("employee_monthly", "?emp_code=EMP0007&month=2026-06"),
             ("department_summary", "?month=2026-06"), ("late_leaderboard", "?month=2026-06"),
             ("department_trend", "?department=Eng&from=2026-05-01&to=2026-07-30")]:
    t = get(f"/admin/explain/{n}{q}")[1]
    print(n, "IXSCAN" in t, "COLLSCAN" in t)