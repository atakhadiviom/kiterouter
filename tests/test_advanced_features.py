import pytest
from starlette.testclient import TestClient

from kiterouter.config import KiteConfig
from kiterouter.router import ProviderRouter
from kiterouter.server import app, router, config, _recent_requests, record_request_log


def test_smart_auto_and_default_model_resolution():
    test_router = ProviderRouter()
    test_router.fallback_chain = ["opencode_free", "cursor"]
    
    # Test auto combo lookup
    combo_auto = test_router.get_combo("auto")
    assert combo_auto is not None
    assert combo_auto["name"] == "auto"
    assert "models" in combo_auto
    assert combo_auto["strategy"] == "fallback"

    # Test default combo lookup
    combo_default = test_router.get_combo("default")
    assert combo_default is not None
    assert combo_default["name"] == "auto"

    # Test get_all_models includes auto and default
    models = test_router.get_all_models()
    model_ids = [m["id"] for m in models]
    assert "auto" in model_ids
    assert "default" in model_ids


def test_recent_requests_and_stats_endpoints():
    client = TestClient(app)

    # Record sample request
    record_request_log(
        model="auto",
        provider="combo/Own",
        tokens_in=100,
        tokens_out=50,
        status="ok",
        latency_ms=250,
        prompt_preview="Hello",
        response_preview="World",
        tokens_saved=12,
    )

    # Test GET /api/stats
    res = client.get("/api/stats")
    assert res.status_code == 200
    stats = res.json()
    assert stats["status"] == "success"
    assert stats["total_requests"] >= 1
    assert stats["successful_requests"] >= 1
    assert stats["tokens_in"] >= 100

    # Test DELETE /api/recent-requests
    del_res = client.delete("/api/recent-requests")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"

    # Verify cleared
    res_after = client.get("/api/recent-requests")
    assert res_after.status_code == 200
    assert len(res_after.json()["requests"]) == 0


def test_config_backup_and_restore_endpoints(tmp_path, monkeypatch):
    test_cfg = KiteConfig()
    monkeypatch.setattr("kiterouter.config.CONFIG_FILE", tmp_path / "config.json")
    test_cfg.combos = {"test-combo": {"name": "test-combo", "models": ["a/b"]}}
    monkeypatch.setattr("kiterouter.server.config", test_cfg)

    client = TestClient(app)

    # Test backup
    backup_res = client.get("/api/config/backup")
    assert backup_res.status_code == 200
    data = backup_res.json()
    assert "combos" in data
    assert "test-combo" in data["combos"]

    # Test restore
    restore_payload = {
        "combos": {"restored-combo": {"name": "restored-combo", "models": ["x/y"]}},
        "enable_rtk": True,
    }
    restore_res = client.post("/api/config/restore", json=restore_payload)
    assert restore_res.status_code == 200
    assert restore_res.json()["status"] == "success"
    assert "restored-combo" in test_cfg.combos
