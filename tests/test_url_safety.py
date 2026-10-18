"""URL 安全校验测试（SSRF 防护）。"""
from __future__ import annotations

import pytest

from app.utils.url_safety import is_safe_url, validate_url_or_raise


def test_https_public_domain_safe():
    assert is_safe_url("https://example.com/image.jpg") is True


def test_http_public_domain_safe():
    assert is_safe_url("http://example.com/image.jpg") is True


def test_private_ip_10_rejected():
    assert is_safe_url("http://10.0.0.1/image.jpg") is False


def test_private_ip_172_rejected():
    assert is_safe_url("http://172.16.0.1/image.jpg") is False


def test_private_ip_192_rejected():
    assert is_safe_url("http://192.168.1.1/image.jpg") is False


def test_loopback_rejected():
    assert is_safe_url("http://127.0.0.1/image.jpg") is False


def test_link_local_rejected():
    assert is_safe_url("http://169.254.1.1/image.jpg") is False


def test_ftp_scheme_rejected():
    assert is_safe_url("ftp://example.com/image.jpg") is False


def test_file_scheme_rejected():
    assert is_safe_url("file:///etc/passwd") is False


def test_no_scheme_rejected():
    assert is_safe_url("example.com/image.jpg") is False


def test_public_ip_safe():
    assert is_safe_url("http://8.8.8.8/image.jpg") is True


def test_validate_or_raise_safe():
    assert validate_url_or_raise("https://example.com/a.jpg") == "https://example.com/a.jpg"


def test_validate_or_raise_unsafe():
    with pytest.raises(ValueError, match="不安全"):
        validate_url_or_raise("http://10.0.0.1/a.jpg")
