# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` and unique `attendance_logs(emp_code, date)` give identity and one record per day, and serve PATCH and the monthly report. `(date desc, emp_code)` serves the sorted list and month-wide analytics; `(status, date, emp_code)` serves status filters; `(emp_code, punch_in desc)` finds the record to punch out; `employees.joined_on` serves headcount. I rejected an index on `late_minutes`: the date range already narrows the leaderboard, and it would slow every write.

2. **Punch-in race.** Both requests find the employee and compute the same IST date, then both call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept only one; the other raises `DuplicateKeyError`, which I return as 409. I never check first.

3. **Ties.** `$rank` on total late minutes gives competition ranking (1, 2, 2, 4). I filter `rank <= limit` afterwards, so everyone tied at the cutoff is returned, even if that exceeds `limit` rows.

4. **Headcount.** The pipeline starts from `employees` who joined on or before month end and joins each person's logs. People with no logs still produce a row, and I count employees, not logs.

5. **100x data.** I would keep a small daily summary per department, updated on each punch and correction, so reports read a few documents instead of scanning logs.
