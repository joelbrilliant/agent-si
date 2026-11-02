"""The OAuth refresh exchange Gmail and Calendar share.

The stored credential is one line of JSON with client_id, client_secret and refresh_token. It's exchanged for a
short-lived access token at the start of every run and never written anywhere.
"""
from __future__ import annotations

from brain import ingress
from brain_fetch import net

TOKEN_URL = "https://oauth2.googleapis.com/token"
_KEYS = ("client_id", "client_secret", "refresh_token")
TESTING_NOTE = ("Google refused the refresh token (invalid_grant). An OAuth app left in Testing status gets refresh "
                "tokens that expire after 7 days: make it a Workspace-internal app, or mint a new token; "
                "brain-fetch setup gmail shows how")


def parse(secret: str) -> dict:
    try:
        data = ingress.loads(secret, what="credential")
    except ingress.IngressError:
        data = None
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key] for key in _KEYS):
        raise net.NetError("the stored credential isn't the JSON brain-fetch setup describes "
                           "(client_id, client_secret and refresh_token)", "config")
    return {key: data[key] for key in _KEYS}


def access_token(secret: str) -> str:
    form = {"grant_type": "refresh_token", **parse(secret)}
    status, payload = net.request("POST", TOKEN_URL, form=form, expect=(400,))
    error = net.field(payload, "error", default="")
    if status == 400 and error == "invalid_grant":
        raise net.NetError(TESTING_NOTE, "auth", status)
    if status == 400:
        reason = error if isinstance(error, str) and error.isidentifier() else "unknown"
        raise net.NetError(f"Google refused the token exchange ({reason})", "auth", status)
    token = net.field(payload, "access_token")
    if not isinstance(token, str) or not token:
        raise net.NetError("Google's token answer had no access token", "auth", status)
    return token
