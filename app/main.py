"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient, ReturnDocument
import json
from bson import Decimal128, json_util
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False by default -> real env vars take priority

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                  # BSON dates come back as aware UTC datetimes
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Indexes + app startup
# --------------------------------------------------------------------------- #
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])


@asynccontextmanager
async def lifespan(app):
    create_indexes()  # idempotent: does nothing if the index already exists
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: conversions
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC datetime -> epoch milliseconds, exact integer math."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def round_half_up(value, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Helpers: shifts and business rules R1-R5
# --------------------------------------------------------------------------- #
def parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of punch_in; early-morning punch-in on an overnight shift
    belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), parse_hhmm(shift_start), IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(date.fromisoformat(date_str), parse_hhmm(shift_end), IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start."""
    delta = punch_in - shift_start_dt(date_str, shift_start)
    if delta > timedelta(minutes=10):
        return delta // timedelta(minutes=1)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if >= 30."""
    delta = punch_out - shift_end_dt(date_str, shift_start, shift_end)
    minutes = delta // timedelta(minutes=1)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: based on the ROUNDED work_hours."""
    return work_hours < 4.5


# --------------------------------------------------------------------------- #
# Output formatting (never expose _id; instants as epoch ms; fill defaults)
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department",
                   "shift_start", "shift_end", "joined_on")


def employee_out(doc: dict) -> dict:
    out = {k: doc.get(k) for k in EMPLOYEE_FIELDS}
    out["created_at"] = dt_to_ms(doc.get("created_at"))
    return out


def history_out(h: dict) -> dict:
    changes = {}
    for field, ch in (h.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": dt_to_ms(ch.get("from")), "to": dt_to_ms(ch.get("to"))}
        else:
            changes[field] = ch
    return {"at": dt_to_ms(h.get("at")), "by": h.get("by"),
            "reason": h.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models (validation -> automatic 422)
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: date

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None   # StrictInt: rejects floats and strings
    status: PresenceStatus = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)          # unique index decides, not a find_one
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cur = (db.employees.find(q).sort("emp_code", 1)
           .skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(d) for d in cur],
            "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)    # unique (emp_code, date) -> race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def build_attendance_list(emp_code, date_from, date_to, status, page, page_size):
    """Shared by GET /attendance and the explain endpoint (same filter/sort/paging)."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    sort = [("date", -1), ("emp_code", 1)]
    return q, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort, skip, limit = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    total = (db.attendance_logs.count_documents(q) if q
             else db.attendance_logs.estimated_document_count())
    cur = db.attendance_logs.find(q).sort(sort).skip(skip).limit(limit)
    return {"items": [record_out(d) for d in cur],
            "total": total, "page": page, "page_size": page_size}



# --------------------------------------------------------------------------- #
# Part B: punch-out
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in is at or before ts (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    wh = compute_work_hours(rec["punch_in"], ts)
    ot = compute_overtime(ts, rec["date"], emp["shift_start"], emp["shift_end"])

    # atomic: matches only while punch_out is still empty -> parallel calls give one 200
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh,
                  "overtime_minutes": ot, "half_day": compute_half_day(wh)}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Part B: regularize (manual correction) with audit history
# --------------------------------------------------------------------------- #
class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields (work_hours etc.) are not declared, so they are ignored


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: date, body: RegularizeIn):
    day = date.isoformat()
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    new_status = body.status or rec["status"]
    off_status = new_status in ("ABSENT", "LEAVE")
    if off_status and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")

    if off_status:
        new_in = new_out = None
        late, wh, ot, hd = 0, None, 0, False
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        late = compute_late_minutes(new_in, day, emp["shift_start"])
        if new_out is None:
            wh, ot, hd = None, 0, False
        else:
            if new_out <= new_in or new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            wh = compute_work_hours(new_in, new_out)
            ot = compute_overtime(new_out, day, emp["shift_start"], emp["shift_end"])
            hd = compute_half_day(wh)

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": hd}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    # optimistic lock: only update if nobody appended to history since we read it
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"],
         "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n]}},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, retry")
    return record_out(updated)



# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_range(month: str):
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def count_weekdays(start: date, end: date) -> int:
    """R7: Mon-Fri between start and end inclusive (pure calendar math)."""
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    n = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            n += 1
    return n


def to_dec(v) -> Decimal:
    if isinstance(v, Decimal128):
        return v.to_decimal()
    return Decimal(str(v or 0))


def rhu(v, places=2):
    """R8 rounding for reported numbers (None stays None)."""
    return None if v is None else round_half_up(to_dec(v), places)


# ---- aggregation expressions (p = field prefix, e.g. "logs.") ----
def present_value(p=""):
    # R5/R6: presence status = 1, half day = 0.5, otherwise 0
    return {"$cond": [{"$in": [f"${p}status", PRESENCE]},
                      {"$cond": [{"$eq": [{"$ifNull": [f"${p}half_day", False]}, True]}, 0.5, 1]},
                      0]}


def is_weekday(field):
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": field}}}, 5]}


def weekday_present(p=""):
    # R7: present_days only counts Mon-Fri records
    return {"$cond": [{"$and": [{"$in": [f"${p}status", PRESENCE]}, is_weekday(f"${p}date")]},
                      present_value(p), 0]}


def late_flag(p=""):
    return {"$cond": [{"$gt": [{"$ifNull": [f"${p}late_minutes", 0]}, 0]}, 1, 0]}


def status_flag(p, s):
    return {"$cond": [{"$eq": [f"${p}status", s]}, 1, 0]}


def has_work_hours(p=""):
    return {"$and": [{"$in": [f"${p}status", PRESENCE]},
                     {"$ne": [{"$ifNull": [f"${p}work_hours", None]}, None]}]}


# --------------------------------------------------------------------------- #
# Pipeline builders (shared by the endpoints AND the explain endpoint)
# --------------------------------------------------------------------------- #
def build_employee_monthly(emp_code, first, last):
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code,
                    "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": weekday_present()},
            "leave_days": {"$sum": status_flag("", "LEAVE")},
            "late_count": {"$sum": late_flag()},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary(first, last, department):
    # Start from EMPLOYEES so people with zero logs are still counted (R9).
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    L = "logs."
    return "employees", [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}],
            "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": weekday_present(L)},
            "wh_sum": {"$sum": {"$cond": [has_work_hours(L), {"$toDecimal": "$logs.work_hours"}, 0]}},
            "wh_n": {"$sum": {"$cond": [has_work_hours(L), 1, 0]}},
            "late_count": {"$sum": late_flag(L)},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": status_flag(L, "LEAVE")},
            "on_duty_count": {"$sum": status_flag(L, "ON_DUTY")},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard(first, last, department, limit):
    pipe = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                      # drops logs of unknown employees
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})   # rank inside dept
    pipe += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},   # limit AFTER ranking, keeps ties
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipe


def check_trend_range(frm, to):
    if to < frm:
        raise HTTPException(422, "'to' must be on or after 'from'")
    if (to - frm).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")


def build_department_trend(department, frm, to):
    f_s, t_s = frm.isoformat(), to.isoformat()
    lo = datetime(frm.year, frm.month, frm.day, tzinfo=timezone.utc)
    hi = datetime(to.year, to.month, to.day, tzinfo=timezone.utc) + timedelta(days=1)
    # rate = present / headcount, 4 decimals half-up, done in Decimal128 (exact)
    rate = {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [
            {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]},
            10000]}, 0.5]}},
        10000]}}
    return "employees", [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": f_s, "$lte": t_s}}}],
            "as": "logs"}},
        {"$unwind": "$logs"},
        {"$replaceRoot": {"newRoot": "$logs"}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": present_value()},   # any day, incl. weekends
                    "late_count": {"$sum": late_flag()}}},
        # guarantee at least one document so $densify works even with zero logs
        {"$unionWith": {"pipeline": [{"$documents": [
            {"_id": f_s, "present_count": 0, "late_count": 0}]}]}},
        {"$group": {"_id": "$_id",
                    "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        # gap filling: one row per calendar day from..to
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]},
                  "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                  "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        # R9 headcount for that day
        {"$lookup": {"from": "employees", "let": {"d": "$date"},
                     "pipeline": [{"$match": {"department": department,
                                              "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                                  {"$count": "n"}],
                     "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, rate, None]}}},
        # 7-day window = this row + up to 6 previous rows inside the range; $avg skips nulls
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1,
                      "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_range(month)
    coll, pipe = build_employee_monthly(emp_code, first, last)
    rows = list(db[coll].aggregate(pipe))
    r = rows[0] if rows else {}
    joined = date.fromisoformat(emp["joined_on"])
    working = count_weekdays(max(first, joined), last)
    present = to_dec(r.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": rhu(present),
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": rhu(present * 100 / working) if working else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_range(month)
    coll, pipe = build_department_summary(first, last, department)
    items = []
    for r in db[coll].aggregate(pipe):
        items.append({
            "department": r["_id"],
            "headcount": len(r["emps"]),
            "present_days": rhu(r["present_days"]),
            "avg_work_hours": rhu(to_dec(r["wh_sum"]) / r["wh_n"]) if r["wh_n"] else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_range(month)
    coll, pipe = build_leaderboard(first, last, department, limit)
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
              "department": r["department"],
              "total_late_minutes": int(r["total_late_minutes"]),
              "late_count": int(r["late_count"])}
             for r in db[coll].aggregate(pipe)]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: date = Query(alias="from"), to: date = Query()):
    check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    coll, pipe = build_department_trend(department, from_, to)
    items = [{"date": r["date"],
              "is_working_day": r["is_working_day"],
              "headcount": int(r["headcount"]),
              "present_count": rhu(r["present_count"]),
              "late_count": int(r["late_count"]),
              "attendance_rate": r.get("attendance_rate"),
              "moving_avg_7d": rhu(r.get("moving_avg_7d"), 4)}
             for r in db[coll].aggregate(pipe)]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain endpoint (Part C)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**params):
        missing = [k for k, v in params.items() if v is None]
        if missing:
            raise HTTPException(422, "missing required parameter(s): " + ", ".join(missing))

    if endpoint == "attendance_list":
        q, sort, skip, lim = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            coll, pipe = build_employee_monthly(emp_code, *month_range(month))
        elif endpoint == "department_summary":
            need(month=month)
            coll, pipe = build_department_summary(*month_range(month), department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            coll, pipe = build_leaderboard(*month_range(month), department, limit)
        else:
            need(department=department, to=to, **{"from": from_})
            check_trend_range(from_, to)
            coll, pipe = build_department_trend(department, from_, to)
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(raw))}
