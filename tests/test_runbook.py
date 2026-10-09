"""Checklist control flow with isolated logs and simulated HTTP responses."""
import json
from unittest.mock import Mock

import httpx
import pytest

from dr import runbook as rb


@pytest.fixture
def drill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rb, "LOG", tmp_path / "runbook.jsonl")
    monkeypatch.setattr(rb, "CONFIRM_INTERVAL", 0)
    probes = Mock(side_effect=lambda region, timeout: (False, "not ready"))
    monkeypatch.setattr(rb.hc, "probe", probes)
    failover = Mock(return_value={"ok": True, "cutover": True,
                                 "weights": True, "vector_count": 20,
                                 "embed_model_version": "v1",
                                 "rpo_seconds": 5, "docs_lost": 2})
    monkeypatch.setattr(rb.fo, "failover", failover)
    get = Mock(return_value=httpx.Response(200, json={"region": "b"}))
    monkeypatch.setattr(rb.httpx, "get", get)
    return probes, failover, get


def events():
    return [json.loads(line) for line in rb.LOG.read_text().splitlines()]


def test_complete_checklist_calls_failover_once(drill, monkeypatch, tmp_path):
    probes, failover, get = drill
    chaos = tmp_path / "chaos/chaos-events.jsonl"
    chaos.parent.mkdir()
    chaos.write_text(json.dumps({"action": "kill", "region": "a", "ts": 1}) + "\n")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    result = rb.run("a", "b", "fs", auto=False)
    assert result["ok"]
    failover.assert_called_once_with("b", "fs", wait=60.0)
    assert probes.call_count == 6 and get.call_count == 10
    records = events()
    assert [event["step"] for event in records] == list(range(1, 8))
    assert records[1]["ts"] > records[1]["t_outage"] == 1
    assert records[3]["vector_count"] == 20
    assert len(records[5]["requests"]) == 10
    assert records[6]["elapsed_s"] >= 0


def test_default_confirmation_declines_empty_input(drill, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    result = rb.run("a", "b", "fs", auto=False)
    assert not result["ok"] and not result["cutover"]
    drill[1].assert_not_called()
    drill[2].assert_not_called()
    assert events()[-1]["step"] == 7


def test_primary_recovery_prevents_failover(drill):
    drill[0].side_effect = [(False, "fail"), (False, "standby"),
                            (True, "ready"), (False, "standby"),
                            (False, "fail"), (False, "standby")]
    assert not rb.run("a", "b", "fs", auto=True)["ok"]
    drill[1].assert_not_called()


def test_failed_cutover_skips_requests(drill):
    drill[1].return_value = {"ok": False, "failed_step": "4_wait_ready",
                            "reason": "timeout"}
    assert not rb.run("a", "b", "fs", auto=True)["ok"]
    drill[1].assert_called_once()
    drill[2].assert_not_called()
    assert events()[-2]["skipped"]


def test_golden_signal_errors_keep_cutover_visible(drill):
    drill[2].side_effect = httpx.ReadTimeout("blocked")
    result = rb.run("a", "b", "fs", auto=True)
    assert not result["ok"] and result["cutover"]
    assert result["error_rate"] == 1
    assert drill[2].call_count == 10
    drill[1].assert_called_once()


def test_confirm_eof_and_auto(monkeypatch):
    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert not rb.confirm(False, "Approve?")
    assert rb.confirm(True, "Approve?")
