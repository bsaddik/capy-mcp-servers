#!/usr/bin/env python3
"""
Read-only Google Health API MCP server for Coach, Benji's private health Captain.

Reads sleep, daily heart rate variability and daily resting heart rate. Dates in
and out are Australia/Melbourne calendar dates (YYYY-MM-DD, end date inclusive).

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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4/users/me/dataTypes"
MELBOURNE = ZoneInfo("Australia/Melbourne")
MAX_RANGE_DAYS = 92

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

    def _get(self, url: str) -> Any:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token(force=attempt > 0)}", "Accept": "application/json"}
            status, payload = self._transport("GET", url, headers, None)
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
            payload = self._get(f"{API_BASE}/{data_type}/dataPoints?{urllib.parse.urlencode(query)}")
            points.extend(payload.get("dataPoints", []))
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                return points


def parse_date(value: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a YYYY-MM-DD date, got {value!r}") from None


def parse_range(start_date: str, end_date: str) -> Tuple[dt.date, dt.date]:
    start, end = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if end < start:
        raise ValueError("end_date is before start_date")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise ValueError(f"range is longer than {MAX_RANGE_DAYS} days")
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


def as_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def daily_filter(field: str, start: dt.date, end: dt.date) -> str:
    after_end = end + dt.timedelta(days=1)
    return f'{field}.date >= "{start.isoformat()}" AND {field}.date < "{after_end.isoformat()}"'


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
    points = client.list_points("daily-heart-rate-variability", daily_filter("dailyHeartRateVariability", start, end))
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
    points = client.list_points("daily-resting-heart-rate", daily_filter("dailyRestingHeartRate", start, end))
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

TOOLS = [
    {
        "name": "get_sleep",
        "description": "Sleep sessions that ended on each Australia/Melbourne date in the range: start, end, main sleep or nap, minutes asleep and awake, and minutes per stage.",
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
            "serverInfo": {"name": "coach-google-health", "version": "1.0.0"},
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
