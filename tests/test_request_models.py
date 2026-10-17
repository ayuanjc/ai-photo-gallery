"""请求模型字段校验测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.request import SearchRequest


def _valid_payload() -> dict:
    return {
        "query": "海边日落",
        "asset_ids": ["asset-001", "asset-002"],
    }


def test_valid_request():
    req = SearchRequest(**_valid_payload())
    assert req.query == "海边日落"
    assert len(req.asset_ids) == 2
    assert req.limit == 50


def test_query_blank_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), query="   ")))


def test_asset_ids_empty_allowed():
    req = SearchRequest(**(dict(_valid_payload(), asset_ids=[])))
    assert req.asset_ids == []


def test_asset_ids_omitted_allowed():
    req = SearchRequest(query="海边日落")
    assert req.asset_ids == []


def test_limit_out_of_range_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), limit=999999)))


def test_unknown_metadata_field_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), include_metadata_fields=["evil_field"])))


def test_query_whitespace_trimmed():
    req = SearchRequest(**(dict(_valid_payload(), query="  海滩  ")))
    assert req.query == "海滩"

def test_asset_id_empty_string_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), asset_ids=["asset-001", ""])))


def test_asset_id_too_long_rejected():
    long_id = "x" * 129
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), asset_ids=[long_id])))


def test_asset_id_duplicate_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(**(dict(_valid_payload(), asset_ids=["dup", "dup"])))