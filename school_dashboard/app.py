import os
import sqlite3
import asyncio
from datetime import datetime, time, timedelta

from flask import Flask, render_template, request, redirect, url_for, jsonify
from dotenv import load_dotenv
from neispy import Neispy
from zoneinfo import ZoneInfo

# -------------------------------------------------------------
# 기본 설정
# -------------------------------------------------------------
load_dotenv()

app = Flask(__name__)

NEIS_API_KEY = os.getenv("NEIS_API_KEY")
SCHOOL_NAME  = os.getenv("SCHOOL_NAME")
GRADE        = os.getenv("GRADE")      # 문자열로 사용
CLASS_NM     = os.getenv("CLASS")
SEMESTER     = int(os.getenv("SEMESTER", "1"))

DB_PATH = "data.db"

KST = ZoneInfo("Asia/Seoul")


def now_kst():
    return datetime.now(KST)


# -------------------------------------------------------------
# SQLite 초기화 (수행평가)
# -------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject    TEXT NOT NULL,
            title      TEXT NOT NULL,
            due_date   TEXT,
            detail     TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def add_assessment(subject, title, due_date, detail):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO assessments (subject, title, due_date, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (subject, title, due_date, detail, now_kst().isoformat()),
    )
    conn.commit()
    conn.close()


def get_assessments():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        SELECT id, subject, title, due_date, detail, created_at
        FROM assessments
        ORDER BY due_date
        """
    )
    rows = c.fetchall()
    conn.close()
    return rows


# -------------------------------------------------------------
# NEIS 비동기 호출 부분 (Neispy 4.x 기준)
# -------------------------------------------------------------
# 1) 학교 코드 조회 (AE, SE)
async def _async_get_school_codes():
    async with Neispy(KEY=NEIS_API_KEY) as neis:
        scinfo = await neis.schoolInfo(SCHUL_NM=SCHOOL_NAME)
        # 구조: scinfo.schoolInfo[1].row[0]
        row = scinfo.schoolInfo[1].row[0]
        ae = row.ATPT_OFCDC_SC_CODE  # 교육청 코드
        se = row.SD_SCHUL_CODE       # 학교 코드
        return ae, se


AE, SE = asyncio.run(_async_get_school_codes())


# 2) 특정 날짜 급식 (yyyyMMdd 문자열)
async def _async_meal_for_ymd(ae, se, ymd_str: str):
    async with Neispy(KEY=NEIS_API_KEY) as neis:
        scmeal = await neis.mealServiceDietInfo(
            ATPT_OFCDC_SC_CODE=ae,
            SD_SCHUL_CODE=se,
            MLSV_YMD=ymd_str,
        )
        # 구조: scmeal.mealServiceDietInfo[1].row
        rows = scmeal.mealServiceDietInfo[1].row
        if not rows:
            return []
        row = rows[0]
        raw = row.DDISH_NM  # "밥(1.2.)<br/>국(5.6.)..."
        return [d.strip() for d in raw.split("<br/>")]


def get_today_meal():
    ymd = now_kst().strftime("%Y%m%d")
    try:
        return asyncio.run(_async_meal_for_ymd(AE, SE, ymd))
    except Exception as e:
        print("meal error:", e)
        return []


def get_week_meals():
    """이번 주(월~금) 급식"""
    result = []
    week_days = get_week_dates()
    weekday_kor = ["월", "화", "수", "목", "금", "토", "일"]

    for d in week_days:
        ymd = d.strftime("%Y%m%d")
        try:
            dishes = asyncio.run(_async_meal_for_ymd(AE, SE, ymd))
        except Exception as e:
            print("week meal error:", d, e)
            dishes = []

        result.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "weekday": weekday_kor[d.weekday()],
                "dishes": dishes,
            }
        )
    return result


# 3) 특정 날짜 시간표 (고등학교 hisTimetable)
async def _async_timetable_for_date(ae, se, year, semester, ymd_int, grade, class_nm):
    async with Neispy(KEY=NEIS_API_KEY) as neis:
        sctimetable = await neis.hisTimetable(
            ATPT_OFCDC_SC_CODE=ae,
            SD_SCHUL_CODE=se,
            AY=str(year),
            SEM=str(semester),
            ALL_TI_YMD=ymd_int,
            GRADE=str(grade),
            CLASS_NM=str(class_nm),
        )
        # 구조: sctimetable.hisTimetable[1].row
        rows = sctimetable.hisTimetable[1].row
        result = []
        for r in rows:
            period = int(r.PERIO)
            subject = r.ITRT_CNTNT
            result.append((period, subject))
        result.sort(key=lambda x: x[0])
        return result


def get_today_timetable():
    today = now_kst()
    ymd_int = int(today.strftime("%Y%m%d"))
    year = today.year
    try:
        return asyncio.run(
            _async_timetable_for_date(
                AE, SE, year, SEMESTER, ymd_int, GRADE, CLASS_NM
            )
        )
    except Exception as e:
        print("timetable error:", e)
        return []


def get_week_timetable():
    """이번 주(월~금) 날짜별 시간표"""
    result = []
    week_days = get_week_dates()
    weekday_kor = ["월", "화", "수", "목", "금", "토", "일"]

    for d in week_days:
        ymd_int = int(d.strftime("%Y%m%d"))
        year = d.year
        try:
            rows = asyncio.run(
                _async_timetable_for_date(
                    AE, SE, year, SEMESTER, ymd_int, GRADE, CLASS_NM
                )
            )
        except Exception as e:
            print("week timetable error:", d, e)
            rows = []

        result.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "weekday": weekday_kor[d.weekday()],
                "rows": rows,  # [(period, subject), ...]
            }
        )
    return result


# -------------------------------------------------------------
# 날짜/교시 계산
# -------------------------------------------------------------
def get_week_dates():
    """이번 주 월~금 날짜 리스트"""
    today = now_kst().date()
    monday = today - timedelta(days=today.weekday())  # 월요일(0)
    return [monday + timedelta(days=i) for i in range(5)]


PERIOD_TIMES = [
    (1, time(8, 40), time(9, 30)),
    (2, time(9, 40), time(10, 30)),
    (3, time(10, 40), time(11, 30)),
    (4, time(11, 40), time(12, 30)),
    (5, time(13, 30), time(14, 20)),
    (6, time(14, 30), time(15, 20)),
    (7, time(15, 30), time(16, 20)),
]


def get_current_and_next_period():
    now = now_kst().time()
    current = None
    next_p = None

    for idx, (p, start, end) in enumerate(PERIOD_TIMES):
        if start <= now <= end:
            current = p
            if idx + 1 < len(PERIOD_TIMES):
                next_p = PERIOD_TIMES[idx + 1][0]
            return current, next_p

        if now < start:
            return None, p  # 아직 수업 전

    # 모든 수업 끝난 뒤
    return None, None


# -------------------------------------------------------------
# Flask Routes
# -------------------------------------------------------------
@app.route("/")
def index():
    today = now_kst()
    weekday_kor = ["월", "화", "수", "목", "금", "토", "일"][today.weekday()]

    meal_list = get_today_meal()
    timetable = get_today_timetable()
    curr, nextp = get_current_and_next_period()

    week_meals = get_week_meals()
    week_timetable = get_week_timetable()

    return render_template(
        "index.html",
        today_date=today.strftime("%Y-%m-%d"),
        weekday=weekday_kor,
        meal_list=meal_list,
        timetable=timetable,
        current_period=curr,
        next_period=nextp,
        week_meals=week_meals,
        week_timetable=week_timetable,
    )


@app.route("/assess", methods=["GET", "POST"])
def assess():
    if request.method == "POST":
        add_assessment(
            request.form.get("subject"),
            request.form.get("title"),
            request.form.get("due_date"),
            request.form.get("detail"),
        )
        return redirect(url_for("assess"))

    asses = get_assessments()
    return render_template("assess.html", assessments=asses)


@app.route("/api/assess")
def api_assess():
    rows = get_assessments()
    return jsonify(
        [
            {
                "id": r[0],
                "subject": r[1],
                "title": r[2],
                "due_date": r[3],
                "detail": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]
    )


if __name__ == "__main__":
    app.run(debug=True)
