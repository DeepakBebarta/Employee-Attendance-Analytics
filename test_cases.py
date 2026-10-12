import json, os, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db = MongoClient(os.getenv("MONGO_URI"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]
B = "http://localhost:8001"
IST = timezone(timedelta(hours=5, minutes=30))
fails = 0

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, method=method, data=data,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def check(name, got, exp):
    global fails
    ok = got == exp
    if not ok:
        fails += 1
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"\n       got={got!r}\n  expected={exp!r}"))

# ---------------- build the nasty dataset (August 2026) ----------------
codes = [f"EMP700{i}" for i in range(1, 8)] + ["EMP7999"]
db.employees.delete_many({"emp_code": {"$in": codes}})
db.attendance_logs.delete_many({"emp_code": {"$in": codes}})

def emp(code, dept, joined, s="09:30", e="18:30"):
    return {"emp_code": code, "name": "Test " + code, "email": code.lower() + "@t.com",
            "department": dept, "shift_start": s, "shift_end": e, "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}

db.employees.insert_many([
    emp("EMP7001", "Ops", "2026-01-01"),
    emp("EMP7002", "Ops", "2026-01-01"),
    emp("EMP7003", "Ops", "2026-01-01"),
    emp("EMP7004", "Ops", "2026-01-01"),          # no logs at all
    emp("EMP7005", "Ops", "2026-08-17"),          # mid-month joiner
    emp("EMP7006", "Ops", "2026-09-10"),          # joins after August
    emp("EMP7007", "Lab", "2026-01-01", "22:00", "06:00"),   # overnight shift
])

def log(code, d, status, late=0, wh=None, half=False):
    pin = pout = None
    if status in ("PRESENT", "WFH", "ON_DUTY"):
        pin = datetime.fromisoformat(d + "T09:30:00").replace(tzinfo=IST).astimezone(timezone.utc)
        if wh is not None:
            pout = pin + timedelta(hours=wh)
    return {"emp_code": code, "date": d, "status": status, "punch_in": pin, "punch_out": pout,
            "work_hours": wh, "late_minutes": late, "overtime_minutes": 0,
            "half_day": half, "history": []}

db.attendance_logs.insert_many([
    log("EMP7001", "2026-08-03", "PRESENT", late=20, wh=8.5),
    log("EMP7001", "2026-08-04", "PRESENT", wh=4.0, half=True),   # half day
    log("EMP7001", "2026-08-08", "PRESENT", late=15, wh=5.0),     # Saturday
    log("EMP7002", "2026-08-03", "PRESENT", late=35, wh=9.0),
    log("EMP7002", "2026-08-05", "LEAVE"),
    log("EMP7003", "2026-08-03", "ON_DUTY", wh=8.0),
    log("EMP7003", "2026-08-04", "WFH", late=35),                 # open record
    log("EMP7003", "2026-08-05", "ABSENT"),
    log("EMP7005", "2026-08-17", "PRESENT", late=12, wh=9.0),
    log("EMP7007", "2026-08-03", "PRESENT", wh=8.75),
    log("EMP7999", "2026-08-03", "PRESENT", late=100, wh=8.0),    # employee does not exist
])

# ---------------- employee monthly ----------------
def monthly(code):
    s, r = req("GET", f"/analytics/employees/{code}/monthly?month=2026-08")
    return (r["working_days"], r["present_days"], r["late_count"],
            r["total_late_minutes"], r["attendance_pct"])

check("monthly EMP7001 (half day + weekend)", monthly("EMP7001"), (21, 1.5, 2, 35, 7.14))
check("monthly EMP7005 (mid-month joiner)",   monthly("EMP7005"), (11, 1.0, 1, 12, 9.09))
check("monthly EMP7004 (no logs)",            monthly("EMP7004"), (21, 0.0, 0, 0, 0.0))
check("monthly EMP7006 (joins next month)",   monthly("EMP7006"), (0, 0.0, 0, 0, None))
check("monthly unknown employee -> 404",
      req("GET", "/analytics/employees/EMP7998/monthly?month=2026-08")[0], 404)

# ---------------- department summary ----------------
s, r = req("GET", "/analytics/departments/summary?month=2026-08")
by = {i["department"]: (i["headcount"], i["present_days"], i["avg_work_hours"], i["late_count"],
                        i["total_late_minutes"], i["leave_count"], i["on_duty_count"])
      for i in r["items"]}
check("summary Ops", by.get("Ops"), (5, 5.5, 7.25, 5, 117, 1, 1))
check("summary Lab", by.get("Lab"), (1, 1.0, 8.75, 0, 0, 0, 0))
check("summary Engineering (no August logs)", by.get("Engineering"), (2, 0.0, None, 0, 0, 0, 0))
s, r = req("GET", "/analytics/departments/summary?month=2026-08&department=Lab")
check("summary department filter", [i["department"] for i in r["items"]], ["Lab"])

# ---------------- leaderboard (3-way tie) ----------------
def board(q):
    s, r = req("GET", "/analytics/leaderboard/late?month=2026-08" + q)
    return [(i["rank"], i["emp_code"], i["total_late_minutes"], i["late_count"]) for i in r["items"]]

tie = [(1, "EMP7001", 35, 2), (1, "EMP7002", 35, 1), (1, "EMP7003", 35, 1)]
check("leaderboard limit=1 keeps all tied", board("&limit=1"), tie)
check("leaderboard limit=4 skips to rank 4", board("&limit=4"), tie + [(4, "EMP7005", 12, 1)])
check("leaderboard department=Lab is empty", board("&department=Lab"), [])

# ---------------- trend with gaps + weekends ----------------
s, r = req("GET", "/analytics/departments/Ops/trend?from=2026-08-01&to=2026-08-09")
got = [(i["date"], i["present_count"], i["late_count"], i["attendance_rate"], i["moving_avg_7d"])
       for i in r["items"]]
check("trend Ops rows", got, [
    ("2026-08-01", 0, 0, None, None),
    ("2026-08-02", 0, 0, None, None),
    ("2026-08-03", 3, 2, 0.75, 0.75),
    ("2026-08-04", 1.5, 1, 0.375, 0.5625),
    ("2026-08-05", 0, 0, 0.0, 0.375),
    ("2026-08-06", 0, 0, 0.0, 0.2813),
    ("2026-08-07", 0, 0, 0.0, 0.225),
    ("2026-08-08", 1, 1, None, 0.225),
    ("2026-08-09", 0, 0, None, 0.225),
])
check("trend headcount 4 every day", {i["headcount"] for i in r["items"]}, {4})
check("trend to<from -> 422", req("GET", "/analytics/departments/Ops/trend?from=2026-08-09&to=2026-08-01")[0], 422)
check("trend 93 days -> 422", req("GET", "/analytics/departments/Ops/trend?from=2026-08-01&to=2026-11-01")[0], 422)
check("trend unknown department -> 404", req("GET", "/analytics/departments/Nope/trend?from=2026-08-01&to=2026-08-09")[0], 404)

# ---------------- overnight punch via the API ----------------
s, r = req("POST", "/attendance/punch-in", {"emp_code": "EMP7007", "punched_at": 1786388400000})  # 00:30 IST 11 Aug
check("overnight punch-in -> previous day, late 150", (s, r.get("date"), r.get("late_minutes")), (201, "2026-08-10", 150))
s, r = req("POST", "/attendance/punch-out", {"emp_code": "EMP7007", "punched_at": 1786410900000})  # 06:45 IST 11 Aug
check("overnight punch-out 6.25h, overtime 45", (s, r.get("work_hours"), r.get("overtime_minutes"), r.get("half_day")), (200, 6.25, 45, False))

# ---------------- validation ----------------
check("punched_at float -> 422", req("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "punched_at": 1786388400000.0})[0], 422)
check("punched_at string -> 422", req("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "punched_at": "1786388400000"})[0], 422)
check("shift_start == shift_end -> 422", req("POST", "/employees", {"emp_code": "EMP7100", "name": "X", "email": "x@t.com", "department": "Ops", "joined_on": "2026-01-01", "shift_start": "09:00", "shift_end": "09:00"})[0], 422)
check("date_from > date_to -> 422", req("GET", "/attendance?date_from=2026-08-10&date_to=2026-08-01")[0], 422)
check("page_size 101 -> 422", req("GET", "/attendance?page_size=101")[0], 422)

db.employees.delete_many({"emp_code": {"$in": codes}})
db.attendance_logs.delete_many({"emp_code": {"$in": codes}})
print(f"\n{fails} failure(s)")
