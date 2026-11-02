"""Slack: messages in the listed channels, and the principal's DMs when include_dms is
true, become capture items. Scopes: channels:history, groups:history and users:read; im:history and im:read
for DMs; channels:read and groups:read only to show channel names.

A message with no replies is one item. A thread becomes one item with its replies once its latest reply is 24
hours old, and waits in the cursor until then. Each run reads from the cursor less 24 hours, so an edit made
within a day gives a new version. Nothing outside the listed channels is ever requested.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from brain.collectors.base import local_date
from brain.collectors.privacy import scrub
from brain_fetch import net, slacktext
from brain_fetch.common import Pulled, capture_doc, line

API = "https://slack.com/api/"
LOOKBACK = 14 * 86400
OVERLAP = 86400
QUIET = 86400
MAX_PAGES = 50
_TS = re.compile(r"[0-9]{10}\.[0-9]{6}")
_AUTH = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired", "missing_scope",
         "not_allowed_token_type", "no_permission", "ekm_access_denied"}
_CONFIG = {"channel_not_found", "not_in_channel", "is_archived"}


def caller(token: str):
    def call(method: str, soft: tuple = (), **params):
        status, payload = net.request("GET", API + method, token=token, params=params)
        if status in (404, 410):
            raise net.NetError(f"Slack answered {status} to {method}", "network", status)
        if net.field(payload, "ok") is True:
            return payload
        error = net.field(payload, "error", default="")
        error = error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,60}", error) else "unknown"
        if error in soft:
            return payload
        kind = "auth" if error in _AUTH else "network" if error == "ratelimited" else "config" if error in _CONFIG else "other"
        raise net.NetError(f"Slack refused {method} ({error})", kind)
    return call


def _seconds(ts: str) -> float:
    return float(ts) if _TS.fullmatch(ts or "") else 0.0


def _utc(ts: str) -> str:
    return datetime.fromtimestamp(int(_seconds(ts)), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pages(call, method: str, key: str, **params):
    """Every page of a cursor-paged list, up to MAX_PAGES. The second value is True when pages were left."""
    found, cursor = [], ""
    for _ in range(MAX_PAGES):
        payload = call(method, limit=200, **params, **({"cursor": cursor} if cursor else {}))
        found.extend(item for item in payload.get(key) or [] if isinstance(item, dict))
        cursor = net.field(payload, "response_metadata", "next_cursor", default="")
        if not isinstance(cursor, str) or not cursor:
            return found, False
    return found, True


def _channels(ctx, call, names) -> dict[str, str]:
    channels = {}
    for channel in ctx.options.get("live_channels") or []:
        info = call("conversations.info", soft=("missing_scope",), channel=channel)
        name = net.field(info, "channel", "name", default="")
        channels[channel] = name if isinstance(name, str) and name else channel
    if ctx.options.get("include_dms"):
        ims, _ = _pages(call, "conversations.list", "channels", types="im")
        for im in ims:
            if isinstance(im.get("id"), str) and isinstance(im.get("user"), str):
                channels[im["id"]] = "dm-" + names(im["user"])
    return channels


def _item(ctx, channel: str, label: str, messages: list, names, me: str) -> dict:
    root = messages[0]
    text = scrub(slacktext.render(root.get("text"), names))
    replies = [
        f"[{_utc(reply['ts'])[11:16]}] {scrub(names(reply['user']))}: {scrub(slacktext.render(reply.get('text'), names))}"
        for reply in messages[1:]
    ]
    if replies:
        text = text + "\n\nReplies:\n" + "\n".join(replies)
    first = text.strip().split("\n", 1)[0].strip() or "(no text)"
    item = {
        "ref": f"slack:{ctx.options['origin']}/{channel}/{root['ts']}",
        "kind": "slack",
        "title": line(scrub(f"#{line(label, 80)}: {line(first, 60)}"), 200),
        "date": local_date(_utc(root["ts"]), ctx.cfg.timezone),
        "from": line(scrub(names(root["user"])), 120) or "(unknown)",
        "from_principal": all(message["user"] == me for message in messages),
        "text": text,
    }
    attachments = [name for message in messages for name in slacktext.files(message)][:20]
    if attachments:
        item["attachments"] = [line(scrub(name), 200) or "(no name)" for name in attachments]
    return item


def _meta(channel: str, messages: list, me: str, fetched: str) -> dict | None:
    if not re.fullmatch(r"[A-Z0-9]{2,20}", channel):
        return None
    return {"id": f"{channel}:{messages[0]['ts']}",
            "messages": [{"at": min(_utc(item["ts"]), fetched), "direction": "outbound" if item["user"] == me else "inbound"}
                         for item in messages[-1000:]]}


def pull(ctx) -> Pulled:
    call = caller(ctx.secret)
    me = net.field(call("auth.test"), "user_id", default="")
    if not isinstance(me, str) or not me:
        raise net.NetError("Slack's auth.test answer had no user_id", "auth")
    names = slacktext.Names(call)
    now = datetime.strptime(ctx.fetched, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    channels = _channels(ctx, call, names)
    seen = ctx.cursor.get("channels") if isinstance(ctx.cursor.get("channels"), dict) else {}
    waiting = ctx.cursor.get("open") if isinstance(ctx.cursor.get("open"), dict) else {}
    waiting = {key: value for key, value in waiting.items() if isinstance(value, str) and _TS.fullmatch(value)}
    groups, latest, capped = [], {}, False
    for channel in sorted(channels):
        since = seen.get(channel) if isinstance(seen.get(channel), str) and _TS.fullmatch(seen[channel]) else ""
        oldest = max(_seconds(since) - OVERLAP, now - LOOKBACK) if since else now - LOOKBACK
        messages, more = _pages(call, "conversations.history", "messages", channel=channel, oldest=f"{oldest:.6f}")
        capped = capped or more
        latest[channel] = max([since] + [m["ts"] for m in messages if _TS.fullmatch(str(m.get("ts")))], key=_seconds)
        for message in messages:
            if not slacktext.kept(message):
                continue
            if message.get("reply_count") and message.get("thread_ts") == message["ts"]:
                reply = str(message.get("latest_reply") or message["ts"])
                waiting[f"{channel}/{message['ts']}"] = reply if _TS.fullmatch(reply) else message["ts"]
            else:
                groups.append((channel, [message]))
    for key in sorted(waiting):
        channel, _, ts = key.partition("/")
        if channel not in channels or not _TS.fullmatch(ts):
            del waiting[key]
            continue
        if now - _seconds(waiting[key]) < QUIET:
            continue
        thread, _ = _pages(call, "conversations.replies", "messages", channel=channel, ts=ts,
                           soft=("thread_not_found",))
        stamps = [m["ts"] for m in thread if _TS.fullmatch(str(m.get("ts")))]
        if not stamps or thread[0].get("ts") != ts or not slacktext.kept(thread[0]):
            del waiting[key]
            continue
        newest = max(stamps, key=_seconds)
        if now - _seconds(newest) < QUIET:
            waiting[key] = newest
            continue
        groups.append((channel, [thread[0]] + [m for m in thread[1:] if slacktext.kept(m)]))
        del waiting[key]
    # One item per ref: a root read again from the overlap and from the cursor keeps its thread version.
    groups = list({(channel, messages[0]["ts"]): (channel, messages) for channel, messages in groups}.values())
    items = [_item(ctx, channel, channels[channel], messages, names, me) for channel, messages in groups]
    pulled = Pulled(cursor={"channels": latest, "open": waiting}, items=len(items))
    pulled.docs.append((ctx.collector.name, capture_doc(ctx, items)))
    meta = ctx.threads()
    if meta is not None:
        rows = [row for row in (_meta(channel, messages, me, ctx.fetched) for channel, messages in groups) if row]
        pulled.docs.append((meta.name, {"format": "agent-brain.slack.v1", "fetched_at": ctx.fetched, "complete": True,
                                        "account": meta.options["account"], "outbound_visible": True,
                                        "threads": rows}))
    pulled.note = f"a channel had more than {MAX_PAGES} pages; the oldest messages were left out" if capped else ""
    return pulled


def probe(ctx) -> str:
    caller(ctx.secret)("auth.test")
    return "ok"
