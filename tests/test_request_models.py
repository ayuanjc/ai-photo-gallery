"""请求模型字段校验测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.request import SearchRequest


def _valid_payload() -> dict:
    return {
        "query": "海边日落",
        "image_urls": ["https://example.com/a.jpg", "http://example.com/b.png"],
    }


def test_valid_request():
    req = SearchRequest(**_valid_payload())
    assert req.query == "海边日落"
    assert len(req.image_urls) == 2
    assert req.limit == 50


def test_query_blank_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), query="   ")))


def test_image_urls_empty_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), image_urls=[])))


def test_image_url_invalid_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), image_urls=["not-a-url"])))


def test_limit_out_of_range_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), limit=999999)))


def test_unknown_metadata_field_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), include_metadata_fields=["evil_field"])))


def test_query_whitespace_trimmed():
    req = SearchRequest(**(dict(_valid_payload(), query="  海滩  ")))
    assert req.query == "海滩"