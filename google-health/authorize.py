#!/usr/bin/env python3
"""
One-time Google sign-in for the Coach Google Health MCP server (server.py).

  python3 authorize.py ~/.config/coach-health/client_secret.json | pbcopy

Reads the Desktop app OAuth client JSON downloaded from Google Cloud, opens the
browser on Google's consent screen (loopback redirect on 127.0.0.1 with a random
port, PKCE, offline access) and prints the COACH_HEALTH_CREDENTIALS JSON to
stdout. Prompts and errors go to stderr; stdout carries only the credentials.

The app is unverified, so Google first shows "Google hasn't verified this app":
click Advanced, then continue. Run this again only if the refresh token is
revoked or the app goes back to Testing (7-day refresh tokens).

Python 3.9+ standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Callable, Dict, Optional, Tuple

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

Transport = Callable[[str, Dict[str, str]], Tuple[int, Dict[str, Any]]]


def post_form(url: str, fields: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}


def load_client(path: str) -> Dict[str, str]:
    with open(path) as f:
        data = json.load(f)
    client = data.get("installed")
    if not client:
        raise SystemExit("Expected a Desktop app OAuth client JSON (top-level key 'installed').")
    return {"client_id": client["client_id"], "client_secret": client["client_secret"]}


def pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def wait_for_code(server: http.server.HTTPServer, state: str) -> str:
    result: Dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if params.get("state") != state:
                result["error"] = "state mismatch"
            elif "error" in params:
                result["error"] = params["error"]
            elif "code" in params:
                result["code"] = params["code"]
            else:
                result["error"] = "no code in the redirect"
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab." if ok else b"Sign-in failed. Check the terminal.")

        def log_message(self, *args: Any) -> None:
            pass

    server.RequestHandlerClass = Handler
    while not result:
        server.handle_request()
    if "error" in result:
        raise SystemExit(f"Authorisation failed: {result['error']}")
    return result["code"]


def authorize(
    client: Dict[str, str],
    open_browser: Callable[[str], Any] = webbrowser.open,
    transport: Transport = post_form,
) -> Dict[str, str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        url = auth_url(client["client_id"], redirect_uri, challenge, state)
        print(f"Opening Google sign-in. If no browser opens, visit:\n{url}\n", file=sys.stderr)
        open_browser(url)
        code = wait_for_code(server, state)
    finally:
        server.server_close()
    status, token = transport(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        },
    )
    if status != 200 or not token.get("refresh_token"):
        raise SystemExit(f"Token exchange failed (HTTP {status}): {token.get('error', 'no refresh_token returned')}")
    missing = set(SCOPES) - set(token.get("scope", "").split())
    if missing:
        raise SystemExit(f"Consent did not grant {', '.join(sorted(missing))}. Run again and tick every box.")
    return {"client_id": client["client_id"], "client_secret": client["client_secret"], "refresh_token": token["refresh_token"]}


def main(argv: Optional[list] = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: authorize.py <path to Desktop OAuth client JSON>")
    print(json.dumps(authorize(load_client(args[0]))))


if __name__ == "__main__":
    main()
