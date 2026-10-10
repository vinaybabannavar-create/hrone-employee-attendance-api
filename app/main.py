"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:   uvicorn app.main:app --port 8000
Env:   MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Layout of this single file
    1. configuration & database handles
    2. small helpers (time conversion, rounding, validation)
    3. business rules R1 - R5 (pure functions, no database)
    4. request models
    5. endpoints: health, employees, attendance
    6. analytics pipelines + endpoints
    7. explain endpoint
"""
import calendar
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration & database handles
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already set in the environment

log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones, so
# subtracting / converting instants can never silently mix naive and aware values.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000, maxPoolSize=100)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# Sort used by GET /attendance (and by its explain). A dict keeps key order.
ATT_SORT = {"date": DESCENDING, "emp_code": ASCENDING}


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
INDEXES = [
    # (collection, keys, options)
    (employees, [("emp_code", ASCENDING)], {"unique": True, "name": "uniq_emp_code"}),
    (employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
    (employees, [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
    (employees, [("joined_on", ASCENDING)], {"name": "joined_on"}),
    # one record per (employee, day): the guarantee behind the punch-in race, the PATCH lookup,
    # the employee monthly report and "emp_code + date range" listings.
    (logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uniq_emp_date"}),
    # listing sorted by date desc / emp_code asc, and every month-wide analytics $match on date.
    (logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_emp"}),
    # listing filtered by status.
    (logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
    # punch-out: "latest record of this employee whose punch_in <= punched_at".
    (logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
]

_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> bool:
    """Create all indexes. create_index with identical options is a no-op, so this is idempotent."""
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return True
        ok = True
        for coll, keys, opts in INDEXES:
            try:
                coll.create_index(keys, **opts)
            except ConnectionFailure as exc:  # includes "server selection timed out": no point trying the rest
                log.warning("MongoDB not reachable, indexes will be created later: %s", exc)
                return False
            except PyMongoError as exc:  # keep the service up; health/explain will show the problem
                ok = False
                log.error("could not create index %s on %s: %s", opts.get("name"), coll.name, exc)
        _indexes_ready = ok
        return ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # MongoDB may not be reachable yet; /health retries
        log.warning("index creation skipped at startup: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime (integer arithmetic, no float drift)."""
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def floor_to_second(ms: int) -> int:
    """R1: instants are truncated to whole seconds before any calculation."""
    return ms - ms % 1000


def now_ms() -> int:
    return int(time.time() * 1000)


def unprocessable(msg: str, loc: tuple = ("body",)) -> HTTPException:
    """422 with the same body shape FastAPI uses for its own validation errors."""
    return HTTPException(422, detail=[{"type": "value_error", "loc": list(loc), "msg": msg, "input": None}])


_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def parse_ymd(value: str) -> Optional[date]:
    if not _DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def require_date(value: str, loc: tuple) -> date:
    d = parse_ymd(value)
    if d is None:
        raise unprocessable("must be a valid date in YYYY-MM-DD format", loc)
    return d


def month_bounds(month: str) -> tuple[date, date]:
    """'2026-07' -> (2026-07-01, 2026-07-31). 422 for anything that is not a real month."""
    if not _MONTH_RE.fullmatch(month) or int(month[:4]) < 1:
        raise unprocessable("month must look like YYYY-MM", ("query", "month"))
    y, m = int(month[:4]), int(month[5:])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def half_up(value: Any, places: int) -> Optional[float]:
    """R8: round half-up. (Python's round() and MongoDB's $round are both half-to-even.)
    Floats are first cleaned to 10 places so 0.12344999999999 (binary noise) behaves like 0.12345."""
    if value is None:
        return None
    d = value if isinstance(value, Decimal) else Decimal(repr(round(float(value), 10)))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def to_api(value: Any) -> Any:
    """Recursively turn stored datetimes into epoch milliseconds for responses."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


def page_skip(page: int, page_size: int) -> Optional[int]:
    skip = (page - 1) * page_size
    return skip if skip < 2**31 else None  # absurd pages simply return nothing


# --------------------------------------------------------------------------- #
# 3. Business rules R1 - R5 (pure functions)
# --------------------------------------------------------------------------- #
def hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


def at_ist(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date of the punch-in, except that on an overnight shift a punch-in
    earlier than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < hhmm_to_minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, att_day: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    seconds = int((punch_in - at_ist(att_day, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (exact Decimal arithmetic, no float rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, att_day: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes from shift_end to punch_out, only if at least 30. Overnight shifts end next day."""
    end_day = att_day + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_day
    minutes = int((punch_out - at_ist(end_day, shift_end)).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  att_day: date, emp: dict) -> dict:
    """All derived values for a record, following the table in DATA_MODEL.md section 3."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, att_day, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, att_day, emp["shift_start"], emp["shift_end"]),
        "half_day": hours < 4.5,  # R5 uses the ROUNDED work_hours
    }


# --------------------------------------------------------------------------- #
# Serialisation (database document -> API JSON)
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(created) if isinstance(created, datetime) else None,
    }


def serialize_record(doc: dict) -> dict:
    """Missing legacy fields are read as defaults; _id is never exposed."""
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    history = []
    for h in doc.get("history") or []:
        history.append({
            "at": to_api(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {k: {"from": to_api(v.get("from")), "to": to_api(v.get("to"))}
                        for k, v in (h.get("changes") or {}).items()},
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(pin) if pin else None,
        "punch_out": dt_to_ms(pout) if pout else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
# strict=True: floats ("1.7e12"), strings and booleans are rejected; the bounds reject seconds-looking values.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("name", "department")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v: str) -> str:
        if parse_ymd(v) is None:
            raise ValueError("must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(min_length=1)]
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(min_length=1)]
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    # Derived fields sent by a client are simply ignored (pydantic drops unknown keys).


# --------------------------------------------------------------------------- #
# 5. Endpoints: health, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    ensure_indexes()  # no-op once done; covers "MongoDB came up after the app"
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = ms_to_dt(now_ms())
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {} if department is None else {"department": department}
    total = employees.count_documents(q)  # the filtered total (R10)
    skip = page_skip(page, page_size)
    items = []
    if skip is not None:
        cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(floor_to_second(body.punched_at if body.punched_at is not None else now_ms()))
    att_day = attendance_date(ts, emp["shift_start"], emp["shift_end"])  # R1
    doc = {
        "emp_code": body.emp_code,
        "date": att_day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, att_day, emp["shift_start"]),  # R2
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date) index: exactly one of N parallel requests wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    out_dt = ms_to_dt(floor_to_second(body.punched_at if body.punched_at is not None else now_ms()))

    # latest record whose punch_in is at or before the punch-out (works across midnight)
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}},
                        sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    in_dt = rec["punch_in"]
    delta = out_dt - in_dt
    if delta <= timedelta(0):
        raise unprocessable("punched_at must be after the punch-in", ("body", "punched_at"))
    if delta > timedelta(hours=24):
        raise unprocessable("punched_at is more than 24 hours after the punch-in", ("body", "punched_at"))

    att_day = date.fromisoformat(rec["date"])
    derived = derive_fields(rec["status"], in_dt, out_dt, att_day, emp)
    # Atomic compare-and-set: only the request that still sees punch_out == null can win.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": in_dt},
        {"$set": {"punch_out": out_dt, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was punched out or changed concurrently")
    return serialize_record(updated)


def build_attendance_query(emp_code: Optional[str], date_from: Optional[str],
                           date_to: Optional[str], status: Optional[str]) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    start = require_date(date_from, ("query", "date_from")) if date_from is not None else None
    end = require_date(date_to, ("query", "date_to")) if date_to is not None else None
    if start and end and start > end:
        raise unprocessable("date_from must not be after date_to", ("query", "date_from"))
    if start or end:
        q["date"] = {}
        if start:
            q["date"]["$gte"] = start.isoformat()
        if end:
            q["date"]["$lte"] = end.isoformat()
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)  # reflects the filters (R10)
    skip = page_skip(page, page_size)
    items = []
    if skip is not None:  # the database sorts, skips and limits; nothing is loaded into memory
        cursor = logs.find(q, {"_id": 0}).sort(list(ATT_SORT.items())).skip(skip).limit(page_size)
        items = [serialize_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = require_date(date, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    times_supplied = body.punch_in is not None or body.punch_out is not None

    if new_status in PRESENCE:
        new_in = ms_to_dt(floor_to_second(body.punch_in)) if body.punch_in is not None else old_in
        new_out = ms_to_dt(floor_to_second(body.punch_out)) if body.punch_out is not None else old_out
        if new_in is None:
            raise unprocessable("a presence status requires a punch_in", ("body", "punch_in"))
        if body.punch_in is not None:
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:  # R1
                raise unprocessable("punch_in must fall on the record's attendance date", ("body", "punch_in"))
        if times_supplied and new_out is not None:
            if new_out <= new_in or new_out - new_in > timedelta(hours=24):
                raise unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))
    else:  # ABSENT / LEAVE clear both punch times
        if times_supplied:
            raise unprocessable("punch times cannot be supplied with status ABSENT or LEAVE", ("body", "status"))
        new_in = new_out = None

    if new_status == old_status and new_in == old_in and new_out == old_out:
        raise unprocessable("the request changes nothing", ("body",))

    derived = derive_fields(new_status, new_in, new_out, day, emp)
    old_values = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {k: {"from": old_values[k], "to": new_values[k]}
               for k in new_values if new_values[k] != old_values[k]}
    entry = {"at": ms_to_dt(now_ms()), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if nobody changed the record since we read it
    # (same status/times and the same number of history entries). Otherwise the loser gets 409.
    guard: dict = {"emp_code": emp_code, "date": day.isoformat(), "status": old_status,
                   "punch_in": old_in, "punch_out": old_out}
    if "history" in rec and isinstance(rec["history"], list):
        guard["history"] = {"$size": len(rec["history"])}
    else:
        guard["history"] = {"$exists": False}
    updated = logs.find_one_and_update(guard, {"$set": new_values, "$push": {"history": entry}},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics: aggregation pipelines + endpoints
#    Every pipeline is built by a function that BOTH the real endpoint and /admin/explain call,
#    so the explain output always describes exactly the query the endpoint runs.
#    Stored derived values (late_minutes, overtime_minutes, half_day, work_hours) are read, never recomputed.
# --------------------------------------------------------------------------- #
PRESENCE_LIST = list(PRESENCE)


def ref(prefix: str, field: str) -> str:
    """'$' + 'status' -> '$status' (document root);  '$$l.' + 'status' -> '$$l.status' (a $map variable)."""
    return f"{prefix}{field}"


def late_of(prefix: str) -> dict:
    return {"$ifNull": [ref(prefix, "late_minutes"), 0]}


def is_weekday_expr(prefix: str) -> dict:
    """True when the record's 'YYYY-MM-DD' date is Monday-Friday (R7). Bad strings -> null -> false."""
    dow = {"$isoDayOfWeek": {"$dateFromString": {"dateString": ref(prefix, "date"),
                                                   "format": "%Y-%m-%d", "onError": None}}}
    return {"$and": [{"$gte": [dow, 1]}, {"$lte": [dow, 5]}]}


def present_weight(prefix: str) -> dict:
    """1 for a presence-status record on Mon-Fri, 0.5 if it is a half day, otherwise 0 (R5, R6, R7)."""
    return {"$cond": [
        {"$and": [{"$in": [ref(prefix, "status"), PRESENCE_LIST]}, is_weekday_expr(prefix)]},
        {"$cond": [{"$ifNull": [ref(prefix, "half_day"), False]}, 0.5, 1]},
        0,
    ]}


def present_any_day(prefix: str) -> dict:
    """Like present_weight but for ANY day of the week: used by the daily trend (present_count)."""
    return {"$cond": [{"$in": [ref(prefix, "status"), PRESENCE_LIST]},
                      {"$cond": [{"$ifNull": [ref(prefix, "half_day"), False]}, 0.5, 1]}, 0]}


def count_working_days(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# ---- employee monthly ------------------------------------------------------ #
def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_weight("$")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late_of("$"), 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late_of("$")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = month_bounds(month)
    emp = get_employee_or_404(emp_code)

    joined = parse_ymd(emp.get("joined_on") or "") or start
    first_day = max(start, joined)  # R7: count working days from joined_on for mid-month joiners
    working_days = count_working_days(first_day, end) if first_day <= end else 0

    rows = list(logs.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    agg = rows[0] if rows else {}
    present = float(agg.get("present_days", 0))
    pct = half_up(Decimal(repr(present)) / Decimal(working_days) * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# ---- department summary ---------------------------------------------------- #
def department_summary_pipeline(start: date, end: date, department: Optional[str]) -> list:
    """Starts from `employees` (so people with no logs still count, R9) and joins each person's
    logs for the month; the work per employee is done with array operators, then grouped by department."""
    match: dict = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department

    def logs_where(cond: dict) -> dict:
        return {"$filter": {"input": "$logs", "as": "l", "cond": cond}}

    has_hours = {"$and": [{"$in": ["$$l.status", PRESENCE_LIST]}, {"$isNumber": "$$l.work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$project": {"_id": 0, "status": 1, "date": 1, "half_day": 1,
                              "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "_id": 0, "department": 1,
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": present_weight("$$l.")}}},
            "late_count": {"$size": logs_where({"$gt": [late_of("$$l."), 0]})},
            "total_late": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": late_of("$$l.")}}},
            "leave_count": {"$size": logs_where({"$eq": ["$$l.status", "LEAVE"]})},
            "on_duty_count": {"$size": logs_where({"$eq": ["$$l.status", "ON_DUTY"]})},
            "hours_sum": {"$sum": {"$map": {"input": logs_where(has_hours), "as": "l", "in": "$$l.work_hours"}}},
            "hours_n": {"$size": logs_where(has_hours)},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "hours_sum": {"$sum": "$hours_sum"},
            "hours_n": {"$sum": "$hours_n"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = month_bounds(month)
    items = []
    for r in employees.aggregate(department_summary_pipeline(start, end, department)):
        # average over RECORDS (sum of hours / number of records), rounded half-up once at the end
        avg = half_up(r["hours_sum"] / r["hours_n"], 2) if r["hours_n"] else None
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


# ---- late leaderboard ------------------------------------------------------ #
def late_leaderboard_pipeline(start: date, end: date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4). It is computed on the total only, so
        # tied employees share a rank. limit is applied to the RANK, after ranking.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = month_bounds(month)
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
              "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
              "late_count": int(r["late_count"])}
             for r in logs.aggregate(late_leaderboard_pipeline(start, end, limit, department))]
    return {"month": month, "items": items}


# ---- department trend ------------------------------------------------------ #
def department_codes(department: str) -> list:
    return [d["emp_code"] for d in employees.find({"department": department}, {"_id": 0, "emp_code": 1})]


def department_trend_pipeline(department: str, codes: list, d_from: date, d_to: date) -> list:
    """One row per calendar day. Days are produced inside the database ($range), merged with the real
    per-day totals by a $group, headcount is counted per day with $lookup, and the 7-day moving average
    is a $setWindowFields over the 7 most recent rows of the requested range."""
    n_days = (d_to - d_from).days + 1
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    calendar_rows = [  # one zero-valued row per day, so empty days and weekends exist
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "days": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {
                "$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$i"}}}}}}}},
        {"$unwind": "$days"},
        {"$project": {"_id": "$days", "present": {"$literal": 0}, "late": {"$literal": 0}}},
    ]
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {"_id": "$date",
                    "present": {"$sum": present_any_day("$")},  # weekends count here (unlike present_days)
                    "late": {"$sum": {"$cond": [{"$gt": [late_of("$"), 0]}, 1, 0]}}}},
        {"$unionWith": {"coll": "employees", "pipeline": calendar_rows}},
        {"$group": {"_id": "$_id", "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
        {"$lookup": {"from": "employees", "let": {"d": "$_id"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$sort": {"_id": 1}},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "working": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}, 5]},
        }},
        {"$set": {"rate": {"$cond": [{"$and": ["$working", {"$gt": ["$headcount", 0]}]},
                                     {"$divide": ["$present", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"_id": 1},
                              "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$_id", "headcount": 1, "present": 1, "late": 1,
                      "working": 1, "ma": 1}},
    ]


def parse_trend_range(date_from: str, date_to: str) -> tuple[date, date]:
    d_from = require_date(date_from, ("query", "from"))
    d_to = require_date(date_to, ("query", "to"))
    if d_to < d_from:
        raise unprocessable("to must not be before from", ("query", "to"))
    if (d_to - d_from).days + 1 > 92:
        raise unprocessable("the range must not exceed 92 days", ("query", "to"))
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = parse_trend_range(from_, to)
    codes = department_codes(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    items = []
    for r in logs.aggregate(department_trend_pipeline(department, codes, d_from, d_to)):
        present, headcount, working = float(r["present"]), int(r["headcount"]), bool(r["working"])
        # rate from exact numbers (present is a multiple of 0.5), so ties round half-up correctly
        rate = half_up(Decimal(repr(present)) / Decimal(headcount), 4) if working and headcount > 0 else None
        items.append({
            "date": r["date"], "is_working_day": working, "headcount": headcount,
            "present_count": present, "late_count": int(r["late"]),
            "attendance_rate": rate, "moving_avg_7d": half_up(r.get("ma"), 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def run_explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    # BSON types (dates, Int64, ...) -> plain JSON
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def need(value: Any, name: str) -> Any:
    if value is None:
        raise unprocessable(f"query parameter '{name}' is required for this endpoint", ("query", name))
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        command = {"find": logs.name, "filter": q, "sort": ATT_SORT,
                   "skip": page_skip(page, page_size) or 0, "limit": page_size}
        collection = logs.name
    elif endpoint == "employee_monthly":
        start, end = month_bounds(need(month, "month"))
        pipeline = employee_monthly_pipeline(need(emp_code, "emp_code"), start, end)
        command, collection = {"aggregate": logs.name, "pipeline": pipeline, "cursor": {}}, logs.name
    elif endpoint == "department_summary":
        start, end = month_bounds(need(month, "month"))
        pipeline = department_summary_pipeline(start, end, department)
        command, collection = {"aggregate": employees.name, "pipeline": pipeline, "cursor": {}}, employees.name
    elif endpoint == "late_leaderboard":
        start, end = month_bounds(need(month, "month"))
        pipeline = late_leaderboard_pipeline(start, end, limit, department)
        command, collection = {"aggregate": logs.name, "pipeline": pipeline, "cursor": {}}, logs.name
    else:  # department_trend
        d_from, d_to = parse_trend_range(need(from_, "from"), need(to, "to"))
        dep = need(department, "department")
        pipeline = department_trend_pipeline(dep, department_codes(dep), d_from, d_to)
        command, collection = {"aggregate": logs.name, "pipeline": pipeline, "cursor": {}}, logs.name

    try:
        explain = run_explain(command)
    except PyMongoError as exc:
        raise HTTPException(503, f"explain failed: {exc}")
    return JSONResponse({"endpoint": endpoint, "collection": collection, "explain": explain})
