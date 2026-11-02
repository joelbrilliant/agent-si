"""Gmail: mail matching live_query becomes capture items, and the threads they touch go to
the gmail metadata collector named by threads. Read-only scope: gmail.readonly.

The cursor is the mailbox historyId. Each run lists the ids that match the query (cheap), and gets in full only
those history reports as added since the cursor. A 404 from history means the id is too old: every listed id is
got again, and the capture plan skips what it already holds.
"""
from __future__ import annotations

from datetime import datetime, timezone

from brain.collectors.base import local_date
from brain.collectors.privacy import scrub
from brain_fetch import google, mailtext, net
from brain_fetch.common import Pulled, capture_doc, line

API = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_QUERY = ("{in:inbox in:sent} -category:promotions -category:social -category:updates -category:forums "
                 "newer_than:14d")
MAX_MESSAGES = 2000
MAX_HISTORY_PAGES = 20


def _utc(millis) -> str:
    try:
        stamp = datetime.fromtimestamp(int(millis) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        stamp = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _listed(token: str, query: str) -> tuple[list[tuple[str, str]], bool]:
    found, page, capped = [], None, False
    while True:
        params = {"q": query, "maxResults": 500, **({"pageToken": page} if page else {})}
        _, payload = net.request("GET", f"{API}/messages", token=token, params=params)
        for item in net.field(payload, "messages", default=[]) or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                found.append((item["id"], str(item.get("threadId") or item["id"])))
        page = net.field(payload, "nextPageToken")
        if len(found) >= MAX_MESSAGES:
            return found[:MAX_MESSAGES], True
        if not isinstance(page, str) or not page:
            return found, capped


def _added(token: str, since: str) -> set[str] | None:
    """Message ids added since the cursor, or None when the cursor is too old (404) or the history is too long."""
    added, page = set(), None
    for _ in range(MAX_HISTORY_PAGES):
        params = {"startHistoryId": since, "historyTypes": "messageAdded", "maxResults": 500,
                  **({"pageToken": page} if page else {})}
        status, payload = net.request("GET", f"{API}/history", token=token, params=params)
        if status == 404:
            return None
        if status == 410:
            raise net.NetError("Gmail answered 410 to a history read", "network", status)
        for entry in net.field(payload, "history", default=[]) or []:
            for added_item in net.field(entry, "messagesAdded", default=[]) or []:
                message_id = net.field(added_item, "message", "id")
                if isinstance(message_id, str):
                    added.add(message_id)
        page = net.field(payload, "nextPageToken")
        if not isinstance(page, str) or not page:
            return added
    return None


def _get(token: str, path: str) -> dict | None:
    status, payload = net.request("GET", f"{API}/{path}", token=token)
    if status in (404, 410):
        return None  # deleted between the list and the read
    return payload if isinstance(payload, dict) else None


def _headers(message: dict) -> dict:
    found = {}
    for item in net.field(message, "payload", "headers", default=[]) or []:
        if isinstance(item, dict):
            found.setdefault(str(item.get("name", "")).lower(), str(item.get("value", "")))
    return found


def _item(message: dict, origin: str, zone: str) -> dict:
    head = _headers(message)
    text, attachments = mailtext.body(message.get("payload") or {})
    subject = line(scrub(line(head.get("subject", ""), 400)), 200) or "(no subject)"
    sender = mailtext.names(head.get("from", ""), 1)
    item = {
        "ref": f"email:{origin}/{message['id']}",
        "kind": "email",
        "title": subject,
        "date": local_date(_utc(message.get("internalDate")), zone),
        "from": line(scrub(sender[0]), 120) if sender else "(no name)",
        "from_principal": "SENT" in (message.get("labelIds") or []),
        "text": scrub(text),
    }
    to = mailtext.names(", ".join(filter(None, [head.get("to", ""), head.get("cc", "")])))
    if to:
        item["to"] = [line(scrub(name), 120) or "(no name)" for name in to]
    if attachments:
        item["attachments"] = [line(scrub(name), 200) or "(no name)" for name in attachments]
    return item


def _thread(token: str, thread_id: str, fetched: str) -> dict | None:
    thread = _get(token, f"threads/{thread_id}?format=minimal")
    if thread is None:
        return None
    messages = []
    for message in thread.get("messages") or []:
        if not isinstance(message, dict) or "DRAFT" in (message.get("labelIds") or []):
            continue
        labels = message.get("labelIds") or []
        messages.append({"at": min(_utc(message.get("internalDate")), fetched),
                         "direction": "outbound" if "SENT" in labels else "inbound"})
    return {"id": thread_id, "subject": "", "messages": messages[-1000:]} if messages else None


def pull(ctx) -> Pulled:
    token = google.access_token(ctx.secret)
    _, profile = net.request("GET", f"{API}/profile", token=token)
    history_id = str(net.field(profile, "historyId", default="") or "")
    if not history_id.isdigit():
        raise net.NetError("Gmail's profile answer had no historyId", "network")
    listed, capped = _listed(token, ctx.options.get("live_query") or DEFAULT_QUERY)
    since = str(ctx.cursor.get("history_id") or "")
    added = _added(token, since) if since.isdigit() else None
    wanted = [(mid, tid) for mid, tid in listed if added is None or mid in added]
    items, threads = [], {}
    for message_id, thread_id in wanted:
        message = _get(token, f"messages/{message_id}?format=full")
        if message is None or "DRAFT" in (message.get("labelIds") or []) or message.get("id") != message_id:
            continue
        items.append(_item(message, ctx.options["origin"], ctx.cfg.timezone))
        threads[str(message.get("threadId") or thread_id)] = True
    pulled = Pulled(cursor={"history_id": history_id}, items=len(items))
    pulled.docs.append((ctx.collector.name, capture_doc(ctx, items)))
    meta = ctx.threads()
    if meta is not None:
        rows = [row for row in (_thread(token, tid, ctx.fetched) for tid in sorted(threads)) if row]
        pulled.docs.append((meta.name, {"format": "agent-brain.gmail.v1", "fetched_at": ctx.fetched, "complete": True,
                                        "account": meta.options["account"], "outbound_visible": True,
                                        "threads": rows}))
    notes = [f"the query matched more than {MAX_MESSAGES} messages; the oldest were left out" if capped else "",
             "the history cursor was too old, so every match was read again" if since and added is None else ""]
    pulled.note = "; ".join(filter(None, notes))
    return pulled


def probe(ctx) -> str:
    token = google.access_token(ctx.secret)
    net.request("GET", f"{API}/profile", token=token)
    return "ok"
