"""华为云 API 签名工具（SDK-HMAC-SHA256 算法）。

实现华为云 AK/SK 请求签名规范，无需引入官方 SDK 即可调用 REST API。
参考: https://support.huaweicloud.com/devg-apisign/api-sign-algorithm.html

签名流程:
    1. 构造 Canonical Request
    2. 构造 String to Sign
    3. 计算 Signature = HexEncode(HMAC-SHA256(SK, StringToSign))
    4. 添加 Authorization 头
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlparse


class HuaweiCloudSigner:
    """华为云请求签名器（SDK-HMAC-SHA256）。"""

    ALGORITHM = "SDK-HMAC-SHA256"

    def __init__(self, ak: str, sk: str) -> None:
        self._ak = ak
        self._sk = sk

    def sign(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | str | None = None,
        datetime_now: datetime | None = None,
    ) -> dict[str, str]:
        """对请求签名，返回包含 Authorization 及所需头的新 headers dict。

        参数:
            method: HTTP 方法（大写）。
            url: 完整请求 URL。
            headers: 已有请求头（不会被原地修改）。
            body: 请求体。
            datetime_now: 签名时间（默认当前 UTC），用于测试注入。
        """
        signed_headers = dict(headers)
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"

        now = datetime_now or datetime.now(timezone.utc)
        sdk_date = now.strftime("%Y%m%dT%H%M%SZ")

        payload = self._encode_body(body)
        payload_hash = self._sha256_hex(payload)

        signed_headers["host"] = host
        signed_headers["x-sdk-date"] = sdk_date
        signed_headers["x-sdk-content-sha256"] = payload_hash

        canonical_uri = self._canonical_uri(parsed.path)
        canonical_query = self._canonical_query(parsed.query)
        canonical_headers, signed_header_names = self._canonical_headers(signed_headers)

        canonical_request = "\n".join(  # noqa: FLY002
            [
                method.upper(),
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_header_names,
                payload_hash,
            ]
        )

        hashed_canonical = self._sha256_hex(canonical_request.encode("utf-8"))
        string_to_sign = "\n".join([self.ALGORITHM, sdk_date, hashed_canonical])  # noqa: FLY002

        signature = self._hmac_sha256_hex(self._sk.encode("utf-8"), string_to_sign.encode("utf-8"))
        authorization = (
            f"{self.ALGORITHM} "
            f"Access={self._ak}, "
            f"SignedHeaders={signed_header_names}, "
            f"Signature={signature}"
        )
        signed_headers["Authorization"] = authorization
        return signed_headers

    # ---- 内部工具 ----

    @staticmethod
    def _encode_body(body: bytes | str | None) -> bytes:
        if body is None:
            return b""
        if isinstance(body, str):
            return body.encode("utf-8")
        return body

    @staticmethod
    def _sha256_hex(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hmac_sha256_hex(key: bytes, msg: bytes) -> str:
        return hmac.new(key, msg, hashlib.sha256).hexdigest()

    @staticmethod
    def _canonical_uri(path: str) -> str:
        if not path:
            return "/"
        segments = path.split("/")
        encoded = "/".join(quote(s, safe="") for s in segments)
        if not encoded.startswith("/"):
            encoded = "/" + encoded
        return encoded

    @staticmethod
    def _canonical_query(query: str) -> str:
        if not query:
            return ""
        pairs: list[tuple[str, str]] = []
        for part in query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
            else:
                k, v = part, ""
            pairs.append((quote(k, safe=""), quote(v, safe="")))
        pairs.sort()
        return "&".join(f"{k}={v}" for k, v in pairs)

    @staticmethod
    def _canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
        lower: dict[str, str] = {}
        for key, value in headers.items():
            lower_key = key.lower()
            lower[lower_key] = value.strip()
        items = sorted(lower.items())
        canonical = "".join(f"{k}:{v}\n" for k, v in items)
        signed_names = ";".join(k for k, _ in items)
        return canonical, signed_names
