"""Connection diagnostics: `python -m macd_desk.diagnose`.

Answers the question a bare HTTP status cannot — whether a failure is the
credentials, this machine's IP, the redirect URI, or Upstox being unreachable.
Prints nothing secret: the key is masked and the secret is only ever reported
as present or absent.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Optional

from .broker.upstox import USER_AGENT, UpstoxClient, UpstoxError
from .config import Settings, load_settings

PUBLIC_IP_URL = "https://api.ipify.org?format=json"
OK, BAD, INFO = "  ok  ", " fail ", " ..   "


def line(mark: str, label: str, detail: str = "") -> None:
    print(f"[{mark}] {label}" + (f"\n         {detail}" if detail else ""))


def public_ip(timeout: float = 5.0) -> Optional[str]:
    """Best effort — Upstox apps can restrict access to a fixed IP."""
    try:
        request = urllib.request.Request(PUBLIC_IP_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode()).get("ip")
    except Exception:
        return None


def reachable(url: str, timeout: float = 8.0):
    """Does the host answer us at all, and with what?"""
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": USER_AGENT,
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, ""
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")[:200]
    except urllib.error.URLError as error:
        return None, str(error.reason)


def run(settings: Optional[Settings] = None) -> int:
    settings = settings or load_settings()
    upstox = settings.upstox
    problems = 0

    print("Upstox connection check\n" + "-" * 60)

    # 1. credentials
    if upstox.configured:
        public = upstox.public()
        line(OK, f"Credentials loaded (key {public['apiKey']}, secret set)")
    else:
        problems += 1
        line(BAD, "Credentials incomplete",
             "missing " + ", ".join(upstox.missing) + " — see .env.example")

    line(INFO, f"Redirect URI  {upstox.redirect_uri or '(unset)'}",
         "This must match the Upstox app registration exactly, host and port included.")

    # 2. this machine, as Upstox sees it
    ip = public_ip()
    if ip:
        line(OK, f"Public IP     {ip}",
             "If the Upstox app has a static IP restriction, this address must be on it.")
    else:
        line(INFO, "Public IP     could not be determined")

    # 3. can we reach the API at all
    status, detail = reachable(f"{upstox.api_base}/v2/login/authorization/dialog")
    if status is None:
        problems += 1
        line(BAD, f"Reaching {upstox.api_base} failed", detail)
    elif status == 403:
        problems += 1
        line(BAD, f"{upstox.api_base} answered HTTP 403",
             "The edge refused us before the API saw the request — usually a static IP "
             "restriction on the app, or an outbound proxy. " + (detail or "(no body)"))
    else:
        line(OK, f"{upstox.api_base} reachable (HTTP {status} on the login dialog)")

    # 4. the cached token, if there is one
    client = UpstoxClient(upstox)
    token = client.tokens.load()
    if token is None:
        line(INFO, "No cached token", "Connect from /broker to create one.")
    elif not token.is_valid():
        line(INFO, "Cached token has expired",
             f"expired {token.expires_at:%d %b %H:%M} — tokens die at 03:30 IST daily")
    else:
        try:
            profile = client.profile()
            line(OK, f"Token works — {profile.get('user_name', 'account')} "
                     f"({profile.get('user_id', '')})")
        except UpstoxError as error:
            problems += 1
            line(BAD, "Cached token rejected", str(error))

    print("-" * 60)
    if problems:
        print(f"{problems} problem(s) above. Fix those, then retry Connect from /broker.")
    else:
        print("No problems found. If the login still fails, the authorization code is "
              "likely being reused — each one works once, so start a fresh Connect.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(run())
