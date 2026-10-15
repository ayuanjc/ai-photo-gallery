"""水印模块集成测试：处理器端到端流程与 API 路由。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api.deps import get_watermark_processor
from app.main import app
from app.models.watermark import (
    TextWatermarkStyle,
    WatermarkPosition,
    WatermarkRequest,
    WatermarkResponse,
    WatermarkResultItem,
)
from app.services.watermark_processor import BatchWatermarkProcessor


# ---- 处理器端到端 ----


async def test_processor_end_to_end(settings, tmp_path):
    """真实文件经真实 ProcessPool 子进程批量处理成功。"""
    src = tmp_path / "photo.png"
    Image.new("RGBA", (120, 80), (40, 40, 40, 255)).save(src, "PNG")

    processor = BatchWatermarkProcessor(settings)
    response = await processor.process(
        WatermarkRequest(source_paths=[str(src)], template_name="copyright")
    )

    assert response.total == 1
    assert response.succeeded == 1
    assert response.failed == 0
    output_path = response.results[0].output_path
    assert output_path is not None
    assert os.path.exists(output_path)


async def test_processor_invalid_path(settings, tmp_path):
    """源文件不存在时 processor 抛 INVALID_PARAMS。"""
    processor = BatchWatermarkProcessor(settings)
    with pytest.raises(Exception) as exc_info:
        await processor.process(
            WatermarkRequest(source_paths=[str(tmp_path / "missing.jpg")], template_name="copyright")
        )
    assert exc_info.value.code.value == "INVALID_PARAMS"


# ---- API 路由（服务替身）----


class FakeWatermarkProcessor:
    """API 集成测试用处理器替身。"""

    def __init__(self) -> None:
        self.calls: list[WatermarkRequest] = []

    async def process(self, request: WatermarkRequest) -> WatermarkResponse:
        self.calls.append(request)
        return WatermarkResponse(
            total=1,
            succeeded=1,
            failed=0,
            results=[
                WatermarkResultItem(
                    source_path=request.source_paths[0],
                    success=True,
                    output_path="/tmp/out_watermarked.jpg",
                )
            ],
        )


@pytest.fixture
def client():
    """返回依赖覆盖后的 TestClient（watermark processor 使用替身）。"""
    fake = FakeWatermarkProcessor()
    app.dependency_overrides[get_watermark_processor] = lambda: fake
    with TestClient(app) as c:
        c._fake = fake  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


def test_api_list_templates(client):
    """GET /api/v1/watermark/templates 返回预置模板。"""
    resp = client.get("/api/v1/watermark/templates")
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()["templates"]]
    assert "copyright" in names
    assert "copyright-tile" in names


def test_api_list_licenses(client):
    """GET /api/v1/watermark/licenses 返回授权类型。"""
    resp = client.get("/api/v1/watermark/licenses")
    assert resp.status_code == 200
    types = [l["type"] for l in resp.json()["licenses"]]
    assert "copyright" in types
    assert "creative_commons" in types


def test_api_apply_success(client):
    """POST /api/v1/watermark/apply 经替身返回成功结果。"""
    resp = client.post(
        "/api/v1/watermark/apply",
        json={"source_paths": ["/tmp/a.jpg"], "template_name": "copyright"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["succeeded"] == 1
    assert body["results"][0]["output_path"] == "/tmp/out_watermarked.jpg"


def test_api_apply_validation_error(client):
    """空 source_paths 返回 400 统一错误结构。"""
    resp = client.post("/api/v1/watermark/apply", json={"source_paths": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAMS"


def test_api_apply_missing_template(client):
    """无模板且无样式时，真实处理器应抛错；替身仅验证请求模型可通过。"""
    resp = client.post("/api/v1/watermark/apply", json={"source_paths": ["/tmp/a.jpg"]})
    assert resp.status_code == 200


async def test_processor_requires_template_or_style(settings):
    """既无 template_name 也无文字/图片样式时抛 INVALID_PARAMS。"""
    processor = BatchWatermarkProcessor(settings)
    with pytest.raises(Exception) as exc_info:
        await processor.process(WatermarkRequest(source_paths=["/tmp/a.jpg"]))
    assert exc_info.value.code.value == "INVALID_PARAMS"


def test_api_apply_with_custom_text(client):
    """自定义文字水印样式请求通过模型校验。"""
    resp = client.post(
        "/api/v1/watermark/apply",
        json={
            "source_paths": ["/tmp/a.jpg"],
            "text": {"text": "HI", "position": "center", "font_size": 30},
        },
    )
    assert resp.status_code == 200