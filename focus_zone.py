from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from sqlalchemy.orm import Session

from database import get_db, User, StudyMaterial

# Share the main app's Base/engine if exposed, otherwise create a local one
# bound to the SAME SQLite file so everything lives together.
try:
    from database import Base, engine  # preferred
except Exception:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import declarative_base
    Base = declarative_base()
    engine = create_engine(
        "sqlite:///./brainbuddy.db",
        connect_args={"check_same_thread": False},
    )

# ============================================================
# MODELS
# ============================================================
class StudySession(Base):
    __tablename__ = "study_sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    study_material_id = Column(Integer, ForeignKey("study_materials.id"), nullable=True)
    timer_mode = Column(String(20), nullable=False)        # pomodoro | stopwatch | custom
    topic = Column(String(200), nullable=True)
    status = Column(String(20), default="running", nullable=False)  # running|paused|completed|abandoned
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    focused_seconds = Column(Integer, default=0, nullable=False)
    planned_seconds = Column(Integer, default=0, nullable=False)   # 0 for stopwatch
    accumulated_seconds = Column(Integer, default=0, nullable=False)  # confirmed completed segments
    last_resumed_at = Column(DateTime, nullable=True)  # start of current running segment
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class StudyGoal(Base):
    __tablename__ = "study_goals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    daily_goal_minutes = Column(Integer, default=120, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# Idempotent — only creates missing tables
try:
    Base.metadata.create_all(engine)
except Exception as e:
    print(f"[FocusZone] Table creation warning: {e}")


# ============================================================
# AUTH HELPERS (replicates main.py cookie auth, no side effects)
# ============================================================
def _current_user(request: Request, db: Session) -> Optional[User]:
    uid = request.cookies.get("user_id")
    if not uid:
        return None
    try:
        return db.query(User).filter(User.id == int(uid)).first()
    except Exception:
        return None


def _require_user(request: Request, db: Session) -> User:
    u = _current_user(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="Login required")
    return u


# ============================================================
# TIME + SAFETY HELPERS
# ============================================================
MAX_STOPWATCH_SECONDS = 8 * 3600
POMODORO_FOCUS_SECONDS = 25 * 60
CUSTOM_MIN_MIN = 1
CUSTOM_MAX_MIN = 240


def _now() -> datetime:
    return datetime.utcnow()


def _live_focused_seconds(s: StudySession) -> int:
    """Authoritative server-side focused-seconds calculation."""
    acc = s.accumulated_seconds or 0
    if s.status == "running" and s.last_resumed_at:
        delta = (_now() - s.last_resumed_at).total_seconds()
        if delta > 0:
            acc += int(delta)
    if s.timer_mode in ("pomodoro", "custom") and s.planned_seconds > 0:
        acc = min(acc, s.planned_seconds)
    if s.timer_mode == "stopwatch":
        acc = min(acc, MAX_STOPWATCH_SECONDS)
    return max(0, int(acc))


def _day_range_utc(tz_offset_minutes: int):
    """Return UTC start/end datetimes for the user's local calendar day.
    tz_offset_minutes follows JS getTimezoneOffset convention
    (UTC - local, in minutes; e.g. IST = -330)."""
    now_utc = _now()
    local_now = now_utc - timedelta(minutes=tz_offset_minutes)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = local_start + timedelta(minutes=tz_offset_minutes)
    return utc_start, utc_start + timedelta(days=1)


def _serialize(s: StudySession) -> dict:
    return {
        "id": s.id,
        "mode": s.timer_mode,
        "topic": s.topic or "",
        "status": s.status,
        "planned_seconds": s.planned_seconds,
        "accumulated_seconds": s.accumulated_seconds,
        "last_resumed_at": (s.last_resumed_at.isoformat() + "Z") if s.last_resumed_at else None,
        "started_at": (s.started_at.isoformat() + "Z") if s.started_at else None,
        "server_now": _now().isoformat() + "Z",
        "focused_seconds": (
            _live_focused_seconds(s) if s.status != "completed" else (s.focused_seconds or 0)
        ),
        "study_material_id": s.study_material_id,
    }


# ============================================================
# ROUTER
# ============================================================
focus_router = APIRouter(prefix="/focus-zone", tags=["focus-zone"])
_tmpl = Jinja2Templates(directory="templates")


# --------- HTML page ---------
@focus_router.get("", response_class=HTMLResponse)
@focus_router.get("/", response_class=HTMLResponse)
def focus_zone_page(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    materials = (
        db.query(StudyMaterial)
        .filter(StudyMaterial.user_id == user.id)
        .order_by(StudyMaterial.id.desc())
        .all()
    )
    goal = db.query(StudyGoal).filter(StudyGoal.user_id == user.id).first()
    goal_minutes = goal.daily_goal_minutes if goal else 120

    return _tmpl.TemplateResponse(
        request=request,
        name="focus_zone.html",
        context={
            "request": request,
            "user": user,
            "materials": materials,
            "goal_minutes": goal_minutes,
        },
    )


# --------- API: Active session ---------
@focus_router.get("/api/active")
def api_active(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    sess = (
        db.query(StudySession)
        .filter(
            StudySession.user_id == user.id,
            StudySession.status.in_(["running", "paused"]),
        )
        .order_by(StudySession.id.desc())
        .first()
    )
    return {"session": _serialize(sess) if sess else None}


# --------- API: Start ---------
@focus_router.post("/api/start")
def api_start(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    user = _require_user(request, db)

    mode = (payload.get("mode") or "").strip().lower()
    if mode not in ("pomodoro", "stopwatch", "custom"):
        raise HTTPException(400, "Invalid timer mode")

    topic = (payload.get("topic") or "").strip()[:200] or None

    material_id = None
    raw_mid = payload.get("material_id")
    if raw_mid not in (None, "", 0):
        try:
            mid = int(raw_mid)
            mat = (
                db.query(StudyMaterial)
                .filter(StudyMaterial.id == mid, StudyMaterial.user_id == user.id)
                .first()
            )
            if mat:
                material_id = mat.id
        except Exception:
            material_id = None

    try:
        planned_min = int(payload.get("planned_minutes", 0))
    except Exception:
        planned_min = 0

    if mode == "pomodoro":
        planned_seconds = POMODORO_FOCUS_SECONDS
    elif mode == "stopwatch":
        planned_seconds = 0
    else:
        if planned_min < CUSTOM_MIN_MIN or planned_min > CUSTOM_MAX_MIN:
            raise HTTPException(
                400, f"Custom duration must be {CUSTOM_MIN_MIN}-{CUSTOM_MAX_MIN} minutes"
            )
        planned_seconds = planned_min * 60

    # Auto-abandon any orphan active session
    orphans = (
        db.query(StudySession)
        .filter(
            StudySession.user_id == user.id,
            StudySession.status.in_(["running", "paused"]),
        )
        .all()
    )
    for o in orphans:
        o.status = "abandoned"
        o.focused_seconds = 0
        o.ended_at = _now()
        o.last_resumed_at = None
    if orphans:
        db.commit()

    now = _now()
    s = StudySession(
        user_id=user.id,
        study_material_id=material_id,
        timer_mode=mode,
        topic=topic,
        status="running",
        started_at=now,
        focused_seconds=0,
        planned_seconds=planned_seconds,
        accumulated_seconds=0,
        last_resumed_at=now,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"session": _serialize(s)}


# --------- API: Pause ---------
@focus_router.post("/api/pause/{session_id}")
def api_pause(session_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    s = (
        db.query(StudySession)
        .filter(StudySession.id == session_id, StudySession.user_id == user.id)
        .first()
    )
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status != "running":
        raise HTTPException(400, f"Cannot pause: status is {s.status}")

    if s.last_resumed_at:
        elapsed = int((_now() - s.last_resumed_at).total_seconds())
        if elapsed > 0:
            s.accumulated_seconds = (s.accumulated_seconds or 0) + elapsed
    s.last_resumed_at = None
    s.status = "paused"
    if s.timer_mode in ("pomodoro", "custom") and s.planned_seconds:
        s.accumulated_seconds = min(s.accumulated_seconds, s.planned_seconds)
    db.commit()
    db.refresh(s)
    return {"session": _serialize(s)}


# --------- API: Resume ---------
@focus_router.post("/api/resume/{session_id}")
def api_resume(session_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    s = (
        db.query(StudySession)
        .filter(StudySession.id == session_id, StudySession.user_id == user.id)
        .first()
    )
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status != "paused":
        raise HTTPException(400, f"Cannot resume: status is {s.status}")
    if s.timer_mode in ("pomodoro", "custom") and s.planned_seconds:
        if (s.accumulated_seconds or 0) >= s.planned_seconds:
            raise HTTPException(400, "Planned time already completed")
    s.status = "running"
    s.last_resumed_at = _now()
    db.commit()
    db.refresh(s)
    return {"session": _serialize(s)}


# --------- API: Complete (finish or natural end) ---------
@focus_router.post("/api/complete/{session_id}")
def api_complete(session_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    s = (
        db.query(StudySession)
        .filter(StudySession.id == session_id, StudySession.user_id == user.id)
        .first()
    )
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status == "completed":
        return {"session": _serialize(s)}  # idempotent
    if s.status == "abandoned":
        raise HTTPException(400, "Session was abandoned")

    final = _live_focused_seconds(s)
    s.accumulated_seconds = final
    s.focused_seconds = final
    s.status = "completed"
    s.ended_at = _now()
    s.last_resumed_at = None
    db.commit()
    db.refresh(s)
    return {"session": _serialize(s)}


# --------- API: Abandon (reset) ---------
@focus_router.post("/api/abandon/{session_id}")
def api_abandon(session_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    s = (
        db.query(StudySession)
        .filter(StudySession.id == session_id, StudySession.user_id == user.id)
        .first()
    )
    if not s:
        raise HTTPException(404, "Session not found")
    if s.status in ("completed", "abandoned"):
        return {"ok": True}
    s.status = "abandoned"
    s.focused_seconds = 0
    s.ended_at = _now()
    s.last_resumed_at = None
    db.commit()
    return {"ok": True}


# --------- API: Stats ---------
@focus_router.get("/api/stats")
def api_stats(request: Request, tz_offset: int = 0, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if tz_offset < -14 * 60 or tz_offset > 14 * 60:
        tz_offset = 0

    day_start, day_end = _day_range_utc(tz_offset)

    today_rows = (
        db.query(StudySession)
        .filter(
            StudySession.user_id == user.id,
            StudySession.status == "completed",
            StudySession.ended_at >= day_start,
            StudySession.ended_at < day_end,
        )
        .all()
    )
    today_seconds = sum((r.focused_seconds or 0) for r in today_rows)
    today_count = len(today_rows)
    avg_seconds = int(today_seconds / today_count) if today_count else 0

    # Last 7 local days
    week_days = []
    for i in range(6, -1, -1):
        d_start = day_start - timedelta(days=i)
        d_end = d_start + timedelta(days=1)
        secs = (
            db.query(func.coalesce(func.sum(StudySession.focused_seconds), 0))
            .filter(
                StudySession.user_id == user.id,
                StudySession.status == "completed",
                StudySession.ended_at >= d_start,
                StudySession.ended_at < d_end,
            )
            .scalar()
            or 0
        )
        local_label_date = d_start - timedelta(minutes=tz_offset)
        week_days.append(
            {
                "date": local_label_date.strftime("%a"),
                "iso_date": local_label_date.strftime("%Y-%m-%d"),
                "seconds": int(secs),
            }
        )

    week_total = sum(d["seconds"] for d in week_days)
    best_day = max(week_days, key=lambda d: d["seconds"]) if week_days else None
    non_zero_days = [d for d in week_days if d["seconds"] > 0]
    avg_daily = int(week_total / 7)

    goal = db.query(StudyGoal).filter(StudyGoal.user_id == user.id).first()
    goal_minutes = goal.daily_goal_minutes if goal else 120

    history_rows = (
        db.query(StudySession)
        .filter(
            StudySession.user_id == user.id,
            StudySession.status.in_(["completed", "abandoned"]),
        )
        .order_by(StudySession.id.desc())
        .limit(15)
        .all()
    )
    history = [
        {
            "id": h.id,
            "topic": h.topic or "—",
            "mode": h.timer_mode,
            "focused_seconds": h.focused_seconds or 0,
            "status": h.status,
            "ended_at": (h.ended_at.isoformat() + "Z") if h.ended_at else None,
        }
        for h in history_rows
    ]

    # Today's completed pomodoro focus count (for long-break logic on client)
    pomo_today = sum(1 for r in today_rows if r.timer_mode == "pomodoro")

    return {
        "today_seconds": today_seconds,
        "today_sessions": today_count,
        "avg_seconds": avg_seconds,
        "week_total": week_total,
        "week_days": week_days,
        "best_day": best_day,
        "avg_daily": avg_daily,
        "goal_minutes": goal_minutes,
        "history": history,
        "pomodoro_focus_today": pomo_today,
    }


# --------- API: Set goal ---------
@focus_router.post("/api/goal")
def api_set_goal(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    user = _require_user(request, db)
    try:
        minutes = int(payload.get("minutes", 0))
    except Exception:
        raise HTTPException(400, "Invalid minutes")
    if minutes < 15 or minutes > 1440:
        raise HTTPException(400, "Goal must be between 15 and 1440 minutes")

    goal = db.query(StudyGoal).filter(StudyGoal.user_id == user.id).first()
    if goal:
        goal.daily_goal_minutes = minutes
        goal.updated_at = _now()
    else:
        db.add(StudyGoal(user_id=user.id, daily_goal_minutes=minutes))
    db.commit()
    return {"ok": True, "minutes": minutes}
