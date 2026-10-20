# LA Pay Tennis Court Monitor

Checks LA Rec & Parks Pay Tennis availability and builds an easy-to-read report (`report.html`):
one card per location, date buttons to focus on a single day, and highlighted "priority" times
(Mon-Thu evenings, weekend mornings). It only **looks** for open slots and alerts you. It never
books or pays for anything, so you still book on the official site.

Requires a Mac, Python 3, and your own free LA Rec & Parks account.
**New to Terminal? Open START-HERE.pdf for a one-page, step-by-step guide.**

## Setup (about 3 minutes)
1. Unzip, open Terminal, and `cd` into the `tennis-monitor` folder.
2. Run `./setup.sh`. It installs everything, opens a browser so you can **log in yourself**, and
   schedules a daily 8:00 AM run.
3. Edit `config.yaml` to pick your locations, days, and time windows.
4. Try it now: `.venv/bin/python monitor.py && open report.html`

## Using it
- Run it any time with the command in step 4. New slots since the last run get a gold outline.
- Click a date button at the top of the report to see just that day.
- If you see "Session expired", run `.venv/bin/python monitor.py --login` again.
- If the site returns 403 errors, set `headless: false` in `config.yaml`.
- Stop the daily run: `launchctl bootout gui/$(id -u)/com.user.tennismonitor`

## Privacy
Your login is saved only on your computer in `session.json`. Never share that file, or the
`debug/` folder. Everyone who uses this needs to run `--login` with their own account.

## Notes
- Keep checks infrequent (daily is plenty) and follow the site's terms of use. The site limits
  reservations to one court per household.
- This is an unofficial tool. If the website changes, it may stop working.
