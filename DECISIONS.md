# DECISIONS.md

1. **Indexes.** `employees.emp_code` (unique) for creates and lookups; `(department, emp_code)` for the employee list; `joined_on` and `(department, joined_on)` for headcounts. `attendance_logs (emp_code, date)` (unique) for punch-in safety, PATCH, monthly summary and the `$lookup`s; `(date desc, emp_code)` for the list sort and leaderboard date range; `(status, date, emp_code)` for status filters; `(emp_code, punch_in desc)` for punch-out. I rejected an index on `late_minutes`: a month of data is already narrowed by date, and it would slow every write.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets MongoDB accept the first; the second raises `DuplicateKeyError`, which I return as 409. There is no check-then-insert.

3. **Ties.** `$rank` gives tied employees the same rank (1, 2, 2, 4). I filter `rank <= limit` after ranking, so both tied employees are returned, even if that is more than `limit`, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and joins logs with `$unwind preserveNullAndEmptyArrays`, so employees without logs still count.

5. **100x data.** Pre-aggregate daily per-department counts into a summary collection (`$merge`) so analytics read small tables.
