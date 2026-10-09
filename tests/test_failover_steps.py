"""Exercise cutover ordering without touching drill state or evidence."""
import json

import httpx
import pytest

from dr import failover as fo


@pytest.fixture
def standby(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fo, "LOG", tmp_path / "reports/events.jsonl")
    (tmp_path / "edge").mkdir()
    active = tmp_path / "edge/active_region"
    active.write_text("a\n")
    weights = tmp_path / "state/region-b/weights"
    weights.mkdir(parents=True)
    weights.joinpath("VERSION").write_text("v1\n")
    states = iter([
        {"region": "b", "pool_state": "warm", "weights": False, "count": 0},
        {"region": "b", "pool_state": "full", "weights": True, "count": 4},
    ])
    monkeypatch.setattr(fo, "state_of", lambda region: next(states))
    monkeypatch.setattr(fo.snapshot, "get", lambda *args: {
        "source_region": "a", "embed_model_version": "v1"})
    monkeypatch.setattr(fo.snapshot, "rpo", lambda *args: {
        "rpo_seconds": 10.0, "docs_lost": 2})
    return active


def test_cutover_only_after_ready(standby, monkeypatch):
    responses = iter([503, 200])

    def get(url, timeout):
        assert url.endswith("/readyz") and timeout > 0
        assert standby.read_text().strip() == "a"
        assert standby.parent.parent.joinpath("state/region-b/pool_state").read_text().strip() == "full"
        return httpx.Response(next(responses))

    monkeypatch.setattr(fo.httpx, "get", get)
    monkeypatch.setattr(fo.time, "sleep", lambda seconds: None)
    result = fo.failover("b", "fs", 1)
    assert result["ok"] and result["vector_count"] == 4
    assert standby.read_text().strip() == "b"
    events = [json.loads(line) for line in fo.LOG.read_text().splitlines()]
    assert [event["step"] for event in events] == [
        "1_verify_target", "2_restore_snapshot", "3_scale_pool",
        "4_wait_ready", "5_dns_cutover"]
    assert events[1]["docs_lost"] == 2
    assert events[1]["rpo_seconds"] == 10.0
    assert events[1]["embed_model_version"] == "v1"
    assert all("ts" in event and "iso" in event for event in events)


def test_timeout_preserves_active_region(standby, monkeypatch):
    def blocked(*args, **kwargs):
        raise httpx.ReadTimeout("netblock")

    monkeypatch.setattr(fo.httpx, "get", blocked)
    result = fo.failover("b", "fs", .01)
    assert not result["ok"]
    assert result["failed_step"] == "4_wait_ready"
    assert standby.read_text().strip() == "a"
    assert "5_dns_cutover" not in fo.LOG.read_text()


def test_missing_snapshot_aborts_before_scaling(standby, monkeypatch):
    def missing(*args):
        raise SystemExit("snapshot missing")

    monkeypatch.setattr(fo.snapshot, "get", missing)
    result = fo.failover("b", "fs", 1)
    assert not result["ok"]
    assert result["failed_step"] == "2_restore_snapshot"
    assert standby.read_text().strip() == "a"
    assert not standby.parent.parent.joinpath("state/region-b/pool_state").exists()
