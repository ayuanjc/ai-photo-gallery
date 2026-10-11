"""华为云 V4 签名工具测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.vision.huawei_signer import HuaweiCloudSigner


def test_sign_returns_required_headers():
    signer = HuaweiCloudSigner("my-ak", "my-sk")
    headers = signer.sign(
        "GET",
        "https://face.cn-north-4.myhuaweicloud.com/v2/proj/face-sets",
        {},
    )
    assert "Authorization" in headers
    assert "host" in headers
    assert "x-sdk-date" in headers
    assert "x-sdk-content-sha256" in headers
    assert headers["host"] == "face.cn-north-4.myhuaweicloud.com"


def test_authorization_format():
    signer = HuaweiCloudSigner("my-ak", "my-sk")
    headers = signer.sign(
        "POST",
        "https://face.cn-north-4.myhuaweicloud.com/v2/proj/face-detect",
        {"Content-Type": "application/json"},
        body='{"image_url":"https://x.com/a.jpg"}',
    )
    auth = headers["Authorization"]
    assert auth.startswith("SDK-HMAC-SHA256 ")
    assert "Access=my-ak" in auth
    assert "SignedHeaders=" in auth
    assert "Signature=" in auth


def test_sign_is_deterministic():
    signer = HuaweiCloudSigner("ak", "sk")
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    h1 = signer.sign("GET", "https://face.test/v2/p/sets", {}, datetime_now=now)
    h2 = signer.sign("GET", "https://face.test/v2/p/sets", {}, datetime_now=now)
    assert h1["Authorization"] == h2["Authorization"]


def test_different_sk_different_signature():
    s1 = HuaweiCloudSigner("ak", "sk1")
    s2 = HuaweiCloudSigner("ak", "sk2")
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    h1 = s1.sign("GET", "https://face.test/v2/p/sets", {}, datetime_now=now)
    h2 = s2.sign("GET", "https://face.test/v2/p/sets", {}, datetime_now=now)
    assert h1["Authorization"] != h2["Authorization"]


def test_different_body_different_signature():
    signer = HuaweiCloudSigner("ak", "sk")
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    h1 = signer.sign("POST", "https://face.test/v2/p/detect", {}, body='{"a":1}', datetime_now=now)
    h2 = signer.sign("POST", "https://face.test/v2/p/detect", {}, body='{"a":2}', datetime_now=now)
    assert h1["Authorization"] != h2["Authorization"]


def test_different_method_different_signature():
    signer = HuaweiCloudSigner("ak", "sk")
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    h1 = signer.sign("GET", "https://face.test/v2/p/sets", {}, datetime_now=now)
    h2 = signer.sign("DELETE", "https://face.test/v2/p/sets", {}, datetime_now=now)
    assert h1["Authorization"] != h2["Authorization"]


def test_canonical_query_sorts_and_encodes():
    result = HuaweiCloudSigner._canonical_query("b=2&a=1&name=hello world")
    assert result == "a=1&b=2&name=hello%20world"


def test_canonical_query_empty():
    assert HuaweiCloudSigner._canonical_query("") == ""


def test_canonical_headers_lowercase_and_sorted():
    canonical, signed_names = HuaweiCloudSigner._canonical_headers(
        {"Content-Type": "application/json", "Host": "example.com"}
    )
    assert canonical == "content-type:application/json\nhost:example.com\n"
    assert signed_names == "content-type;host"


def test_canonical_uri_encodes_spaces():
    result = HuaweiCloudSigner._canonical_uri("/v2/proj id/face-sets")
    assert result == "/v2/proj%20id/face-sets"


def test_canonical_uri_empty_returns_root():
    assert HuaweiCloudSigner._canonical_uri("") == "/"


def test_sign_with_port_in_host():
    signer = HuaweiCloudSigner("ak", "sk")
    headers = signer.sign("GET", "https://face.test:8080/v2/p/sets", {})
    assert headers["host"] == "face.test:8080"


def test_sign_preserves_original_headers():
    signer = HuaweiCloudSigner("ak", "sk")
    original = {"X-Custom": "value"}
    headers = signer.sign("GET", "https://face.test/v2/p/sets", original)
    assert headers.get("X-Custom") == "value"