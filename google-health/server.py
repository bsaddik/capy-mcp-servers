#!/usr/bin/env python3
"""
Read-only Google Health API MCP server for Coach, Benji's private health Captain.

Reads sleep, daily heart rate variability, daily resting heart rate, daily steps,
Active Zone Minutes and VO2 max, and computes Coachable Health Age (CHA).
Dates in and out are Australia/Melbourne calendar dates (YYYY-MM-DD, end date
inclusive).

Credentials: COACH_HEALTH_CREDENTIALS holds {"client_id", "client_secret",
"refresh_token"} from authorize.py. Access tokens live in memory only. Nothing
is logged: tokens and health values never reach stderr.

Python 3.9+ standard library only, so it runs from a raw URL with plain python3.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from math import log
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
]
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4/users/me/dataTypes"
MELBOURNE = ZoneInfo("Australia/Melbourne")
MAX_RANGE_DAYS = 92
ROLLUP_MAX_DAYS = 90

Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Any]]


def urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, Any]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class GoogleHealthError(RuntimeError):
    pass


def api_error(what: str, status: int, payload: Any) -> GoogleHealthError:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        detail = f"{err.get('status', '')} {err.get('message', '')}".strip()
    else:
        detail = str(err or "")
    if what == "token" and detail.startswith("invalid_grant"):
        detail += " (refresh token expired or revoked: re-run authorize.py)"
    if status == 403 and "scope" in detail.lower():
        detail += " (consent predates a newer scope: re-run authorize.py)"
    return GoogleHealthError(f"{what} request failed with HTTP {status}: {detail}".rstrip(": "))


class HealthClient:
    def __init__(self, credentials: Dict[str, str], transport: Transport = urllib_transport, clock: Callable[[], float] = time.time):
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not credentials.get(k)]
        if missing:
            raise GoogleHealthError(f"COACH_HEALTH_CREDENTIALS is missing {', '.join(missing)}")
        self._creds = credentials
        self._transport = transport
        self._clock = clock
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    def _token(self, force: bool = False) -> str:
        if not force and self._access_token and self._clock() < self._expires_at - 60:
            return self._access_token
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": self._creds["client_id"],
                "client_secret": self._creds["client_secret"],
                "refresh_token": self._creds["refresh_token"],
            }
        ).encode()
        status, payload = self._transport("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if status != 200 or not payload.get("access_token"):
            raise api_error("token", status, payload)
        self._access_token = payload["access_token"]
        self._expires_at = self._clock() + int(payload.get("expires_in", 3600))
        return self._access_token

    def _request(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token(force=attempt > 0)}", "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            status, payload = self._transport(method, url, headers, data)
            if status == 401 and attempt == 0:
                continue
            if status != 200:
                raise api_error("Google Health", status, payload)
            return payload
        raise AssertionError("unreachable")

    def list_points(self, data_type: str, filter_expr: str) -> List[Dict[str, Any]]:
        points: List[Dict[str, Any]] = []
        page_token = ""
        while True:
            query = {"filter": filter_expr}
            if page_token:
                query["pageToken"] = page_token
            payload = self._request("GET", f"{API_BASE}/{data_type}/dataPoints?{urllib.parse.urlencode(query)}")
            points.extend(payload.get("dataPoints", []))
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                return points

    def daily_rollup(self, data_type: str, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "range": {"start": {"date": civil_date(start)}, "end": {"date": civil_date(end + dt.timedelta(days=1))}},
            "windowSizeDays": 1,
            "pageSize": ROLLUP_MAX_DAYS,
        }
        points: List[Dict[str, Any]] = []
        while True:
            payload = self._request("POST", f"{API_BASE}/{data_type}/dataPoints:dailyRollUp", body)
            points.extend(payload.get("rollupDataPoints", []))
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                return points
            body["pageToken"] = page_token


def parse_date(value: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a YYYY-MM-DD date, got {value!r}") from None


def parse_range(start_date: str, end_date: str, max_days: int = MAX_RANGE_DAYS) -> Tuple[dt.date, dt.date]:
    start, end = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if end < start:
        raise ValueError("end_date is before start_date")
    if (end - start).days >= max_days:
        raise ValueError(f"range is longer than {max_days} days")
    return start, end


def melbourne_midnight_utc(day: dt.date) -> str:
    local = dt.datetime.combine(day, dt.time(), tzinfo=MELBOURNE)
    return local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: str) -> dt.datetime:
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$", value)
    if not m:
        raise ValueError(f"unexpected timestamp format {value!r}")
    zone = "+00:00" if m.group(2) == "Z" else m.group(2)
    return dt.datetime.fromisoformat(m.group(1) + zone)


def to_melbourne(value: str) -> dt.datetime:
    return parse_timestamp(value).astimezone(MELBOURNE)


def api_date(value: Dict[str, int]) -> str:
    return dt.date(value["year"], value["month"], value["day"]).isoformat()


def civil_date(day: dt.date) -> Dict[str, int]:
    return {"year": day.year, "month": day.month, "day": day.day}


def as_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def daily_filter(field: str, start: dt.date, end: dt.date) -> str:
    after_end = end + dt.timedelta(days=1)
    return f'{field}.date >= "{start.isoformat()}" AND {field}.date < "{after_end.isoformat()}"'


# Coachable Health Age (CHA), Lee et al. 2026, arXiv 2611.08568, section 4.1.
# Pure functions, no I/O. Each domain maps a window mean to log(HR) with the
# paper's piecewise equations; Eq. (1) sums w * log(HR) / G into years. Core
# domains (Eqs. 3-6) carry the WEAR-ME collinearity weights (Eq. 8). Optional
# domains carry weight 1.0 and count only when their inputs exist: VO2 max
# (Eqs. 10-11) and sleep regularity (Eq. 12). Insulin resistance (Eq. 13) is
# omitted: no wearable prediction is available. Health Age floors at 18.0.

CHA_G = 0.1
CHA_HEALTH_AGE_FLOOR = 18.0
CHA_POUNDS_PER_KG = 2.20462
CHA_WEIGHTS = {"sleep": 0.8790, "resting_heart_rate": 0.7066, "active_zone_minutes": 0.6950, "steps": 0.4773}
CHA_OPTIONAL_WEIGHT = 1.0


@dataclass(frozen=True)
class ChaInputs:
    chronological_age: float
    resting_heart_rate_bpm: float
    steps_per_day: float
    sleep_hours_per_night: float
    active_zone_minutes_per_week: float
    vo2_max: Optional[float] = None
    sex: Optional[str] = None
    weight_kg: Optional[float] = None
    sleep_midpoint_sd_minutes: Optional[float] = None


def log_hr_resting_heart_rate(rhr: float) -> float:
    if rhr <= 60:
        return 0.0
    if rhr <= 70:
        return (rhr - 60) / 10 * log(1.08)
    return log(1.08) + (rhr - 70) / 20 * (log(1.33) - log(1.08))


def log_hr_steps(steps: float) -> float:
    return (min(steps, 9000) - 8000) / 1000 * log(0.88)


def log_hr_sleep(hours: float) -> float:
    if hours < 6.0:
        return (6.0 - hours) * log(1.20)
    if hours <= 9.0:
        return 0.0
    return (hours - 9.0) * log(1.34)


def log_hr_active_zone_minutes(azm: float) -> float:
    if azm < 115:
        return 0.0
    if azm < 245:
        return (azm - 115) / 130 * log(0.76)
    if azm < 418:
        return log(0.76) + (azm - 245) / 173 * (log(0.71) - log(0.76))
    if azm < 500:
        return log(0.71) + (azm - 418) / 82 * (log(0.68) - log(0.71))
    return log(0.68)


def expected_vo2_max(age: float, female: bool, weight_lb: float) -> float:
    return 79.9 - 0.39 * age - 13.7 * (1 if female else 0) - 0.127 * weight_lb


def log_hr_vo2_max(measured: float, age: float, female: bool, weight_kg: float) -> float:
    return (measured - expected_vo2_max(age, female, weight_kg * CHA_POUNDS_PER_KG)) / 3.5 * log(0.88)


def log_hr_sleep_regularity(sd_minutes: float) -> float:
    sd = min(sd_minutes, 120)
    if sd <= 30:
        return 0.0
    if sd <= 60:
        return (sd - 30) / 30 * log(1.16)
    if sd <= 90:
        return log(1.16) + (sd - 60) / 30 * (log(1.52) - log(1.16))
    return log(1.52) + (sd - 90) / 30 * (log(2.11) - log(1.52))


def parse_sex(sex: Optional[str]) -> Optional[bool]:
    if sex is None:
        return None
    value = sex.strip().lower()
    if value in ("female", "f"):
        return True
    if value in ("male", "m"):
        return False
    raise ValueError(f"sex must be 'female' or 'male', got {sex!r}")


def coachable_health_age(inputs: ChaInputs) -> Dict[str, object]:
    log_hrs = {
        "resting_heart_rate": (CHA_WEIGHTS["resting_heart_rate"], log_hr_resting_heart_rate(inputs.resting_heart_rate_bpm)),
        "steps": (CHA_WEIGHTS["steps"], log_hr_steps(inputs.steps_per_day)),
        "sleep": (CHA_WEIGHTS["sleep"], log_hr_sleep(inputs.sleep_hours_per_night)),
        "active_zone_minutes": (CHA_WEIGHTS["active_zone_minutes"], log_hr_active_zone_minutes(inputs.active_zone_minutes_per_week)),
    }
    female = parse_sex(inputs.sex)
    omitted = {"insulin_resistance": "no wearable insulin-resistance prediction is available"}
    if inputs.vo2_max is not None and female is not None and inputs.weight_kg is not None:
        log_hrs["vo2_max"] = (CHA_OPTIONAL_WEIGHT, log_hr_vo2_max(inputs.vo2_max, inputs.chronological_age, female, inputs.weight_kg))
    else:
        omitted["vo2_max"] = "needs VO2 max, sex and weight"
    if inputs.sleep_midpoint_sd_minutes is not None:
        log_hrs["sleep_regularity"] = (CHA_OPTIONAL_WEIGHT, log_hr_sleep_regularity(inputs.sleep_midpoint_sd_minutes))
    else:
        omitted["sleep_regularity"] = "needs sleep midpoint standard deviation"

    domains = {name: {"weight": w, "log_hr": lhr, "years": w * lhr / CHA_G} for name, (w, lhr) in log_hrs.items()}
    delta = sum(d["years"] for d in domains.values())
    raw_health_age = inputs.chronological_age + delta
    health_age = max(raw_health_age, CHA_HEALTH_AGE_FLOOR)
    return {
        "chronological_age": inputs.chronological_age,
        "health_age": health_age,
        "delta_years": health_age - inputs.chronological_age,
        "floored": raw_health_age < CHA_HEALTH_AGE_FLOOR,
        "domains": domains,
        "omitted_domains": omitted,
    }


def get_sleep(client: HealthClient, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    start, end = parse_range(start_date, end_date)
    lower = melbourne_midnight_utc(start)
    upper = melbourne_midnight_utc(end + dt.timedelta(days=1))
    points = client.list_points("sleep", f'sleep.interval.end_time >= "{lower}" AND sleep.interval.end_time < "{upper}"')
    sessions = []
    for point in points:
        sleep = point["sleep"]
        began, ended = to_melbourne(sleep["interval"]["startTime"]), to_melbourne(sleep["interval"]["endTime"])
        summary = sleep.get("summary", {})
        meta = sleep.get("metadata", {})
        sessions.append(
            {
                "date": ended.date().isoformat(),
                "start": began.isoformat(timespec="minutes"),
                "end": ended.isoformat(timespec="minutes"),
                "midpoint": (began + (ended - began) / 2).isoformat(timespec="minutes"),
                "main_sleep": bool(meta.get("mainSleep")),
                "nap": bool(meta.get("nap")),
                "minutes_asleep": as_int(summary.get("minutesAsleep")),
                "minutes_awake": as_int(summary.get("minutesAwake")),
                "minutes_in_sleep_period": as_int(summary.get("minutesInSleepPeriod")),
                "minutes_to_fall_asleep": as_int(summary.get("minutesToFallAsleep")),
                "stage_minutes": {s["type"].lower(): as_int(s.get("minutes")) for s in summary.get("stagesSummary", []) if s.get("type")},
            }
        )
    return sorted(sessions, key=lambda s: s["start"])


def get_hrv(client: HealthClient, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    start, end = parse_range(start_date, end_date)
    points = client.list_points("daily-heart-rate-variability", daily_filter("daily_heart_rate_variability", start, end))
    days = []
    for point in points:
        hrv = point["dailyHeartRateVariability"]
        days.append(
            {
                "date": api_date(hrv["date"]),
                "average_rmssd_ms": hrv.get("averageHeartRateVariabilityMilliseconds"),
                "deep_sleep_rmssd_ms": hrv.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds"),
                "non_rem_heart_rate_bpm": as_int(hrv.get("nonRemHeartRateBeatsPerMinute")),
                "entropy": hrv.get("entropy"),
            }
        )
    return sorted(days, key=lambda d: d["date"])


def get_resting_heart_rate(client: HealthClient, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    start, end = parse_range(start_date, end_date)
    points = client.list_points("daily-resting-heart-rate", daily_filter("daily_resting_heart_rate", start, end))
    days = []
    for point in points:
        rhr = point["dailyRestingHeartRate"]
        days.append(
            {
                "date": api_date(rhr["date"]),
                "resting_heart_rate_bpm": as_int(rhr.get("beatsPerMinute")),
                "calculation_method": rhr.get("dailyRestingHeartRateMetadata", {}).get("calculationMethod"),
            }
        )
    return sorted(days, key=lambda d: d["date"])


def get_steps(client: HealthClient, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    start, end = parse_range(start_date, end_date, ROLLUP_MAX_DAYS)
    days = []
    for point in client.daily_rollup("steps", start, end):
        steps = point.get("steps", {}).get("countSum")
        if steps is not None:
            days.append({"date": api_date(point["civilStartTime"]["date"]), "steps": int(steps)})
    return sorted(days, key=lambda d: d["date"])


def get_active_zone_minutes(client: HealthClient, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    start, end = parse_range(start_date, end_date, ROLLUP_MAX_DAYS)
    days = []
    for point in client.daily_rollup("active-zone-minutes", start, end):
        azm = point.get("activeZoneMinutes")
        if azm is None:
            continue
        zones = {zone: int(azm.get(field) or 0) for zone, field in (("fat_burn", "sumInFatBurnHeartZone"), ("cardio", "sumInCardioHeartZone"), ("peak", "sumInPeakHeartZone"))}
        days.append({"date": api_date(point["civilStartTime"]["date"]), "active_zone_minutes": sum(zones.values()), **zones})
    return sorted(days, key=lambda d: d["date"])


def get_vo2max(client: HealthClient, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    start, end = parse_range(start_date, end_date)
    days = []
    for point in client.list_points("daily-vo2-max", daily_filter("daily_vo2_max", start, end)):
        vo2 = point["dailyVo2Max"]
        days.append(
            {
                "date": api_date(vo2["date"]),
                "vo2_max": vo2.get("vo2Max"),
                "estimated": bool(vo2.get("estimated")),
                "cardio_fitness_level": vo2.get("cardioFitnessLevel"),
            }
        )
    return sorted(days, key=lambda d: d["date"])


def minutes_after_noon(timestamp: str) -> int:
    local = dt.datetime.fromisoformat(timestamp)
    return (local.hour * 60 + local.minute - 720) % 1440


def get_coachable_health_age(
    client: HealthClient,
    end_date: str,
    chronological_age: Any,
    sex: Optional[str] = None,
    weight_kg: Any = None,
    window_days: Any = 90,
) -> Dict[str, Any]:
    end = parse_date(end_date, "end_date")
    if not isinstance(window_days, int) or not 1 <= window_days <= ROLLUP_MAX_DAYS:
        raise ValueError(f"window_days must be a whole number from 1 to {ROLLUP_MAX_DAYS}")
    if not isinstance(chronological_age, (int, float)) or not 18 <= chronological_age <= 120:
        raise ValueError("chronological_age must be a number of years from 18 to 120")
    if weight_kg is not None and (not isinstance(weight_kg, (int, float)) or weight_kg <= 0):
        raise ValueError("weight_kg must be a positive number")
    parse_sex(sex)
    first, last = (end - dt.timedelta(days=window_days - 1)).isoformat(), end.isoformat()

    rhr = [d["resting_heart_rate_bpm"] for d in get_resting_heart_rate(client, first, last) if d["resting_heart_rate_bpm"] is not None]
    steps = {d["date"]: d["steps"] for d in get_steps(client, first, last) if d["steps"] > 0}
    azm = {d["date"]: d["active_zone_minutes"] for d in get_active_zone_minutes(client, first, last)}
    worn_days = set(steps) | set(azm)
    vo2 = [d["vo2_max"] for d in get_vo2max(client, first, last) if d["vo2_max"] is not None]
    asleep: Dict[str, int] = {}
    main_sessions: Dict[str, Dict[str, Any]] = {}
    for session in get_sleep(client, first, last):
        if session["nap"] or session["minutes_asleep"] is None:
            continue
        asleep[session["date"]] = asleep.get(session["date"], 0) + session["minutes_asleep"]
        if session["minutes_asleep"] > main_sessions.get(session["date"], {}).get("minutes_asleep", -1):
            main_sessions[session["date"]] = session
    midpoints = [minutes_after_noon(s["midpoint"]) for s in main_sessions.values()]

    coverage = {
        "resting_heart_rate": len(rhr),
        "steps": len(steps),
        "active_zone_minutes": len(worn_days),
        "sleep": len(asleep),
        "sleep_midpoint": len(midpoints),
        "vo2_max": len(vo2),
    }
    missing = [name for name in ("resting_heart_rate", "steps", "active_zone_minutes", "sleep") if coverage[name] == 0]
    if missing:
        raise ValueError(f"no {', '.join(missing)} data between {first} and {last}")

    inputs = ChaInputs(
        chronological_age=float(chronological_age),
        resting_heart_rate_bpm=statistics.mean(rhr),
        steps_per_day=statistics.mean(steps.values()),
        sleep_hours_per_night=statistics.mean(asleep.values()) / 60,
        active_zone_minutes_per_week=7 * sum(azm.get(day, 0) for day in worn_days) / len(worn_days),
        vo2_max=statistics.mean(vo2) if vo2 else None,
        sex=sex,
        weight_kg=None if weight_kg is None else float(weight_kg),
        sleep_midpoint_sd_minutes=statistics.stdev(midpoints) if len(midpoints) >= 2 else None,
    )
    result = coachable_health_age(inputs)
    return {
        "window": {"start_date": first, "end_date": last, "days": window_days},
        "coverage_days": coverage,
        "inputs": {k: round(v, 1) if isinstance(v, float) else v for k, v in vars(inputs).items() if k != "chronological_age"},
        "chronological_age": result["chronological_age"],
        "health_age": round(result["health_age"], 1),
        "delta_years": round(result["delta_years"], 2),
        "floored_at_18": result["floored"],
        "domain_years": {name: round(d["years"], 2) for name, d in result["domains"].items()},
        "omitted_domains": result["omitted_domains"],
    }


def get_daily_summary(client: HealthClient, date: str) -> Dict[str, Any]:
    day = parse_date(date, "date").isoformat()
    hrv = get_hrv(client, day, day)
    rhr = get_resting_heart_rate(client, day, day)
    return {
        "date": day,
        "sleep": get_sleep(client, day, day),
        "hrv": hrv[0] if hrv else None,
        "resting_heart_rate": rhr[0] if rhr else None,
    }


RANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "First Australia/Melbourne calendar date, YYYY-MM-DD."},
        "end_date": {"type": "string", "description": "Last Australia/Melbourne calendar date, YYYY-MM-DD, inclusive."},
    },
    "required": ["start_date", "end_date"],
}
ROLLUP_RANGE_SCHEMA = {**RANGE_SCHEMA, "description": f"At most {ROLLUP_MAX_DAYS} days."}

TOOLS = [
    {
        "name": "get_sleep",
        "description": "Sleep sessions that ended on each Australia/Melbourne date in the range: start, end, midpoint, main sleep or nap, minutes asleep and awake, and minutes per stage.",
        "inputSchema": RANGE_SCHEMA,
    },
    {
        "name": "get_hrv",
        "description": "Daily heart rate variability for each date in the range: average and deep-sleep RMSSD in milliseconds, non-REM heart rate and entropy, measured during the night that ended on that date.",
        "inputSchema": RANGE_SCHEMA,
    },
    {
        "name": "get_resting_heart_rate",
        "description": "Daily resting heart rate in beats per minute for each date in the range.",
        "inputSchema": RANGE_SCHEMA,
    },
    {
        "name": "get_steps",
        "description": "Daily step totals for each date in the range (at most 90 days). Days with no data are left out.",
        "inputSchema": ROLLUP_RANGE_SCHEMA,
    },
    {
        "name": "get_active_zone_minutes",
        "description": "Daily Active Zone Minutes for each date in the range (at most 90 days): the total and the minutes earned in the fat burn, cardio and peak zones. Days with no data are left out.",
        "inputSchema": ROLLUP_RANGE_SCHEMA,
    },
    {
        "name": "get_vo2max",
        "description": "Daily VO2 max (cardio fitness score) in mL/kg/min for each date in the range, whether Google marked it estimated, and the cardio fitness level.",
        "inputSchema": RANGE_SCHEMA,
    },
    {
        "name": "get_coachable_health_age",
        "description": (
            "Coachable Health Age (Lee et al. 2026, arXiv 2611.08568): window means of resting heart rate, steps per day, sleep hours per night, "
            "weekly Active Zone Minutes, VO2 max and sleep midpoint spread, turned into a Health Age and the years each domain adds or removes. "
            "Negative years make Health Age younger. Days with no data are skipped and counted in coverage_days. VO2 max counts only when sex and weight_kg are given."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "end_date": {"type": "string", "description": "Last Australia/Melbourne calendar date of the window, YYYY-MM-DD, inclusive."},
                "chronological_age": {"type": "number", "description": "Age in years, 18 to 120."},
                "sex": {"type": "string", "enum": ["female", "male"], "description": "Biological sex, for expected VO2 max."},
                "weight_kg": {"type": "number", "description": "Body weight in kilograms, for expected VO2 max."},
                "window_days": {"type": "integer", "minimum": 1, "maximum": ROLLUP_MAX_DAYS, "default": 90, "description": "Window length in days ending on end_date."},
            },
            "required": ["end_date", "chronological_age"],
        },
    },
    {
        "name": "get_daily_summary",
        "description": "Sleep, heart rate variability and resting heart rate for one Australia/Melbourne date, for a training-readiness check.",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "Australia/Melbourne calendar date, YYYY-MM-DD."}},
            "required": ["date"],
        },
    },
]


def handle_tool_call(client: HealthClient, name: str, args: Dict[str, Any]) -> Any:
    if name == "get_sleep":
        return get_sleep(client, args.get("start_date"), args.get("end_date"))
    if name == "get_hrv":
        return get_hrv(client, args.get("start_date"), args.get("end_date"))
    if name == "get_resting_heart_rate":
        return get_resting_heart_rate(client, args.get("start_date"), args.get("end_date"))
    if name == "get_steps":
        return get_steps(client, args.get("start_date"), args.get("end_date"))
    if name == "get_active_zone_minutes":
        return get_active_zone_minutes(client, args.get("start_date"), args.get("end_date"))
    if name == "get_vo2max":
        return get_vo2max(client, args.get("start_date"), args.get("end_date"))
    if name == "get_coachable_health_age":
        return get_coachable_health_age(
            client, args.get("end_date"), args.get("chronological_age"), args.get("sex"), args.get("weight_kg"), args.get("window_days", 90)
        )
    if name == "get_daily_summary":
        return get_daily_summary(client, args.get("date"))
    raise ValueError(f"Unknown tool: {name}")


def handle_request(req: Dict[str, Any], get_client: Callable[[], HealthClient]) -> Optional[Dict[str, Any]]:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    if req_id is None:
        return None
    if method == "initialize":
        result: Any = {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "coach-google-health", "version": "1.1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    elif method == "tools/call":
        try:
            out = handle_tool_call(get_client(), params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "isError": False}
        except Exception as e:
            result = {"content": [{"type": "text", "text": f"Error: {type(e).__name__}: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def main() -> None:
    client: Optional[HealthClient] = None

    def get_client() -> HealthClient:
        nonlocal client
        if client is None:
            raw = os.environ.get("COACH_HEALTH_CREDENTIALS")
            if not raw:
                raise GoogleHealthError("COACH_HEALTH_CREDENTIALS is not set")
            try:
                creds = json.loads(raw)
            except ValueError:
                raise GoogleHealthError("COACH_HEALTH_CREDENTIALS is not valid JSON") from None
            client = HealthClient(creds)
        return client

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        resp = handle_request(req, get_client)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
