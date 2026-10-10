# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | MongoClient setup | No `tz_aware=True`, so dates come back without a time zone and IST math silently uses the server's local zone | Punch-in on a non-IST server gets the wrong `date` / `late_minutes` | Added `tz_aware=True`; all instants handled as UTC and converted to IST only for rules |
| 2 | Startup | No indexes created at all | Duplicate emp_code / punch-ins possible; explain shows COLLSCAN | `create_indexes()` in the lifespan hook, idempotent |
| 3 | `/health` | Always returns ok, never talks to MongoDB | Stop MongoDB: still 200 | `ping` command; 503 on failure |
| 4 | `compute_late_minutes` | Checks `minutes > 10` on whole minutes, so 09:40:01 -> 0 instead of 10 | Spec example 09:40:01 | Compare the exact timedelta `> 10 min`, then floor minutes |
| 5 | `compute_late_minutes` | Shift start built with `replace()` on a UTC/naive time, not IST; overnight shifts ignored | 09:28 IST punch shows as late | Build shift start from the attendance date in IST |
| 6 | `compute_work_hours` | Python `round()` is float/banker's rounding, not half-up | 54 seconds = 0.015 h -> 0.01 instead of 0.02 | `Decimal` + `ROUND_HALF_UP` |
| 7 | `compute_overtime` | No 30-minute minimum; overnight shift end placed on the same day; `int()` truncates instead of floor | 18:35 out gives 5 instead of 0; overnight 06:40 out gives a wrong value | Minimum 30 rule, shift end +1 day when overnight, floor division |
| 8 | Models | No validation (emp_code pattern, email, HH:MM, joined_on date, shift_start != shift_end, status values) | `{"emp_code":"x","shift_start":"99:99"}` returns 201 | Pydantic Field patterns / Literal / validator -> 422 |
| 9 | `PunchInIn.punched_at` | Accepts seconds, floats and numeric strings | `punched_at: 1783570201` (seconds) accepted | `StrictInt` with range 1e11..4102444800000 |
| 10 | `create_employee` | Check-then-insert race; `created_at` naive local time and returned as datetime | Two parallel creates both get 201 | Unique index + catch `DuplicateKeyError` -> 409; UTC time, returned as epoch ms |
| 11 | `list_employees` | `skip = page * page_size` skips the first page; `total` ignores the filter; no sort; no page limits | `page=1` misses first items; `department=Sales` total = 6 | `(page-1)*page_size`, filtered count, sort by emp_code, `page>=1`, `page_size<=100` |
| 12 | `punch_in` | Unknown employee crashes (500) | `emp_code: EMP9999` | 404 |
| 13 | `punch_in` | `fromtimestamp` uses server time zone; no R1 overnight rule; no truncation to seconds | Overnight 00:30 punch gets the wrong date | `ms_to_dt` (UTC, truncated) + `attendance_date()` |
| 14 | `punch_in` | Check-then-insert race | Two parallel punch-ins both 201 | Unique (emp_code, date) index + `DuplicateKeyError` -> 409 |
| 15 | `punch_in` / `list_attendance` | Returns `id` (from `_id`) and datetimes instead of epoch ms; missing `history`/`half_day` not defaulted | Response contains `id`; legacy record has no `half_day` | `record_out()` formatter |
| 16 | `list_attendance` | Loads the whole collection into memory and sorts in Python; no `emp_code` tiebreak | Slow on 100k docs | DB sort `date desc, emp_code asc` + skip/limit on an index |
| 17 | `list_attendance` | No 422 when `date_from > date_to`; status not validated; no page limits | `date_from=2026-07-10&date_to=2026-07-01` returns 200 | Explicit checks, `Literal` status, Query limits |

**Looked at and not a defect:** `load_dotenv()` does not override real environment variables by default, which is exactly what the spec requires.
