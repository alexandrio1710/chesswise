"""
Endpoints behind the Insights, Skills and Opponents pages.

Every one of them takes the same filter bar (site, time class, color, date range,
profile) and turns it into an insights.Filters via insights_filters() — so the
validation lives in one place. Kept out of server.py, which had grown past 1,700
lines; server.py includes this router.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

import auth
import geography
import insights
import insights_report
import skills

router = APIRouter()


def normalize_source(source: str | None) -> str | None:
    return None if source in (None, "", "all") else source


# Insights page (insights_report.py): one filter bar drives every section.
TIME_CLASSES = {"bullet", "blitz", "rapid", "classical", "daily"}
RANGES = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "1y": 365}


def insights_filters(source, profile_id, time_class, color, date_range, date_from, date_to) -> insights.Filters:
    if time_class and time_class not in TIME_CLASSES:
        raise HTTPException(status_code=422, detail=f"Unknown time class: {time_class}")
    if color and color not in ("white", "black"):
        raise HTTPException(status_code=422, detail=f"Unknown color: {color}")
    if date_range and date_range != "all" and date_range not in RANGES:
        raise HTTPException(status_code=422, detail=f"Unknown range: {date_range}")
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{label} must be YYYY-MM-DD")
    if not date_from and date_range in RANGES:
        date_from = (datetime.now(timezone.utc) - timedelta(days=RANGES[date_range])).strftime("%Y-%m-%d")
    return insights.Filters(source=normalize_source(source), profile_id=profile_id, time_class=time_class or None,
                            color=color or None, date_from=date_from or None, date_to=date_to or None)


@router.get("/api/insights/report")
def api_insights_report(
    source: str | None = Query(default=None), time_class: str | None = Query(default=None), color: str | None = Query(default=None),
    range: str | None = Query(default=None), date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
    tz_offset: int = Query(default=0, ge=-840, le=840),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
):
    """Every Insights section for the filtered games (see insights_report.py).
    `tz_offset` is the browser's Date.getTimezoneOffset(), so the calendar
    sections are in the viewer's own time rather than UTC."""
    flt = insights_filters(source, profile_id, time_class, color, range, date_from, date_to)
    return insights_report.build_report(flt, tz_offset)


@router.get("/api/insights/geography")
def api_insights_geography(
    source: str | None = Query(default=None), time_class: str | None = Query(default=None), color: str | None = Query(default=None),
    range: str | None = Query(default=None), date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
):
    """Results by opponent country (from the cached profile lookups) and the
    most frequent opponents, for the filtered games."""
    flt = insights_filters(source, profile_id, time_class, color, range, date_from, date_to)
    return geography.report(flt)


@router.post("/api/insights/geography/start")
def api_insights_geography_start(
    source: str | None = Query(default=None), time_class: str | None = Query(default=None), color: str | None = Query(default=None),
    range: str | None = Query(default=None), date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
):
    """Look up the country of every not-yet-looked-up opponent in the filtered
    games, in the background (one small public-API request each). Poll
    GET /api/insights/geography for progress."""
    flt = insights_filters(source, profile_id, time_class, color, range, date_from, date_to)
    return {"started": geography.start_lookup(flt)}


@router.get("/api/skills")
def api_skills(
    source: str | None = Query(default=None), time_class: str | None = Query(default=None), color: str | None = Query(default=None),
    range: str | None = Query(default=None), date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
):
    """Skills, mistake anatomy, opening leaks and the prioritized plan (see skills.py)."""
    flt = insights_filters(source, profile_id, time_class, color, range, date_from, date_to)
    return skills.build(flt)


@router.get("/api/skills/examples")
def api_skills_examples(
    cause: str = Query(...), limit: int = Query(default=6, ge=1, le=12),
    source: str | None = Query(default=None), time_class: str | None = Query(default=None), color: str | None = Query(default=None),
    range: str | None = Query(default=None), date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
):
    """Example positions behind one mistake cause."""
    if cause not in skills.CAUSES:
        raise HTTPException(status_code=422, detail=f"Unknown cause: {cause}")
    flt = insights_filters(source, profile_id, time_class, color, range, date_from, date_to)
    return {"examples": skills.mistake_examples(flt, cause, limit)}


@router.get("/api/insights/tactic-examples")
def api_insights_tactic_examples(
    motif: str = Query(...), side: str = Query(...), outcome: str = Query(...), limit: int = Query(default=4, ge=1, le=12),
    source: str | None = Query(default=None), time_class: str | None = Query(default=None), color: str | None = Query(default=None),
    range: str | None = Query(default=None), date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
    profile_id: int | None = Depends(auth.require_profile_filter_access),
):
    """Example positions behind one tactics count (e.g. forks you missed)."""
    if side not in ("you", "opp"):
        raise HTTPException(status_code=422, detail="side must be 'you' or 'opp'")
    flt = insights_filters(source, profile_id, time_class, color, range, date_from, date_to)
    return {"examples": insights_report.tactic_examples(flt, motif, side, outcome, limit)}
