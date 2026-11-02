from __future__ import annotations

from tests.conftest import make_packet, ro_rows, seed, source_op, upsert_op


def _thread(tid, account, external_id, **fields):
    row = {"id": tid, "channel": "email", "account": account, "external_id": external_id}
    row.update(fields)
    return upsert_op("upsert_thread", row, "src-note-anon")


def _link(lid, action_id, thread_id, role="awaiting_reply", since="2026-10-01T00:00:00Z", **fields):
    row = {"id": lid, "action_id": action_id, "thread_id": thread_id, "role": role, "since": since}
    row.update(fields)
    return upsert_op("upsert_thread_link", row, "src-note-anon")


def _action(aid="act-thread"):
    return upsert_op(
        "upsert_action",
        {
            "id": aid,
            "title": "Wait on Dana",
            "status": "waiting",
            "status_basis": "observed",
            "project_id": "general",
            "waiting_on_person_id": "per-dana-okafor",
            "last_verified_at": "2026-10-01T00:00:00Z",
        },
        "src-note-anon",
    )


def test_reply_on_the_linked_thread_marks_replied(instance, apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            _action(),
            _thread("thr-a", "work-mail", "thread-a", last_inbound_at="2026-10-02T00:00:00Z"),
            _link("lnk-a", "act-thread", "thr-a"),
        )
    )
    state = ro_rows(
        instance,
        "SELECT reply_state FROM thread_link_state WHERE link_id = 'lnk-a'",
    )
    assert state == [("replied",)]


def test_message_on_another_thread_does_not_mark_replied(instance, apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            _action(),
            _thread("thr-a", "work-mail", "thread-a"),
            _thread("thr-b", "work-mail", "thread-b", last_inbound_at="2026-10-02T00:00:00Z"),
            _link("lnk-a", "act-thread", "thr-a"),
        )
    )
    state = ro_rows(
        instance,
        "SELECT reply_state FROM thread_link_state WHERE link_id = 'lnk-a'",
    )
    assert state == [("waiting",)]


def test_no_inbound_after_since_is_still_waiting(instance, apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            _action(),
            _thread("thr-a", "work-mail", "thread-a", last_inbound_at="2026-09-01T00:00:00Z"),
            _link("lnk-a", "act-thread", "thr-a", since="2026-10-01T00:00:00Z"),
        )
    )
    assert ro_rows(instance, "SELECT reply_state FROM thread_link_state WHERE link_id = 'lnk-a'") == [
        ("waiting",)
    ]


def test_owes_reply_is_replied_after_the_principal_answers(instance, apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            _action(),
            _thread(
                "thr-a",
                "work-mail",
                "thread-a",
                last_inbound_at="2026-10-01T00:00:00Z",
                last_outbound_at="2026-10-02T00:00:00Z",
                outbound_seen=True,
            ),
            _link("lnk-a", "act-thread", "thr-a", role="owes_reply", since="2026-10-01T00:00:00Z"),
        )
    )
    assert ro_rows(instance, "SELECT reply_state FROM thread_link_state WHERE link_id = 'lnk-a'") == [
        ("replied",)
    ]


def test_owes_reply_is_unknown_when_outbound_is_not_visible(instance, apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            _action(),
            _thread("thr-a", "work-mail", "thread-a", outbound_seen=False),
            _link("lnk-a", "act-thread", "thr-a", role="owes_reply"),
        )
    )
    assert ro_rows(instance, "SELECT reply_state FROM thread_link_state WHERE link_id = 'lnk-a'") == [
        ("unknown",)
    ]


def test_new_message_after_seen_through_and_cleared_by_seen_through(instance, apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            _action(),
            _thread("thr-a", "work-mail", "thread-a", last_inbound_at="2026-10-03T00:00:00Z"),
            _link("lnk-a", "act-thread", "thr-a", seen_through="2026-10-02T00:00:00Z"),
        )
    )
    assert ro_rows(instance, "SELECT new_message FROM thread_link_state WHERE link_id = 'lnk-a'") == [
        (1,)
    ]
    apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_thread_link",
                {"id": "lnk-a", "seen_through": "2026-10-03T00:00:00Z"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    assert ro_rows(instance, "SELECT new_message FROM thread_link_state WHERE link_id = 'lnk-a'") == [
        (0,)
    ]


def test_closed_link_is_not_reported(instance, apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            _action(),
            _thread("thr-a", "work-mail", "thread-a", last_inbound_at="2026-10-03T00:00:00Z"),
            _link("lnk-a", "act-thread", "thr-a", status="closed"),
        )
    )
    assert ro_rows(instance, "SELECT * FROM thread_link_state WHERE link_id = 'lnk-a'") == []


def test_thread_identity_is_unique_per_channel_account_and_external_id(apply) -> None:
    seed(apply)
    first = apply(
        make_packet(
            "tester",
            _thread("thr-a", "work-mail", "thread-a"),
        )
    )
    second = apply(
        make_packet(
            "tester",
            _thread("thr-b", "work-mail", "thread-a"),
        )
    )
    assert first.receipt["status"] == "applied"
    assert second.receipt["status"] == "rejected"


def test_slack_external_id_must_be_channel_and_ts(apply) -> None:
    seed(apply)
    bad = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_thread",
                {
                    "id": "thr-slack-bad",
                    "channel": "slack",
                    "account": "team-slack",
                    "external_id": "not-a-slack-id",
                },
                "src-note-anon",
            ),
        )
    )
    good = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_thread",
                {
                    "id": "thr-slack-good",
                    "channel": "slack",
                    "account": "team-slack",
                    "external_id": "C0ABC1234:1791400000.000100",
                },
                "src-note-anon",
            ),
        )
    )
    assert bad.receipt["status"] == "rejected"
    assert good.receipt["status"] == "applied"


def test_link_needs_exactly_one_subject(apply) -> None:
    seed(apply)
    apply(make_packet("tester", _action(), _thread("thr-a", "work-mail", "thread-a")))
    both = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_thread_link",
                {
                    "id": "lnk-both",
                    "action_id": "act-thread",
                    "checklist_item_id": "chk-x",
                    "thread_id": "thr-a",
                    "role": "event",
                    "since": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    none = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_thread_link",
                {
                    "id": "lnk-none",
                    "thread_id": "thr-a",
                    "role": "event",
                    "since": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    assert both.receipt["status"] == "rejected"
    assert none.receipt["status"] == "rejected"


def test_checklist_item_can_link_to_a_thread(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_checklist_item",
                {
                    "id": "chk-thread",
                    "project_id": "general",
                    "title": "Checklist",
                    "state": "waiting",
                    "state_basis": "observed",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
            _thread("thr-c", "work-mail", "thread-c"),
            upsert_op(
                "upsert_thread_link",
                {
                    "id": "lnk-c",
                    "checklist_item_id": "chk-thread",
                    "thread_id": "thr-c",
                    "role": "event",
                    "since": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    assert outcome.receipt["status"] == "applied"


def test_reply_signal_needs_a_linked_thread(apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            _action(),
            _thread("thr-a", "work-mail", "thread-a"),
            _thread("thr-b", "work-mail", "thread-b"),
            _link("lnk-a", "act-thread", "thr-a"),
        )
    )
    no_thread = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_signal",
                {
                    "id": "sig-none",
                    "kind": "reply",
                    "subject_table": "actions",
                    "subject_id": "act-thread",
                    "basis": "observed",
                },
                "src-note-anon",
            ),
        )
    )
    unlinked = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_signal",
                {
                    "id": "sig-unlinked",
                    "kind": "reply",
                    "subject_table": "actions",
                    "subject_id": "act-thread",
                    "thread_id": "thr-b",
                    "basis": "observed",
                },
                "src-note-anon",
            ),
        )
    )
    linked = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_signal",
                {
                    "id": "sig-linked",
                    "kind": "reply",
                    "subject_table": "actions",
                    "subject_id": "act-thread",
                    "thread_id": "thr-a",
                    "basis": "observed",
                },
                "src-note-anon",
            ),
        )
    )
    assert no_thread.receipt["status"] == "rejected"
    assert unlinked.receipt["status"] == "rejected"
    assert linked.receipt["status"] == "applied"
