"""Incremental Imagine-conversation sync (gdownloader.archive_conversations).

By default only conversations that are new, or whose modifyTime changed since they were
last archived cleanly, get their /responses fetched; --deep re-reads them all. State is
per account in conversation_state.json. All HTTP is mocked.

Run: python -m pytest test_incremental_conversations.py
"""

from __future__ import annotations

import argparse
import json
from urllib.parse import parse_qs, urlparse

import pytest

import gdownloader as g

AUTH = g.RequestSpec(method="GET", url="https://grok.com/x", headers={}, cookies={"sso": "s"}, body=None)


class FakeGrok:
    """conversations: list of pages, each a list of (id, modifyTime[, starred])."""

    def __init__(self, pages, fail_ids=(), unreadable=()):
        self.pages = pages
        self.fail_ids = set(fail_ids)        # downloads fail for these
        self.unreadable = set(unreadable)    # /responses errors for these
        self.list_calls = 0
        self.fetched: list[str] = []

    def request(self, _client, spec):
        url = urlparse(spec.url)
        if url.path.endswith("/responses"):
            conv = url.path.split("/")[-2]
            if conv in self.unreadable:
                raise RuntimeError("boom")
            self.fetched.append(conv)
            return {"conv": conv}
        token = parse_qs(url.query).get("pageToken", ["0"])[0]
        i = int(token)
        self.list_calls += 1
        convs = [{"conversationId": c[0], "title": c[0], "modifyTime": c[1],
                  "starred": c[2] if len(c) > 2 else False} for c in self.pages[i]]
        nxt = str(i + 1) if i + 1 < len(self.pages) else None
        return {"conversations": convs, "nextPageToken": nxt, "textSearchMatches": []}


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda _s: None)
    state_path = tmp_path / "conversation_state.json"
    by_id: dict = {}

    def _run(fake, account="default", deep=False, max_pages=None, refresh=False):
        monkeypatch.setattr(g, "ACCOUNT_ID", account)
        monkeypatch.setattr(g, "request_json_with_backoff", fake.request)
        monkeypatch.setattr(g, "extract_conversation_items", lambda data: [{"id": "item-" + data["conv"]}])

        def fake_archive(_client, _spec, items, by, _args, _saved):
            conv = items[0]["id"][5:]
            if conv in fake.fail_ids:
                g.FAILED_COUNT += 1
                return 0
            rec = by.setdefault(items[0]["id"], {"id": items[0]["id"], "accounts": []})
            if account not in rec["accounts"]:
                rec["accounts"].append(account)
            return 1

        monkeypatch.setattr(g, "_archive_conversation_items", fake_archive)
        args = argparse.Namespace(grok_conversations=[], refresh_metadata=refresh, max_pages=max_pages,
                                  deep=deep, conversation_state=state_path, quiet=True)
        fake.fetched.clear()
        fake.list_calls = 0
        return g.archive_conversations(None, AUTH, AUTH, by_id, args)

    _run.state = lambda: json.loads(state_path.read_text())
    _run.by_id = by_id
    return _run


T = ["2026-10-05T23:19:27.258Z", "2026-03-18T10:00:00Z", "2026-03-18T09:00:00Z",
     "2026-02-17T00:00:00Z", "2026-02-16T00:00:00Z", "2026-01-01T00:00:00Z"]


def pages():
    return [[("a", T[0]), ("b", T[1])], [("c", T[2]), ("d", T[3])], [("e", T[4]), ("f", T[5])]]


def test_first_run_reads_everything_then_unchanged_are_skipped(run, capsys):
    fake = FakeGrok(pages())
    run(fake)
    assert fake.fetched == list("abcdef")
    assert run.state()["accounts"]["default"]["conversations"]["a"] == T[0]
    run(fake)
    assert fake.fetched == []
    assert fake.list_calls == 1  # early stop: page 1 was entirely unchanged
    assert "0 changed/new, 2 unchanged skipped" in capsys.readouterr().out


def test_changed_and_new_conversations_are_fetched(run, capsys):
    fake = FakeGrok(pages())
    run(fake)
    # "d" was edited (now most recent) and "z" is brand new.
    fake.pages = [[("z", "2026-10-06T08:00:00Z"), ("d", "2026-10-06T07:00:00Z")],
                  [("a", T[0]), ("b", T[1])], [("c", T[2]), ("e", T[4])], [("f", T[5])]]
    run(fake)
    assert sorted(fake.fetched) == ["d", "z"]
    assert fake.list_calls == 2  # stopped after the first fully-unchanged page
    assert run.state()["accounts"]["default"]["conversations"]["d"] == "2026-10-06T07:00:00Z"
    assert "2 changed/new, 2 unchanged skipped" in capsys.readouterr().out


def test_deep_refetches_everything_and_refreshes_state(run):
    fake = FakeGrok(pages())
    run(fake)
    run(fake, deep=True)
    assert fake.fetched == list("abcdef") and fake.list_calls == 3
    run(fake)
    assert fake.fetched == []


def test_failed_conversation_is_not_recorded_and_is_retried_even_deep_in_the_list(run):
    fake = FakeGrok(pages(), fail_ids={"f"}, unreadable={"e"})
    run(fake)
    st = run.state()["accounts"]["default"]
    assert "f" not in st["conversations"] and "e" not in st["conversations"]
    assert st["pending"] == ["e", "f"]
    fake.fail_ids.clear()
    fake.unreadable.clear()
    run(fake)
    # Page 1 is unchanged, but pending e/f sit on page 3, so listing must reach them.
    assert fake.fetched == ["e", "f"] and fake.list_calls == 3
    st = run.state()["accounts"]["default"]
    assert st["pending"] == [] and st["conversations"]["f"] == T[5]
    run(fake)
    assert fake.fetched == [] and fake.list_calls == 1


def test_out_of_order_listing_disables_the_early_stop(run):
    fake = FakeGrok(pages())
    run(fake)
    # Page 1 isn't in modifyTime order (b before the newer a), so the order can't be
    # trusted: keep listing even though page 1 is unchanged — and find the new "x".
    fake.pages = [[("b", T[1]), ("a", T[0])], [("c", T[2]), ("d", T[3])],
                  [("x", "2026-09-01T00:00:00Z"), ("f", T[5])]]
    run(fake)
    assert fake.list_calls == 3 and fake.fetched == ["x"]


def test_starred_conversation_pinned_on_top_does_not_break_the_early_stop(run):
    fake = FakeGrok([[("old", T[5], True), ("a", T[0])], [("b", T[1]), ("c", T[2])], [("d", T[3])]])
    run(fake)
    run(fake)
    assert fake.list_calls == 1 and fake.fetched == []


def test_state_is_per_account(run):
    fake = FakeGrok(pages())
    run(fake, account="default")
    run(fake, account="2172d829")  # never synced: reads everything
    assert fake.fetched == list("abcdef")
    st = run.state()["accounts"]
    assert set(st) == {"default", "2172d829"}
    fake.pages[0][0] = ("a", "2026-10-06T00:00:00Z")
    run(fake, account="2172d829")
    assert fake.fetched == ["a"]
    assert run.state()["accounts"]["default"]["conversations"]["a"] == T[0]  # untouched


def test_complete_listing_prunes_deleted_conversations(run):
    fake = FakeGrok(pages())
    run(fake)
    fake.pages = [[("a", T[0]), ("b", T[1])], [("c", T[2])]]
    run(fake, deep=True)
    assert set(run.state()["accounts"]["default"]["conversations"]) == {"a", "b", "c"}


def test_refresh_metadata_reads_all_and_never_touches_state(run):
    fake = FakeGrok(pages())
    run(fake)
    before = run.state()
    fake.pages[0][0] = ("a", "2026-10-06T00:00:00Z")
    run(fake, refresh=True)
    assert fake.fetched == list("abcdef")
    assert run.state() == before


def test_saved_state_is_ignored_when_the_library_has_nothing_from_the_account(run):
    fake = FakeGrok(pages())
    run(fake)
    run.by_id.clear()  # metadata.json reset
    run(fake)
    assert fake.fetched == list("abcdef")


def test_record_failure_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "FAILED_COUNT", 0)
    job = g._ItemJob("new", {"id": "x"}, "x", "https://e/x.jpg", tmp_path / "x")
    g._record_failure(job, RuntimeError("nope"), argparse.Namespace(failures=tmp_path / "f.json"))
    assert g.FAILED_COUNT == 1


def test_cli_passes_deep_through(monkeypatch):
    import grokive
    seen = []
    monkeypatch.setattr(grokive, "run", lambda cmd: seen.append(cmd) or 0)
    monkeypatch.setattr("sys.argv", ["grokive.py", "conversations", "--deep", "--account", "abc"])
    assert grokive.main() == 0
    assert "--deep" in seen[0] and "--grok-conversations" in seen[0]
    monkeypatch.setattr("sys.argv", ["grokive.py", "conversations"])
    grokive.main()
    assert "--deep" not in seen[1]


def test_server_passes_deep_to_the_conversations_step_only(monkeypatch):
    import os, tempfile
    os.environ.setdefault("GROK_DATA_DIR", tempfile.mkdtemp())
    import server
    calls = []
    monkeypatch.setattr(server, "_load_accounts", lambda: [{"id": "default", "name": "acc1", "active": True}])
    monkeypatch.setattr(server, "_account_configured", lambda _id: True)
    monkeypatch.setattr(server, "_run_step", lambda label, args: calls.append(args) or 0)
    monkeypatch.setattr(server, "_autonomous_enabled", lambda: False)
    server._sync_worker(None, True)
    conv = [a for a in calls if a[2] == "conversations"]
    assert conv and "--deep" in conv[0]
    assert all("--deep" not in a for a in calls if a[2] != "conversations")
    calls.clear()
    server._sync_worker("default")
    assert all("--deep" not in a for a in calls)
