"""防御性编程修复测试。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.utils.date_parse import parse_date_param
from app.utils.url_safety import is_safe_url


class TestUrlSafetyDefensive:
    """P0 SSRF 修复：DNS 解析失败 fail-closed + userinfo 拒绝。"""

    def test_reject_userinfo_in_url(self):
        assert is_safe_url("https://user:pass@example.com/img.jpg") is False

    def test_reject_ftp_scheme(self):
        assert is_safe_url("ftp://example.com/file") is False

    def test_reject_private_ip(self):
        assert is_safe_url("http://192.168.1.1/img.jpg") is False
        assert is_safe_url("http://10.0.0.1/img.jpg") is False
        assert is_safe_url("http://172.16.0.1/img.jpg") is False

    def test_reject_loopback(self):
        assert is_safe_url("http://127.0.0.1/img.jpg") is False

    def test_trusted_host_bypasses_check(self):
        import importlib
        import os

        original = os.environ.get("URL_TRUSTED_HOSTS", "")
        os.environ["URL_TRUSTED_HOSTS"] = "my-immich.local"
        import app.utils.url_safety
        importlib.reload(app.utils.url_safety)
        from app.utils.url_safety import is_safe_url as _is_safe
        try:
            assert _is_safe("http://my-immich.local/api/assets/1") is True
        finally:
            os.environ["URL_TRUSTED_HOSTS"] = original
            importlib.reload(app.utils.url_safety)


class TestDateParse:
    """P1 日期参数解析。"""

    def test_none_returns_none(self):
        assert parse_date_param(None) is None

    def test_empty_string_returns_none(self):
        assert parse_date_param("") is None
        assert parse_date_param("  ") is None

    def test_iso_with_timezone(self):
        dt = parse_date_param("2024-06-15T10:00:00+08:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2024
        assert dt.month == 6

    def test_iso_with_z_suffix(self):
        dt = parse_date_param("2024-06-15T10:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_iso_without_timezone_defaults_utc(self):
        dt = parse_date_param("2024-06-15T10:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError, match="格式无效"):
            parse_date_param("not-a-date")
        with pytest.raises(ValueError, match="格式无效"):
            parse_date_param("2024-13-45")

    def test_field_name_in_error(self):
        with pytest.raises(ValueError, match="date_from"):
            parse_date_param("bad", field_name="date_from")


class TestLivezReadyz:
    """P2 /livez /readyz 端点。"""

    def test_livez_returns_ok(self):
        from app.main import app
        with TestClient(app) as client:
            resp = client.get("/livez")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    def test_readyz_returns_checks(self):
        from app.main import app
        with TestClient(app) as client:
            resp = client.get("/readyz")
            assert resp.status_code == 200
            body = resp.json()
            assert "checks" in body
            assert "immich" in body["checks"]


class TestRankingTiebreaker:
    """P2 排序确定性 tiebreaker。"""

    def test_same_score_sorted_by_asset_id(self):
        from app.services.ranking import rank_assets

        assets = [
            {"id": "zzz", "score": 0.5},
            {"id": "aaa", "score": 0.5},
            {"id": "mmm", "score": 0.5},
        ]
        scored = rank_assets(
            assets,
            time_weight=0.3,
            similarity_weight=0.7,
            half_life_days=365.0,
            default_iso="1970-01-01T00:00:00+00:00",
        )
        ids = [a[0]["id"] for a in scored]
        assert ids == ["aaa", "mmm", "zzz"]