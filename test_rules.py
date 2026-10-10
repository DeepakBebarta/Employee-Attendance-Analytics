from datetime import datetime, timezone
from app.main import (IST, attendance_date, compute_late_minutes,
                      compute_work_hours, compute_overtime)

def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)

# R2 examples from the spec (shift 09:30)
assert compute_late_minutes(ist(2026,7,6,9,40,0),  "2026-07-06", "09:30") == 0
assert compute_late_minutes(ist(2026,7,6,9,40,1),  "2026-07-06", "09:30") == 10
assert compute_late_minutes(ist(2026,7,6,10,15,59),"2026-07-06", "09:30") == 45

# R4 half-up: 54 seconds = 0.015 h -> must be 0.02
a = ist(2026,7,6,9,0,0)
assert compute_work_hours(a, a.replace(second=54)) == 0.02

# Sample doc 7: overnight 22:00-06:00, in 21:55 on 6th, out 06:40 on 7th
pin, pout = ist(2026,7,6,21,55), ist(2026,7,7,6,40)
assert attendance_date(pin, "22:00", "06:00") == "2026-07-06"
assert compute_work_hours(pin, pout) == 8.75
assert compute_overtime(pout, "2026-07-06", "22:00", "06:00") == 40

# Overnight: punch-in at 00:30 on the 7th belongs to the 6th
assert attendance_date(ist(2026,7,7,0,30), "22:00", "06:00") == "2026-07-06"

# R3: 5 min overtime is under 30 -> 0
assert compute_overtime(ist(2026,7,6,18,35), "2026-07-06", "09:30", "18:30") == 0

print("all rule tests passed")
