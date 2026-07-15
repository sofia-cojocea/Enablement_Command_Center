import urllib.request
import urllib.error
import json
import time
from datetime import datetime, timezone
import sys, os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import config
    API_KEY = config.LITMOS_API_KEY
except ImportError:
    API_KEY = None

API_KEY = os.environ.get('LITMOS_API_KEY') or API_KEY

if not API_KEY:
    print("ERROR: No API key found. Set LITMOS_API_KEY env var or provide config.py.")
    sys.exit(1)

BASE           = "https://api.litmos.com/v1.svc"
HEADERS        = {"apikey": API_KEY, "Accept": "application/json"}
OUTPUT_FILE    = "index.html"
COMPLETED_FILE = "completed.json"

PATHS = [
    ("4nP0bnJjL0M1", "SuccessKPI Bootcamp Experience", 10),
    ("7WKO7vtviOg1", "Generative AI Capabilities",     10),
]

COHORT_DAYS = 10  # expected pace based on this window

if os.path.exists(COMPLETED_FILE):
    with open(COMPLETED_FILE) as f:
        completed_store = json.load(f)
else:
    completed_store = {}

def get(endpoint):
    results, start, limit = [], 0, 100
    while True:
        url = f"{BASE}/{endpoint}?source=successkpi&limit={limit}&start={start}&format=json"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req) as r:
                batch = json.loads(r.read().decode())
                if not batch: break
                results += batch
                if len(batch) < limit: break
                start += limit
        except urllib.error.HTTPError as e:
            if e.code == 503:
                print("  Rate limited — waiting 10s...")
                time.sleep(10); continue
            break
    return results

def parse_date(s):
    if not s: return None
    try:
        if '/Date(' in s:
            ms = int(s.replace('/Date(','').split('+')[0].split('-')[0])
            return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')
    except: pass
    return None

def days_since(date_str):
    if not date_str: return 9999
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - dt).days
    except: return 9999

def login_label(days):
    if days == 0:   return "Today"
    if days == 1:   return "Yesterday"
    if days < 9999: return f"{days} days ago"
    return "Never"

# ── Pull all path metadata ─────────────────────────────────────────────────
print("Fetching course lists...")
path_meta = {}
for path_id, path_name, cohort_days in PATHS:
    courses      = get(f"learningpaths/{path_id}/courses")
    course_ids   = {c['Id'] for c in courses}
    course_names = {c['Id']: c['Name'] for c in courses}
    users        = get(f"learningpaths/{path_id}/users")
    path_meta[path_id] = {
        "name": path_name, "cohort_days": cohort_days,
        "course_ids": course_ids, "course_names": course_names,
        "users": {u["Id"]: u for u in users}
    }
    print(f"  {path_name}: {len(courses)} courses, {len(users)} users")

# Total courses across all paths combined
all_course_ids   = {}
all_course_names = {}
for meta in path_meta.values():
    all_course_ids.update({cid: True for cid in meta["course_ids"]})
    all_course_names.update(meta["course_names"])
TOTAL_COURSES = len(all_course_ids)
print(f"\nTotal courses across all paths: {TOTAL_COURSES}")

# Build unified user list
all_user_ids = {}
for meta in path_meta.values():
    for uid, u in meta["users"].items():
        if uid not in all_user_ids:
            all_user_ids[uid] = u

print(f"Total unique users: {len(all_user_ids)}")
print("Fetching per-user course progress...")

all_learners = []
for i, (uid, u) in enumerate(all_user_ids.items()):
    name = f"{u['FirstName']} {u['LastName']}".strip()
    username = u.get('UserName', '').strip().lower()
    if not username.endswith('@successkpi.com'):
        print(f"  Skipping {name} — not a successkpi.com account")
        continue

    # Check if user is active in Litmos
    url = f"{BASE}/users/{uid}?source=successkpi&format=json"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as r:
            user_record = json.loads(r.read().decode())
    except:
        user_record = {}
    time.sleep(0.25)
    if not isinstance(user_record, dict) or not user_record.get('Active', True):
        print(f"  Skipping {name} — inactive or unreadable")
        continue

    print(f"  [{i+1}/{len(all_user_ids)}] {name}")

    user_courses = get(f"users/{uid}/courses")
    time.sleep(0.25)

    # All courses across both paths for this user
    bc = [c for c in user_courses if c['Id'] in all_course_ids]

    # Lock individual courses — once Complete, always Complete
    for c in bc:
        course_key = f"{uid}:{c['Id']}"
        if c.get('Complete'):
            completed_store[course_key] = True
        elif completed_store.get(course_key):
            c['Complete'] = True

    completed_courses = [c for c in bc if c.get('Complete')]
    progress_pct      = round(len(completed_courses) / TOTAL_COURSES * 100) if TOTAL_COURSES else 0

    # Lock overall completion
    store_key = f"all:{uid}"
    if progress_pct == 100:
        completed_store[store_key] = True
    is_completed = completed_store.get(store_key, False)

    # Dates
    all_dates = []
    for c in bc:
        for key in ('StartDate', 'DateCompleted'):
            d = parse_date(c.get(key))
            if d: all_dates.append(d)

    start_date   = min(all_dates) if all_dates else None
    days_started = days_since(start_date)
    days_ago     = days_since(max(all_dates)) if all_dates else 9999

    # EP: 10% per day, locks at 100% if overdue and not complete
    days_elapsed = min(days_started, COHORT_DAYS) if start_date else None
    ep = min(round(days_elapsed / COHORT_DAYS * 100), 100) if days_elapsed is not None else 0
    if days_started > COHORT_DAYS and not is_completed:
        ep = 100

    diff     = progress_pct - ep
    scores   = [c['PercentageComplete'] for c in bc if c.get('PercentageComplete')]
    quiz_avg = round(sum(scores)/len(scores)) if scores else 0
    failed   = len([c for c in bc if c.get('StartDate') and (c.get('PercentageComplete') or 0) < 70 and not c.get('Complete')])

    # R-Index
    prog_score  = min(100, round((progress_pct / ep) * 100)) if ep > 0 else 100
    login_score = (100 if days_ago == 0 else 90 if days_ago == 1 else 75 if days_ago == 2 else
                   60 if days_ago == 3 else 35 if days_ago <= 5 else 10 if days_ago < 9999 else 0)
    quiz_score  = min(100, quiz_avg)
    ri          = round(prog_score * 0.45 + login_score * 0.35 + quiz_score * 0.20)

    # Bucket
    if start_date is None and not any(c.get('StartDate') for c in bc):
        bucket = "not_started"
    elif is_completed:
        bucket = "historical"
    else:
        bucket = "active"

    # Status
    if is_completed:
        status = "green"
    elif diff < -40 or failed > 3 or (days_ago > 3 and days_ago < 9999):
        status = "red"
    elif diff < -20 or failed >= 1 or (days_ago > 3 and days_ago < 9999):
        status = "yellow"
    else:
        status = "green"

    if ri >= 75:
        status = "green"
        status_reason = f"R-Index {ri} — strong overall"
    elif ri >= 40 and status == "red":
        status = "yellow"
        status_reason = f"R-Index {ri} — some risk signals"
    elif ri < 40:
        status = "red"
        status_reason = f"R-Index {ri} — needs attention"
    else:
        if status == "green":
            status_reason = f"R-Index {ri} — on pace"
        elif status == "yellow":
            flags = []
            if diff < -20: flags.append(f"{abs(diff)}% behind pace")
            if failed >= 1: flags.append(f"{failed} course(s) struggling")
            if days_ago > 3 and days_ago < 9999: flags.append(f"inactive {days_ago}d")
            status_reason = " · ".join(flags) if flags else f"R-Index {ri}"
        else:
            flags = []
            if diff < -40: flags.append(f"{abs(diff)}% behind pace")
            if failed > 3:  flags.append(f"{failed} courses struggling")
            if days_ago > 3 and days_ago < 9999: flags.append(f"inactive {days_ago}d")
            status_reason = " · ".join(flags) if flags else f"R-Index {ri}"

    all_learners.append({
        "name":            name,
        "email":           u.get("Email",""),
        "progress":        progress_pct,
        "ep":              ep,
        "diff":            diff,
        "quizAvg":         quiz_avg,
        "lastLoginLabel":  login_label(days_ago),
        "loginDays":       days_ago,
        "completedCount":  len(completed_courses),
        "totalCourses":    TOTAL_COURSES,
        "startDate":       start_date,
        "daysElapsed":     days_elapsed,
        "daysStarted":     days_started,
        "isCompleted":     is_completed,
        "status":          status,
        "statusReason":    status_reason,
        "bucket":          bucket,
        "courses": [
            {
                "name":     all_course_names.get(c['Id'], c.get('Name','')),
                "complete": c.get('Complete', False),
                "pct":      c.get('PercentageComplete', 0),
                "started":  parse_date(c.get('StartDate'))
            }
            for c in bc
        ]
    })

with open(COMPLETED_FILE, 'w') as f:
    json.dump(completed_store, f, indent=2)
print(f"\nCompletions saved: {len(completed_store)} total")

from zoneinfo import ZoneInfo
generated_at = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%B %d, %Y at %I:%M %p")
learners_json = json.dumps(all_learners, ensure_ascii=False)
print("Building dashboard HTML...")

css = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --brand-dark:    #032169;
    --brand-main:    #0d4ccd;
    --brand-light:   #30bced;
    --page-bg:       #f2f4f8;
    --surface:       #FFFFFF;
    --border:        #d4d4da;
    --blue-50:       #e8edf7;
    --blue-100:      #c0cef0;
    --blue-400:      #30bced;
    --green-50:      #E8F5E9; --green-400: #43A047; --green-600: #2E7D32;
    --yellow-50:     #FFF8E1; --yellow-400: #FFB300; --yellow-600: #E65100;
    --red-50:        #FFEBEE; --red-400: #E53935; --red-600: #C62828;
    --gray-50:       #f2f4f8; --gray-100: #e8eaed; --gray-200: #d4d4da;
    --text-primary:  #262626; --text-secondary: #4a5256; --text-tertiary: #a7a9ab;
    --radius-md: 8px; --radius-lg: 12px; --radius-xl: 16px;
  }
  html, body { font-family: 'Inter', sans-serif; font-size: 14px; background: var(--page-bg); color: var(--text-primary); min-height: 100vh; line-height: 1.5; -webkit-font-smoothing: antialiased; }
  .page-wrap { max-width: 1280px; margin: 0 auto; padding: 28px 24px 48px; }
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; }
  .header-left { display: flex; align-items: center; gap: 14px; }
  .logo-mark { width: 38px; height: 38px; background: var(--brand-dark); border-radius: 10px; display: flex; align-items: center; justify-content: center; }
  .logo-mark i { color: #fff; font-size: 20px; }
  .header-title { font-size: 20px; font-weight: 600; color: var(--text-primary); letter-spacing: -0.3px; }
  .header-sub { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
  .badge-refreshed { background: var(--gray-100); color: var(--text-secondary); font-size: 11px; font-weight: 500; padding: 5px 12px; border-radius: 20px; border: 1px solid var(--border); display: flex; align-items: center; gap: 5px; }
  .tab-bar { display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 2px solid var(--border); }
  .tab-btn { font-size: 13px; font-weight: 500; padding: 10px 18px; border: none; background: none; color: var(--text-secondary); cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; font-family: inherit; transition: all 0.12s; border-radius: 6px 6px 0 0; }
  .tab-btn:hover { background: var(--gray-50); color: var(--text-primary); }
  .tab-btn.active { color: var(--brand-main); border-bottom-color: var(--brand-main); background: var(--blue-50); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .controls-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
  .controls-label { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
  .window-select { font-size: 12px; font-family: inherit; padding: 6px 12px; border-radius: 20px; border: 1px solid var(--border); background: var(--surface); color: var(--text-primary); cursor: pointer; outline: none; }
  .window-select:focus { border-color: var(--brand-main); }
  .kpi-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 16px; }
  .kpi-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px 18px; }
  .kpi-label { font-size: 11px; font-weight: 500; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }
  .kpi-value { font-size: 28px; font-weight: 600; color: var(--text-primary); line-height: 1; letter-spacing: -0.5px; }
  .kpi-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 5px; }
  .health-card { background: var(--blue-50); border: 1px solid var(--blue-100); border-radius: var(--radius-xl); padding: 18px 22px; margin-bottom: 16px; display: flex; align-items: center; gap: 22px; }
  .health-ring-wrap { position: relative; width: 82px; height: 82px; flex-shrink: 0; }
  .health-ring-wrap svg { width: 82px; height: 82px; }
  .health-score-label { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); text-align: center; }
  .health-score-num { font-size: 20px; font-weight: 600; color: var(--brand-dark); line-height: 1; }
  .health-score-txt { font-size: 10px; color: var(--brand-main); font-weight: 500; }
  .health-title { font-size: 14px; font-weight: 600; color: var(--brand-dark); margin-bottom: 4px; }
  .health-desc { font-size: 12px; color: var(--brand-main); line-height: 1.55; }
  .status-pills { display: flex; gap: 7px; margin-top: 10px; flex-wrap: wrap; }
  .pill { font-size: 12px; font-weight: 500; padding: 4px 12px; border-radius: 20px; }
  .pill-green  { background: var(--green-50);  color: var(--green-600);  border: 1px solid #C8E6C9; }
  .pill-yellow { background: var(--yellow-50); color: var(--yellow-600); border: 1px solid #FFE082; }
  .pill-red    { background: var(--red-50);    color: var(--red-600);    border: 1px solid #FFCDD2; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 16px; }
  .panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 18px 20px; }
  .panel-title { font-size: 13px; font-weight: 600; color: var(--text-primary); margin-bottom: 16px; display: flex; align-items: center; gap: 7px; }
  .panel-title i { font-size: 15px; color: var(--brand-light); }
  .donut-layout { display: flex; align-items: center; gap: 22px; }
  .donut-wrap { position: relative; width: 110px; height: 110px; flex-shrink: 0; }
  .donut-wrap svg { width: 110px; height: 110px; }
  .donut-center { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); text-align: center; }
  .donut-count { font-size: 22px; font-weight: 600; color: var(--text-primary); line-height: 1; }
  .donut-lbl { font-size: 10px; color: var(--text-secondary); margin-top: 3px; }
  .donut-legend { flex: 1; display: flex; flex-direction: column; gap: 11px; }
  .legend-row { display: flex; align-items: center; justify-content: space-between; }
  .legend-left { display: flex; align-items: center; gap: 8px; }
  .legend-dot { width: 9px; height: 9px; border-radius: 50%; }
  .legend-name { font-size: 12px; color: var(--text-secondary); }
  .legend-right { display: flex; gap: 10px; align-items: center; }
  .legend-count { font-size: 13px; font-weight: 600; color: var(--text-primary); }
  .legend-pct { font-size: 11px; color: var(--text-tertiary); min-width: 30px; text-align: right; }
  .pace-row { display: flex; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); }
  .pace-item { flex: 1; text-align: center; }
  .pace-item + .pace-item { border-left: 1px solid var(--border); }
  .pace-val { font-size: 16px; font-weight: 600; }
  .pace-lbl { font-size: 10px; color: var(--text-secondary); margin-top: 2px; text-transform: uppercase; letter-spacing: 0.04em; }
  .score-weighting { display: flex; flex-direction: column; gap: 15px; }
  .weight-row { display: flex; flex-direction: column; gap: 6px; }
  .weight-header { display: flex; align-items: center; justify-content: space-between; }
  .weight-name { font-size: 13px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }
  .weight-name i { font-size: 14px; color: var(--brand-light); }
  .weight-pct { font-size: 13px; font-weight: 600; color: var(--text-primary); min-width: 36px; text-align: right; }
  input[type=range] { -webkit-appearance: none; appearance: none; width: 100%; height: 5px; border-radius: 3px; outline: none; cursor: pointer; margin: 0; }
  input[type=range].slider-prog  { background: linear-gradient(to right, #0d4ccd 0%, #0d4ccd 45%, #d4d4da 45%); color: #0d4ccd; }
  input[type=range].slider-login { background: linear-gradient(to right, #43A047 0%, #43A047 35%, #d4d4da 35%); color: #43A047; }
  input[type=range].slider-quiz  { background: linear-gradient(to right, #FFB300 0%, #FFB300 20%, #d4d4da 20%); color: #FFB300; }
  input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 15px; height: 15px; border-radius: 50%; background: #fff; border: 2px solid currentColor; box-shadow: 0 1px 3px rgba(0,0,0,0.15); }
  input[type=range]::-moz-range-thumb { width: 15px; height: 15px; border-radius: 50%; background: #fff; border: 2px solid currentColor; cursor: pointer; }
  .weight-total-row { display: flex; align-items: center; justify-content: space-between; padding: 7px 12px; border-radius: var(--radius-md); background: var(--gray-50); border: 1px solid var(--border); margin-top: 2px; }
  .weight-total-lbl { font-size: 12px; color: var(--text-secondary); }
  .weight-total-val { font-size: 13px; font-weight: 600; }
  .total-ok { color: var(--green-600); }
  .cohort-row { display: flex; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); }
  .cohort-item { flex: 1; text-align: center; }
  .cohort-item + .cohort-item { border-left: 1px solid var(--border); }
  .cohort-val { font-size: 16px; font-weight: 600; }
  .cohort-lbl { font-size: 10px; color: var(--text-secondary); margin-top: 2px; text-transform: uppercase; letter-spacing: 0.04em; }
  .learner-table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
  .table-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--border); }
  .table-title { font-size: 13px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
  .table-title i { font-size: 15px; color: var(--brand-light); }
  .table-hint { font-size: 11px; color: var(--text-tertiary); margin-left: 3px; font-weight: 400; }
  .filter-btns { display: flex; gap: 5px; }
  .filter-btn { font-size: 12px; padding: 4px 11px; border-radius: 20px; border: 1px solid var(--border); background: transparent; color: var(--text-secondary); cursor: pointer; font-family: inherit; transition: all 0.1s; }
  .filter-btn:hover { background: var(--gray-100); }
  .filter-btn.active { background: var(--brand-main); color: #fff; border-color: var(--brand-main); }
  table { width: 100%; border-collapse: collapse; }
  thead th { font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.05em; padding: 9px 16px; text-align: left; background: var(--gray-50); border-bottom: 1px solid var(--border); }
  tbody tr { border-bottom: 1px solid var(--gray-100); cursor: pointer; transition: background 0.1s; }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: var(--blue-50); }
  td { padding: 11px 16px; vertical-align: middle; }
  .learner-name { display: flex; align-items: center; gap: 9px; }
  .avatar { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 9px; font-weight: 600; flex-shrink: 0; }
  .av-green  { background: var(--green-50);  color: var(--green-600); }
  .av-yellow { background: var(--yellow-50); color: var(--yellow-600); }
  .av-red    { background: var(--red-50);    color: var(--red-600); }
  .name-text { font-size: 13px; font-weight: 500; }
  .name-green  { color: var(--green-600); }
  .name-yellow { color: var(--yellow-600); }
  .name-red    { color: var(--red-600); }
  .prog-wrap { display: flex; align-items: center; gap: 7px; }
  .prog-track { width: 70px; height: 5px; background: var(--gray-100); border-radius: 3px; overflow: hidden; flex-shrink: 0; position: relative; }
  .prog-fill { height: 100%; border-radius: 3px; }
  .pf-green  { background: var(--green-400); }
  .pf-yellow { background: var(--yellow-400); }
  .pf-red    { background: var(--red-400); }
  .ep-tick { position: absolute; top: -2px; height: 9px; width: 2px; background: var(--brand-light); border-radius: 1px; z-index: 1; }
  .prog-val { font-size: 12px; color: var(--text-secondary); white-space: nowrap; }
  .diff-green  { font-size: 12px; color: var(--green-600); font-weight: 500; }
  .diff-yellow { font-size: 12px; color: var(--yellow-600); font-weight: 500; }
  .diff-red    { font-size: 12px; color: var(--red-600); font-weight: 500; }
  .diff-gray   { font-size: 12px; color: var(--text-tertiary); }
  .quiz-val { font-size: 13px; font-weight: 500; }
  .qv-green { color: var(--green-600); } .qv-yellow { color: var(--yellow-600); } .qv-red { color: var(--red-600); } .qv-gray { color: var(--text-tertiary); }
  .login-ok   { font-size: 12px; color: var(--text-secondary); }
  .login-warn { font-size: 12px; color: var(--yellow-600); font-weight: 500; }
  .login-bad  { font-size: 12px; color: var(--red-600); font-weight: 500; }
  .ri-num { font-size: 13px; font-weight: 600; color: var(--text-primary); }
  .status-badge { font-size: 11px; font-weight: 500; padding: 3px 9px; border-radius: 20px; cursor: help; }
  .sb-green  { background: var(--green-50);  color: var(--green-600);  border: 1px solid #C8E6C9; }
  .sb-yellow { background: var(--yellow-50); color: var(--yellow-600); border: 1px solid #FFE082; }
  .sb-red    { background: var(--red-50);    color: var(--red-600);    border: 1px solid #FFCDD2; }
  .day-badge { font-size: 12px; color: var(--text-secondary); }
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(15,23,42,0.35); z-index: 200; align-items: center; justify-content: center; padding: 24px; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--surface); border-radius: var(--radius-xl); padding: 22px; width: 100%; max-width: 540px; border: 1px solid var(--border); max-height: 90vh; overflow-y: auto; }
  .modal-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .modal-name { font-size: 15px; font-weight: 600; color: var(--text-primary); }
  .close-btn { background: var(--gray-100); border: none; cursor: pointer; color: var(--text-secondary); width: 26px; height: 26px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 15px; }
  .close-btn:hover { background: var(--gray-200); }
  .modal-stats { display: grid; grid-template-columns: repeat(4,1fr); gap: 7px; margin-bottom: 14px; }
  .modal-stat { background: var(--gray-50); border-radius: var(--radius-md); padding: 9px; text-align: center; border: 1px solid var(--border); }
  .modal-stat .mv { font-size: 14px; font-weight: 600; color: var(--text-primary); }
  .modal-stat .ml { font-size: 10px; color: var(--text-secondary); margin-top: 2px; text-transform: uppercase; letter-spacing: 0.04em; }
  .ri-factors { display: grid; grid-template-columns: repeat(3,1fr); gap: 7px; margin-bottom: 14px; }
  .ri-factor { background: var(--blue-50); border-radius: var(--radius-md); padding: 9px; text-align: center; border: 1px solid var(--blue-100); }
  .ri-factor .rv { font-size: 17px; font-weight: 600; color: var(--brand-main); }
  .ri-factor .rl { font-size: 10px; color: var(--brand-dark); margin-top: 2px; }
  .modal-section-label { font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }
  .course-item { display: flex; align-items: center; justify-content: space-between; padding: 9px 0; border-bottom: 1px solid var(--gray-100); }
  .course-item:last-child { border-bottom: none; }
  .course-name { font-size: 12px; color: var(--text-primary); flex: 1; margin-right: 10px; }
  .cb-pass { background: var(--green-50); color: var(--green-600); border: 1px solid #C8E6C9; font-size: 11px; font-weight: 500; padding: 2px 8px; border-radius: 20px; white-space: nowrap; }
  .cb-prog { background: var(--yellow-50); color: var(--yellow-600); border: 1px solid #FFE082; font-size: 11px; font-weight: 500; padding: 2px 8px; border-radius: 20px; white-space: nowrap; }
  .cb-ns   { background: var(--gray-100); color: var(--text-secondary); font-size: 11px; font-weight: 500; padding: 2px 8px; border-radius: 20px; white-space: nowrap; }
  .footer { text-align: center; font-size: 11px; color: var(--text-tertiary); margin-top: 28px; padding-top: 14px; border-top: 1px solid var(--border); }
  .pw-page { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; background: var(--page-bg); }
  .pw-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-xl); padding: 40px; width: 100%; max-width: 380px; box-shadow: 0 4px 24px rgba(0,0,0,0.06); text-align: center; }
  .pw-lock { width: 56px; height: 56px; background: var(--blue-50); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 20px; }
  .pw-lock i { font-size: 26px; color: var(--brand-main); }
  .pw-title { font-size: 20px; font-weight: 600; color: var(--text-primary); margin-bottom: 4px; }
  .pw-sub { font-size: 13px; color: var(--text-secondary); margin-bottom: 24px; }
  .pw-input { width: 100%; padding: 11px 14px; border: 1px solid var(--border); border-radius: var(--radius-md); font-size: 14px; font-family: inherit; color: var(--text-primary); outline: none; margin-bottom: 10px; text-align: center; transition: border-color 0.12s, box-shadow 0.12s; }
  .pw-input:focus { border-color: var(--brand-main); box-shadow: 0 0 0 3px rgba(13,76,205,0.1); }
  .pw-error { font-size: 12px; color: var(--red-600); min-height: 18px; margin-bottom: 10px; }
  .pw-btn { width: 100%; background: var(--brand-dark); color: #fff; border: none; border-radius: var(--radius-md); padding: 12px; font-size: 14px; font-weight: 500; cursor: pointer; font-family: inherit; transition: background 0.12s; }
  .pw-btn:hover { background: var(--brand-main); }
  .pw-note { font-size: 12px; color: var(--text-tertiary); margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border); }
  .empty-state { padding: 48px; text-align: center; color: var(--text-secondary); font-size: 13px; }
  .tip { position: relative; display: inline-flex; align-items: center; cursor: default; }
  .tip-icon { width: 14px; height: 14px; border-radius: 50%; background: var(--gray-200); color: var(--text-secondary); font-size: 9px; font-weight: 700; display: inline-flex; align-items: center; justify-content: center; margin-left: 5px; flex-shrink: 0; cursor: help; font-style: normal; line-height: 1; }
  .tip-icon:hover { background: var(--blue-100); color: var(--brand-main); }
  .tip-box { display: none; position: absolute; bottom: calc(100% + 7px); left: 50%; transform: translateX(-50%); background: #1E293B; color: #F1F5F9; font-size: 11px; font-weight: 400; line-height: 1.5; padding: 6px 10px; border-radius: 6px; white-space: nowrap; z-index: 999; pointer-events: none; box-shadow: 0 4px 12px rgba(0,0,0,0.2); }
  .tip-box::after { content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%); border: 5px solid transparent; border-top-color: #1E293B; }
  .tip:hover .tip-box { display: block; }
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
"""

js = r"""
var ECC_PASSWORD = 'ECCAdmin2026!';

function checkPW() {
  var val = document.getElementById('pwInput').value;
  var err = document.getElementById('pwError');
  if (!val) { err.textContent = 'Please enter the password.'; return; }
  if (val === ECC_PASSWORD) {
    document.getElementById('screen-password').style.display = 'none';
    document.getElementById('screen-dashboard').style.display = 'block';
    document.getElementById('pwInput').value = '';
  } else {
    err.textContent = 'Incorrect password. Please try again.';
    document.getElementById('pwInput').value = '';
  }
}

document.addEventListener('DOMContentLoaded', function() {
  var pi = document.getElementById('pwInput');
  if (pi) pi.addEventListener('keydown', function(e) { if (e.key === 'Enter') checkPW(); });
});

const ALL_LEARNERS = __LEARNERS__;
let weights = { prog: 45, login: 35, quiz: 20 };
let activeWindow = 30;
let activeFilter = 'all';

function calcRI(l) {
  const progScore  = l.ep > 0 ? Math.min(100, Math.round((l.progress / l.ep) * 100)) : 100;
  const loginScore = l.loginDays === 0 ? 100 : l.loginDays === 1 ? 90 : l.loginDays === 2 ? 75 :
                     l.loginDays <= 3 ? 60 : l.loginDays <= 5 ? 35 : l.loginDays < 9999 ? 10 : 0;
  const quizScore  = Math.min(100, l.quizAvg || 0);
  const t = weights.prog + weights.login + weights.quiz;
  const wP = t > 0 ? weights.prog/100 : 0.45;
  const wL = t > 0 ? weights.login/100 : 0.35;
  const wQ = t > 0 ? weights.quiz/100  : 0.20;
  return { ri: Math.round(progScore*wP + loginScore*wL + quizScore*wQ), progScore, loginScore, quizScore };
}

function getFiltered() {
  return ALL_LEARNERS.filter(function(l) {
    return l.bucket === 'active' && l.daysStarted <= activeWindow;
  });
}

function switchTab(bucket, btn) {
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
  document.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
  btn.classList.add('active');
  document.getElementById('tab-' + bucket).classList.add('active');
}

function setWindow(val) {
  activeWindow = parseInt(val);
  refreshActiveView();
}

function setStatusFilter(val, btn) {
  activeFilter = val;
  btn.closest('.filter-btns').querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  renderActiveTable();
}

function refreshActiveView() {
  renderKPIs();
  renderHealthBanner();
  renderDonut();
  renderActiveTable();
}

function renderKPIs() {
  var rows   = getFiltered();
  var green  = rows.filter(function(l){return l.status==='green';}).length;
  var yellow = rows.filter(function(l){return l.status==='yellow';}).length;
  var red    = rows.filter(function(l){return l.status==='red';}).length;
  var avgProg = rows.length ? Math.round(rows.reduce(function(s,l){return s+l.progress;},0)/rows.length) : 0;
  document.getElementById('kpiTotal').textContent  = rows.length;
  document.getElementById('kpiGreen').textContent  = green;
  document.getElementById('kpiYellow').textContent = yellow;
  document.getElementById('kpiRed').textContent    = red;
  document.getElementById('kpiAvg').textContent    = avgProg + '%';
  document.getElementById('kpiGreen2').textContent  = green;
  document.getElementById('kpiYellow2').textContent = yellow;
  document.getElementById('kpiRed2').textContent    = red;
}

function renderHealthBanner() {
  var rows  = getFiltered();
  var avgRI = rows.length ? Math.round(rows.reduce(function(s,l){return s+calcRI(l).ri;},0)/rows.length) : 0;
  var red   = rows.filter(function(l){return l.status==='red';}).length;
  var riOff = (226.19 - 226.19 * avgRI / 100).toFixed(2);
  document.getElementById('riRing').setAttribute('stroke-dashoffset', riOff);
  document.getElementById('riScore').textContent = avgRI;
  document.getElementById('cohortRIval').textContent = avgRI;
  document.getElementById('riDesc').innerHTML = 'Readiness Index <strong>' + avgRI + '/100</strong> across ' + rows.length + ' active learners. ' + red + ' learner' + (red !== 1 ? 's' : '') + ' need attention.';
  document.getElementById('pillGreen').textContent  = rows.filter(function(l){return l.status==='green';}).length  + ' on track';
  document.getElementById('pillYellow').textContent = rows.filter(function(l){return l.status==='yellow';}).length + ' at risk';
  document.getElementById('pillRed').textContent    = rows.filter(function(l){return l.status==='red';}).length    + ' behind';
  var tt = document.getElementById('riWeightTooltip');
  if (tt) tt.textContent = '0\u2013100 score: progress (' + weights.prog + '%) \u00b7 login (' + weights.login + '%) \u00b7 quiz (' + weights.quiz + '%)';
}

function renderDonut() {
  var rows   = getFiltered();
  var total  = rows.length || 1;
  var green  = rows.filter(function(l){return l.status==='green';}).length;
  var yellow = rows.filter(function(l){return l.status==='yellow';}).length;
  var red    = rows.filter(function(l){return l.status==='red';}).length;
  var circ   = 263.89;
  var gLen   = circ * green  / total;
  var yLen   = circ * yellow / total;
  var rLen   = circ * red    / total;
  var yRot   = -90 + (green  / total * 360);
  var rRot   = -90 + ((green + yellow) / total * 360);
  var avgProg = rows.length ? Math.round(rows.reduce(function(s,l){return s+l.progress;},0)/rows.length) : 0;
  var epAvg   = rows.length ? Math.round(rows.reduce(function(s,l){return s+(l.ep||0);},0)/rows.length) : 0;
  var variance = avgProg - epAvg;
  document.getElementById('dGreen').setAttribute('stroke-dashoffset',  (circ-gLen).toFixed(2));
  document.getElementById('dYellow').setAttribute('stroke-dashoffset', (circ-yLen).toFixed(2));
  document.getElementById('dYellow').setAttribute('transform','rotate('+yRot.toFixed(1)+' 55 55)');
  document.getElementById('dRed').setAttribute('stroke-dashoffset',    (circ-rLen).toFixed(2));
  document.getElementById('dRed').setAttribute('transform','rotate('+rRot.toFixed(1)+' 55 55)');
  document.getElementById('donutCount').textContent = rows.length;
  document.getElementById('legendGreen').textContent      = green;
  document.getElementById('legendYellow').textContent     = yellow;
  document.getElementById('legendRed').textContent        = red;
  document.getElementById('legendGreenPct').textContent   = Math.round(green/total*100)  + '%';
  document.getElementById('legendYellowPct').textContent  = Math.round(yellow/total*100) + '%';
  document.getElementById('legendRedPct').textContent     = Math.round(red/total*100)    + '%';
  document.getElementById('paceAvg').textContent = avgProg + '%';
  document.getElementById('paceEP').textContent  = epAvg  + '%';
  var varEl = document.getElementById('paceVar');
  varEl.textContent = (variance >= 0 ? '+' : '') + variance + '%';
  varEl.style.color = variance >= 0 ? 'var(--green-600)' : 'var(--red-600)';
  document.getElementById('paceLearners').textContent = rows.length;
}

function renderActiveTable() {
  var tbody = document.getElementById('tbodyActive');
  if (!tbody) return;
  var rows = getFiltered();
  if (activeFilter !== 'all') rows = rows.filter(function(l){return l.status === activeFilter;});
  rows.sort(function(a,b){return b.progress - a.progress;});
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:24px;color:var(--text-tertiary);font-size:13px;">No learners match this filter.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(function(l) {
    var idx      = ALL_LEARNERS.indexOf(l);
    var diff     = l.progress - l.ep;
    var diffStr  = l.ep > 0 ? (diff >= 0 ? '+'+diff+'%' : diff+'%') : '—';
    var diffCls  = l.ep === 0 ? 'diff-gray' : diff >= 0 ? 'diff-green' : diff >= -20 ? 'diff-yellow' : 'diff-red';
    var quizCls  = l.quizAvg >= 75 ? 'qv-green' : l.quizAvg >= 60 ? 'qv-yellow' : l.quizAvg > 0 ? 'qv-red' : 'qv-gray';
    var loginCls = l.loginDays <= 2 ? 'login-ok' : l.loginDays <= 4 ? 'login-warn' : 'login-bad';
    var epPct    = Math.min(l.ep, 100);
    var ri       = calcRI(l).ri;
    var initials = l.name.split(' ').map(function(w){return w[0]||'';}).join('').slice(0,2).toUpperCase();
    var isOverdue = l.daysStarted > 10 && !l.isCompleted;
    var dayLabel  = isOverdue ? 'Overdue' : (l.daysElapsed !== null ? 'Day '+Math.min(l.daysElapsed+1,10) : '—');
    var dayStyle  = isOverdue ? 'color:var(--red-600);font-weight:500;' : '';
    var epTick    = epPct > 0 ? '<div class="ep-tick" style="left:calc('+epPct+'% - 1px)"></div>' : '';
    var statusLabel  = l.status === 'green' ? 'On track' : l.status === 'yellow' ? 'At risk' : 'Behind';
    var statusReason = (l.statusReason||'').replace(/'/g,'&#39;');
    return '<tr onclick="openModal('+idx+')">'
      + '<td><div class="learner-name"><div class="avatar av-'+l.status+'">'+initials+'</div><span class="name-text name-'+l.status+'">'+l.name+'</span></div></td>'
      + '<td><div class="prog-wrap"><div class="prog-track"><div class="prog-fill pf-'+l.status+'" style="width:'+l.progress+'%"></div>'+epTick+'</div><span class="prog-val">'+l.progress+'% ('+l.completedCount+'/'+l.totalCourses+')</span></div></td>'
      + '<td class="'+diffCls+'">'+diffStr+'</td>'
      + '<td class="day-badge" style="'+dayStyle+'">'+dayLabel+'</td>'
      + '<td class="quiz-val '+quizCls+'">'+(l.quizAvg > 0 ? l.quizAvg+'%' : '—')+'</td>'
      + '<td class="'+loginCls+'">'+l.lastLoginLabel+'</td>'
      + '<td class="ri-num">'+ri+'</td>'
      + '<td><span class="tip status-badge sb-'+l.status+'">'+statusLabel+'<span class="tip-box" style="right:0;left:auto;transform:none;">'+statusReason+'</span></span></td>'
      + '</tr>';
  }).join('');
}

function renderHistoricalTable() {
  var tbody = document.getElementById('tbodyHistorical');
  if (!tbody) return;
  var rows = ALL_LEARNERS.filter(function(l){return l.bucket === 'historical';});
  rows.sort(function(a,b){return a.name.localeCompare(b.name);});
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--text-tertiary);font-size:13px;">No completions yet.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(function(l) {
    var idx      = ALL_LEARNERS.indexOf(l);
    var initials = l.name.split(' ').map(function(w){return w[0]||'';}).join('').slice(0,2).toUpperCase();
    return '<tr onclick="openModal('+idx+')">'
      + '<td><div class="learner-name"><div class="avatar av-green">'+initials+'</div><span class="name-text name-green">'+l.name+'</span></div></td>'
      + '<td style="font-size:12px;color:var(--text-secondary);">'+(l.startDate||'—')+'</td>'
      + '<td class="quiz-val qv-green">'+(l.quizAvg > 0 ? l.quizAvg+'%' : '—')+'</td>'
      + '<td><span class="status-badge sb-green">All ' + l.totalCourses + ' courses complete</span></td>'
      + '</tr>';
  }).join('');
}

function renderNotStartedTable() {
  var tbody = document.getElementById('tbodyNotStarted');
  if (!tbody) return;
  var rows = ALL_LEARNERS.filter(function(l){return l.bucket === 'not_started';});
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" style="text-align:center;padding:24px;color:var(--text-tertiary);font-size:13px;">No learners pending start.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(function(l) {
    var idx      = ALL_LEARNERS.indexOf(l);
    var initials = l.name.split(' ').map(function(w){return w[0]||'';}).join('').slice(0,2).toUpperCase();
    return '<tr onclick="openModal('+idx+')">'
      + '<td><div class="learner-name"><div class="avatar" style="background:var(--gray-100);color:var(--text-secondary);">'+initials+'</div><span class="name-text" style="color:var(--text-secondary);">'+l.name+'</span></div></td>'
      + '<td style="font-size:12px;color:var(--text-tertiary);">'+l.email+'</td>'
      + '<td><span class="status-badge" style="background:var(--gray-100);color:var(--text-secondary);border:1px solid var(--border);">Not started</span></td>'
      + '</tr>';
  }).join('');
}

function onSlider(changed) {
  var progEl  = document.getElementById('sliderProg');
  var loginEl = document.getElementById('sliderLogin');
  var quizEl  = document.getElementById('sliderQuiz');
  var prog  = parseInt(progEl.value);
  var login = parseInt(loginEl.value);
  var quiz  = parseInt(quizEl.value);
  var total = prog + login + quiz;
  if (total > 100) {
    var excess = total - 100;
    if (changed === 'prog') {
      var ot = login + quiz;
      if (ot > 0) { login = Math.max(0, login - Math.round(excess * login / ot)); quiz = 100 - prog - login; quiz = Math.max(0, quiz); }
      else { login = 0; quiz = 0; prog = 100; }
    } else if (changed === 'login') {
      var ot = prog + quiz;
      if (ot > 0) { prog = Math.max(0, prog - Math.round(excess * prog / ot)); quiz = 100 - login - prog; quiz = Math.max(0, quiz); }
      else { prog = 0; quiz = 0; login = 100; }
    } else {
      var ot = prog + login;
      if (ot > 0) { prog = Math.max(0, prog - Math.round(excess * prog / ot)); login = 100 - quiz - prog; login = Math.max(0, login); }
      else { prog = 0; login = 0; quiz = 100; }
    }
    prog  = Math.round(prog  / 5) * 5;
    login = Math.round(login / 5) * 5;
    quiz  = 100 - prog - login; quiz = Math.max(0, quiz);
    progEl.value = prog; loginEl.value = login; quizEl.value = quiz;
  }
  weights = { prog: prog, login: login, quiz: quiz };
  document.getElementById('wProg').textContent  = prog  + '%';
  document.getElementById('wLogin').textContent = login + '%';
  document.getElementById('wQuiz').textContent  = quiz  + '%';
  document.getElementById('weightTotal').textContent = '= ' + (prog+login+quiz) + '%';
  var tt = document.getElementById('riWeightTooltip');
  if (tt) tt.textContent = '0\u2013100 score: progress (' + prog + '%) \u00b7 login (' + login + '%) \u00b7 quiz (' + quiz + '%)';
  updateSliderFill('sliderProg',  prog,  '#0d4ccd');
  updateSliderFill('sliderLogin', login, '#43A047');
  updateSliderFill('sliderQuiz',  quiz,  '#FFB300');
  renderHealthBanner();
  renderActiveTable();
}

function updateSliderFill(id, val, fill) {
  var el = document.getElementById(id);
  if (el) el.style.background = 'linear-gradient(to right,'+fill+' 0%,'+fill+' '+val+'%,#d4d4da '+val+'%)';
}

function openModal(idx) {
  var l = ALL_LEARNERS[idx];
  var diff = l.progress - l.ep;
  var r    = calcRI(l);
  document.getElementById('modalName').textContent  = l.name;
  document.getElementById('mProgress').textContent  = l.progress + '% (' + l.completedCount + '/' + l.totalCourses + ' courses)';
  document.getElementById('mEP').textContent        = l.ep > 0 ? (diff >= 0 ? '+' : '') + diff + '%' : '—';
  document.getElementById('mLogin').textContent     = l.lastLoginLabel;
  document.getElementById('mRI').textContent        = r.ri + ' / 100';
  document.getElementById('riProg').textContent     = r.progScore;
  document.getElementById('riLogin').textContent    = r.loginScore;
  document.getElementById('riQuiz').textContent     = r.quizScore;
  document.getElementById('modalCourses').innerHTML = l.courses.map(function(c) {
    var cls = !c.started ? 'cb-ns' : c.complete ? 'cb-pass' : 'cb-prog';
    var lbl = !c.started ? 'Not started' : c.complete ? 'Complete' : (c.pct||0) + '% in progress';
    return '<div class="course-item"><span class="course-name">'+c.name+'</span><span class="'+cls+'">'+lbl+'</span></div>';
  }).join('') || '<div style="padding:12px 0;color:var(--text-tertiary);font-size:13px;">No course data.</div>';
  document.getElementById('modalOverlay').classList.add('open');
}

function closeModal() { document.getElementById('modalOverlay').classList.remove('open'); }
document.getElementById('modalOverlay').addEventListener('click', function(e) { if (e.target === this) closeModal(); });

refreshActiveView();
renderHistoricalTable();
renderNotStartedTable();
"""

js = js.replace('__LEARNERS__', learners_json)

total_active      = sum(1 for l in all_learners if l['bucket'] == 'active')
total_historical  = sum(1 for l in all_learners if l['bucket'] == 'historical')
total_not_started = sum(1 for l in all_learners if l['bucket'] == 'not_started')

html = (
'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
'<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
'<title>Enablement Command Center — SuccessKPI</title>\n'
'<link rel="preconnect" href="https://fonts.googleapis.com">\n'
'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
'<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">\n'
'<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css">\n'
'<style>\n' + css + '\n</style>\n</head>\n<body>\n'
'<div id="screen-password" style="display:block;">\n  <div class="pw-page">\n    <div class="pw-card">\n      <div class="pw-lock"><i class="ti ti-lock"></i></div>\n      <div class="pw-title">Enablement Command Center</div>\n      <div class="pw-sub">Enter the admin password to continue</div>\n      <input id="pwInput" class="pw-input" type="password" placeholder="Password" autocomplete="off" />\n      <div class="pw-error" id="pwError"></div>\n      <button class="pw-btn" onclick="checkPW()"><i class="ti ti-lock-open" style="font-size:14px;margin-right:6px;"></i>Enter dashboard</button>\n      <div class="pw-note">Contact your enablement team if you need access.</div>\n    </div>\n  </div>\n</div>\n<div id="screen-dashboard" style="display:none;">\n'
'<div class="page-wrap">\n'
'  <div class="header">\n'
'    <div class="header-left">\n'
'      <div class="logo-mark"><i class="ti ti-rocket"></i></div>\n'
'      <div>\n'
'        <div class="header-title">Enablement Command Center</div>\n'
'        <div class="header-sub">SuccessKPI &nbsp;&middot;&nbsp; Enablement Pathway &nbsp;&middot;&nbsp; ' + str(TOTAL_COURSES) + ' courses</div>\n'
'      </div>\n'
'    </div>\n'
'    <div class="badge-refreshed"><i class="ti ti-refresh" style="font-size:12px;"></i>&nbsp; Refreshed ' + generated_at + '</div>\n'
'  </div>\n'
'  <div class="tab-bar">\n'
f'    <button class="tab-btn active" onclick="switchTab(\'active\',this)">Active Learners ({total_active})</button>\n'
f'    <button class="tab-btn" onclick="switchTab(\'historical\',this);renderHistoricalTable()">Completed ({total_historical})</button>\n'
f'    <button class="tab-btn" onclick="switchTab(\'not_started\',this);renderNotStartedTable()">Not Started ({total_not_started})</button>\n'
'  </div>\n'
'  <div id="tab-active" class="tab-content active">\n'
'    <div class="controls-bar">\n'
'      <span class="controls-label"><i class="ti ti-calendar" style="font-size:13px;vertical-align:-2px;color:var(--brand-light);margin-right:3px;"></i>Window:</span>\n'
'      <select class="window-select" onchange="setWindow(this.value)">\n'
'        <option value="30">Last 30 days</option>\n'
'        <option value="60">Last 60 days</option>\n'
'        <option value="90">Last 90 days</option>\n'
'        <option value="120">Last 120 days</option>\n'
'        <option value="365">All (365 days)</option>\n'
'      </select>\n'
'    </div>\n'
'    <div class="kpi-grid">\n'
'      <div class="kpi-card"><div class="kpi-label">Active learners</div><div class="kpi-value" id="kpiTotal">—</div><div class="kpi-sub">in window</div></div>\n'
'      <div class="kpi-card"><div class="kpi-label">On track</div><div class="kpi-value" style="color:var(--green-600)" id="kpiGreen">—</div></div>\n'
'      <div class="kpi-card"><div class="kpi-label">At risk</div><div class="kpi-value" style="color:var(--yellow-600)" id="kpiYellow">—</div></div>\n'
'      <div class="kpi-card"><div class="kpi-label">Behind</div><div class="kpi-value" style="color:var(--red-600)" id="kpiRed">—</div></div>\n'
'      <div class="kpi-card"><div class="kpi-label">Avg progress</div><div class="kpi-value" id="kpiAvg">—</div></div>\n'
'    </div>\n'
'    <div class="health-card">\n'
'      <div class="health-ring-wrap"><svg viewBox="0 0 82 82" xmlns="http://www.w3.org/2000/svg">\n'
'        <circle cx="41" cy="41" r="36" fill="none" stroke="#c0cef0" stroke-width="8"/>\n'
'        <circle id="riRing" cx="41" cy="41" r="36" fill="none" stroke="#0d4ccd" stroke-width="8" stroke-dasharray="226.19" stroke-dashoffset="113" stroke-linecap="round" transform="rotate(-90 41 41)"/>\n'
'      </svg><div class="health-score-label"><div class="health-score-num" id="riScore">—</div><div class="health-score-txt">/ 100</div></div></div>\n'
'      <div class="health-details">\n'
'        <div class="health-title">Readiness Index <span class="tip"><span class="tip-icon">?</span><span class="tip-box" id="riWeightTooltip">0–100 score: progress (45%) · login (35%) · quiz (20%)</span></span></div>\n'
'        <div class="health-desc" id="riDesc">Loading...</div>\n'
'        <div class="status-pills">\n'
'          <span class="pill pill-green" id="pillGreen">— on track</span>\n'
'          <span class="pill pill-yellow" id="pillYellow">— at risk</span>\n'
'          <span class="pill pill-red" id="pillRed">— behind</span>\n'
'        </div>\n'
'      </div>\n'
'    </div>\n'
'    <div class="two-col">\n'
'      <div class="panel">\n'
'        <div class="panel-title"><i class="ti ti-chart-donut"></i> Status distribution</div>\n'
'        <div class="donut-layout">\n'
'          <div class="donut-wrap"><svg viewBox="0 0 110 110" xmlns="http://www.w3.org/2000/svg">\n'
'            <circle cx="55" cy="55" r="42" fill="none" stroke="#E8F5E9" stroke-width="13"/>\n'
'            <circle id="dGreen"  cx="55" cy="55" r="42" fill="none" stroke="#43A047" stroke-width="13" stroke-dasharray="263.89" stroke-dashoffset="263.89" stroke-linecap="butt" transform="rotate(-90 55 55)"/>\n'
'            <circle id="dYellow" cx="55" cy="55" r="42" fill="none" stroke="#FFB300" stroke-width="13" stroke-dasharray="263.89" stroke-dashoffset="263.89" stroke-linecap="butt" transform="rotate(-90 55 55)"/>\n'
'            <circle id="dRed"    cx="55" cy="55" r="42" fill="none" stroke="#E53935" stroke-width="13" stroke-dasharray="263.89" stroke-dashoffset="263.89" stroke-linecap="butt" transform="rotate(-90 55 55)"/>\n'
'          </svg><div class="donut-center"><div class="donut-count" id="donutCount">—</div><div class="donut-lbl">learners</div></div></div>\n'
'          <div class="donut-legend">\n'
'            <div class="legend-row"><div class="legend-left"><div class="legend-dot" style="background:#43A047"></div><span class="legend-name">On track</span></div><div class="legend-right"><span class="legend-count" id="legendGreen">—</span><span class="legend-pct" id="legendGreenPct">—</span></div></div>\n'
'            <div class="legend-row"><div class="legend-left"><div class="legend-dot" style="background:#FFB300"></div><span class="legend-name">At risk</span></div><div class="legend-right"><span class="legend-count" id="legendYellow">—</span><span class="legend-pct" id="legendYellowPct">—</span></div></div>\n'
'            <div class="legend-row"><div class="legend-left"><div class="legend-dot" style="background:#E53935"></div><span class="legend-name">Behind</span></div><div class="legend-right"><span class="legend-count" id="legendRed">—</span><span class="legend-pct" id="legendRedPct">—</span></div></div>\n'
'          </div>\n'
'        </div>\n'
'        <div class="pace-row">\n'
'          <div class="pace-item"><div class="pace-val" id="paceAvg" style="color:var(--brand-main)">—</div><div class="pace-lbl">Avg progress</div></div>\n'
'          <div class="pace-item"><div class="pace-val" id="paceEP" style="color:var(--blue-400)">—</div><div class="pace-lbl"><span class="tip">Expected <span class="tip-icon">?</span><span class="tip-box" style="left:0;transform:none;">10% per day from start date</span></span></div></div>\n'
'          <div class="pace-item"><div class="pace-val" id="paceVar">—</div><div class="pace-lbl">Variance</div></div>\n'
'          <div class="pace-item"><div class="pace-val" id="paceLearners">—</div><div class="pace-lbl">Learners</div></div>\n'
'        </div>\n'
'      </div>\n'
'      <div class="panel">\n'
'        <div class="panel-title"><i class="ti ti-adjustments-horizontal"></i> Readiness Index weighting <span class="tip"><span class="tip-icon">?</span><span class="tip-box">Drag sliders to adjust. Must total 100%.</span></span></div>\n'
'        <div class="score-weighting">\n'
'          <div class="weight-row"><div class="weight-header"><div class="weight-name"><i class="ti ti-trending-up"></i> Course progress</div><div class="weight-pct" id="wProg">45%</div></div><input type="range" class="slider-prog" id="sliderProg" min="0" max="100" step="5" value="45" oninput="onSlider(\'prog\')"></div>\n'
'          <div class="weight-row"><div class="weight-header"><div class="weight-name"><i class="ti ti-login"></i> Login activity</div><div class="weight-pct" id="wLogin">35%</div></div><input type="range" class="slider-login" id="sliderLogin" min="0" max="100" step="5" value="35" oninput="onSlider(\'login\')"></div>\n'
'          <div class="weight-row"><div class="weight-header"><div class="weight-name"><i class="ti ti-clipboard-check"></i> Quiz performance</div><div class="weight-pct" id="wQuiz">20%</div></div><input type="range" class="slider-quiz" id="sliderQuiz" min="0" max="100" step="5" value="20" oninput="onSlider(\'quiz\')"></div>\n'
'          <div class="weight-total-row"><span class="weight-total-lbl">Total weighting</span><span class="weight-total-val total-ok" id="weightTotal">= 100%</span></div>\n'
'        </div>\n'
'        <div class="cohort-row">\n'
'          <div class="cohort-item"><div class="cohort-val" style="color:var(--green-600)" id="kpiGreen2">—</div><div class="cohort-lbl">On track</div></div>\n'
'          <div class="cohort-item"><div class="cohort-val" style="color:var(--yellow-600)" id="kpiYellow2">—</div><div class="cohort-lbl">At risk</div></div>\n'
'          <div class="cohort-item"><div class="cohort-val" style="color:var(--red-600)" id="kpiRed2">—</div><div class="cohort-lbl">Behind</div></div>\n'
'          <div class="cohort-item"><div class="cohort-val" id="cohortRIval">—</div><div class="cohort-lbl">R-Index avg</div></div>\n'
'        </div>\n'
'      </div>\n'
'    </div>\n'
'    <div class="learner-table-wrap">\n'
'      <div class="table-header">\n'
'        <div class="table-title"><i class="ti ti-users"></i> Learner progress <span class="table-hint">— click a row to see courses</span></div>\n'
'        <div class="filter-btns">\n'
'          <button class="filter-btn active" onclick="setStatusFilter(\'all\',this)">All</button>\n'
'          <button class="filter-btn" onclick="setStatusFilter(\'green\',this)">On track</button>\n'
'          <button class="filter-btn" onclick="setStatusFilter(\'yellow\',this)">At risk</button>\n'
'          <button class="filter-btn" onclick="setStatusFilter(\'red\',this)">Behind</button>\n'
'        </div>\n'
'      </div>\n'
'      <table><thead><tr>\n'
'        <th style="width:22%">Learner</th>\n'
'        <th style="width:18%">Progress</th>\n'
'        <th style="width:8%"><span class="tip">vs EP<span class="tip-icon">?</span><span class="tip-box">Actual vs expected pace (10%/day)</span></span></th>\n'
'        <th style="width:8%">Day</th>\n'
'        <th style="width:10%"><span class="tip">Quiz avg<span class="tip-icon">?</span><span class="tip-box">Avg score across attempted courses</span></span></th>\n'
'        <th style="width:13%">Last active</th>\n'
'        <th style="width:8%"><span class="tip">R-Index<span class="tip-icon">?</span><span class="tip-box">Progress 45% · Login 35% · Quiz 20%</span></span></th>\n'
'        <th style="width:8%">Status</th>\n'
'      </tr></thead>\n'
'      <tbody id="tbodyActive"></tbody></table>\n'
'    </div>\n'
'  </div>\n'
'  <div id="tab-historical" class="tab-content">\n'
'    <div class="learner-table-wrap" style="margin-top:8px;">\n'
'      <div class="table-header"><div class="table-title"><i class="ti ti-trophy"></i> Completed learners — all ' + str(TOTAL_COURSES) + ' courses done</div></div>\n'
'      <table><thead><tr>\n'
'        <th style="width:35%">Learner</th><th style="width:20%">Started</th><th style="width:20%">Quiz avg</th><th style="width:25%">Status</th>\n'
'      </tr></thead><tbody id="tbodyHistorical"></tbody></table>\n'
'    </div>\n'
'  </div>\n'
'  <div id="tab-not_started" class="tab-content">\n'
'    <div class="learner-table-wrap" style="margin-top:8px;">\n'
'      <div class="table-header"><div class="table-title"><i class="ti ti-user-plus"></i> Not yet started</div></div>\n'
'      <table><thead><tr>\n'
'        <th style="width:35%">Learner</th><th style="width:45%">Email</th><th style="width:20%">Status</th>\n'
'      </tr></thead><tbody id="tbodyNotStarted"></tbody></table>\n'
'    </div>\n'
'  </div>\n'
'  <div class="footer">SuccessKPI Enablement Command Center &nbsp;&middot;&nbsp; Refreshed ' + generated_at + ' &nbsp;&middot;&nbsp; ' + str(len(all_learners)) + ' total learners &nbsp;&middot;&nbsp; ' + str(TOTAL_COURSES) + ' courses</div>\n'
'</div>\n'
'</div>\n<div class="modal-overlay" id="modalOverlay">\n'
'  <div class="modal">\n'
'    <div class="modal-header">\n'
'      <div class="modal-name" id="modalName">Learner detail</div>\n'
'      <button class="close-btn" onclick="closeModal()"><i class="ti ti-x"></i></button>\n'
'    </div>\n'
'    <div class="modal-stats">\n'
'      <div class="modal-stat"><div class="mv" id="mProgress">—</div><div class="ml">Progress</div></div>\n'
'      <div class="modal-stat"><div class="mv" id="mEP">—</div><div class="ml"><span class="tip">vs EP <span class="tip-icon">?</span><span class="tip-box">Actual vs expected pace</span></span></div></div>\n'
'      <div class="modal-stat"><div class="mv" id="mLogin">—</div><div class="ml">Last active</div></div>\n'
'      <div class="modal-stat"><div class="mv" id="mRI">—</div><div class="ml"><span class="tip">R-Index <span class="tip-icon">?</span><span class="tip-box">Progress 45% · Login 35% · Quiz 20%</span></span></div></div>\n'
'    </div>\n'
'    <div class="ri-factors">\n'
'      <div class="ri-factor"><div class="rv" id="riProg">—</div><div class="rl">Progress score</div></div>\n'
'      <div class="ri-factor"><div class="rv" id="riLogin">—</div><div class="rl">Login score</div></div>\n'
'      <div class="ri-factor"><div class="rv" id="riQuiz">—</div><div class="rl">Quiz score</div></div>\n'
'    </div>\n'
'    <div class="modal-section-label">All courses (' + str(TOTAL_COURSES) + ' total)</div>\n'
'    <div id="modalCourses"></div>\n'
'  </div>\n'
'</div>\n'
'<script>\n' + js + '\n</script>\n'
'</body>\n</html>\n'
)

with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"\nDone! Open with: open {OUTPUT_FILE}")

try:
    if not hasattr(config, 'EMAIL_FROM'):
        print("No email config, skipping.")
    else:
        print("Sending email...")
        active_all = [l for l in all_learners if l['bucket'] == 'active' and l['daysStarted'] <= 30]
        red    = [l for l in active_all if l['status'] == 'red']
        yellow = [l for l in active_all if l['status'] == 'yellow']
        green  = [l for l in active_all if l['status'] == 'green']

        def make_rows(ll):
            if not ll: return "<tr><td colspan='4' style='padding:12px;color:#888;'>None today.</td></tr>"
            return "".join([f"<tr><td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;'>{l['name']}</td><td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;'>{l['progress']}% ({l['completedCount']}/{l['totalCourses']})</td><td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;'>{l['lastLoginLabel']}</td><td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;'>{l['statusReason']}</td></tr>" for l in ll])

        email_html = f"""<html><body style="font-family:Inter,sans-serif;background:#f2f4f8;margin:0;padding:24px;">
<div style="max-width:600px;margin:0 auto;">
  <div style="background:#032169;border-radius:12px 12px 0 0;padding:20px 24px;">
    <div style="color:#fff;font-size:18px;font-weight:600;">Enablement Command Center</div>
    <div style="color:#30bced;font-size:12px;margin-top:4px;">Daily Summary · {generated_at}</div>
  </div>
  <div style="background:#fff;padding:20px 24px;border:1px solid #d4d4da;border-top:none;">
    <div style="display:flex;gap:12px;margin-bottom:20px;">
      <div style="flex:1;background:#f2f4f8;border-radius:8px;padding:14px;text-align:center;"><div style="font-size:24px;font-weight:600;">{len(active_all)}</div><div style="font-size:11px;color:#4a5256;margin-top:4px;text-transform:uppercase;">Active</div></div>
      <div style="flex:1;background:#E8F5E9;border-radius:8px;padding:14px;text-align:center;"><div style="font-size:24px;font-weight:600;color:#2E7D32;">{len(green)}</div><div style="font-size:11px;color:#2E7D32;margin-top:4px;text-transform:uppercase;">On track</div></div>
      <div style="flex:1;background:#FFF8E1;border-radius:8px;padding:14px;text-align:center;"><div style="font-size:24px;font-weight:600;color:#E65100;">{len(yellow)}</div><div style="font-size:11px;color:#E65100;margin-top:4px;text-transform:uppercase;">At risk</div></div>
      <div style="flex:1;background:#FFEBEE;border-radius:8px;padding:14px;text-align:center;"><div style="font-size:24px;font-weight:600;color:#C62828;">{len(red)}</div><div style="font-size:11px;color:#C62828;margin-top:4px;text-transform:uppercase;">Behind</div></div>
    </div>
    <div style="margin-bottom:20px;"><div style="font-size:13px;font-weight:600;margin-bottom:10px;">🔴 Behind</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;"><thead><tr style="background:#f2f4f8;"><th style="padding:8px 12px;text-align:left;">Name</th><th style="padding:8px 12px;text-align:left;">Progress</th><th style="padding:8px 12px;text-align:left;">Last active</th><th style="padding:8px 12px;text-align:left;">Reason</th></tr></thead><tbody>{make_rows(red)}</tbody></table></div>
    <div><div style="font-size:13px;font-weight:600;margin-bottom:10px;">🟡 At risk</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;"><thead><tr style="background:#f2f4f8;"><th style="padding:8px 12px;text-align:left;">Name</th><th style="padding:8px 12px;text-align:left;">Progress</th><th style="padding:8px 12px;text-align:left;">Last active</th><th style="padding:8px 12px;text-align:left;">Reason</th></tr></thead><tbody>{make_rows(yellow)}</tbody></table></div>
  </div>
  <div style="background:#f2f4f8;border-radius:0 0 12px 12px;padding:12px 24px;border:1px solid #d4d4da;border-top:none;text-align:center;"><div style="font-size:11px;color:#a7a9ab;">SuccessKPI Enablement Command Center · Auto-generated</div></div>
</div></body></html>"""

        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Daily Summary — {len(red)} behind, {len(yellow)} at risk · {datetime.now().strftime('%b %d')}"
        msg['From']    = config.EMAIL_FROM
        msg['To']      = config.EMAIL_TO
        msg.attach(MIMEText(email_html, 'html'))
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(config.EMAIL_FROM, config.EMAIL_PASSWORD)
            server.sendmail(config.EMAIL_FROM, config.EMAIL_TO, msg.as_string())
        print("Email sent!")
except Exception as e:
    print(f"Email skipped: {e}")