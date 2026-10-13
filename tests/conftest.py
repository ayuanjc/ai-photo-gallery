"""测试公共夹具。"""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.runtime_config import RuntimeConfig, reset_runtime_config

os.environ.setdefault("URL_TRUSTED_HOSTS", "a.com,immich.test,example.com")


@pytest.fixture(autouse=True)
def isolated_runtime_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离运行时配置：每个测试使用独立的临时持久化路径，防止真实覆盖值泄漏进测试。"""
    persist_path = tmp_path / "runtime_config.json"
    original_init = RuntimeConfig.__init__

    def patched_init(self, settings, persist_path_=None):
        original_init(self, settings, persist_path=persist_path or persist_path_)

    monkeypatch.setattr(RuntimeConfig, "__init__", patched_init)
    reset_runtime_config()
    yield
    reset_runtime_config()


def pytest_collection_modifyitems(config, items):
    """将 async def test 替换为 asyncio.run 同步包装（当 pytest-asyncio 不可用时）。"""
    try:
        import pytest_asyncio  # noqa: F401

        return
    except ImportError:
        pass

    for item in items:
        if not isinstance(item, pytest.Function):
            continue
        target = getattr(item, "_obj", None)
        if target is None or not inspect.iscoroutinefunction(target):
            continue
        original = target

        def sync_wrapper(*args, _orig=original, **kwargs):
            return asyncio.run(_orig(*args, **kwargs))

        item._obj = sync_wrapper


@pytest.fixture
def settings() -> Settings:
    return Settings(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        immich_timeout_seconds=1.0,
        immich_connect_timeout_seconds=1.0,
        immich_max_retries=1,
        immich_retry_backoff_seconds=0.0,
        time_weight=0.3,
        similarity_weight=0.7,
        time_decay_half_life_days=365.0,
        default_datetime_iso="1970-01-01T00:00:00+00:00",
        import_allowed_dirs=["/tmp", "C:\\Users"],
    )


@pytest.fixture
def face_settings() -> Settings:
    return Settings(
        immich_base_url="http://immich.test",
        immich_api_key="test-key",
        huawei_ak="test-ak",
        huawei_sk="test-sk",
        huawei_project_id="test-project",
        huawei_frs_endpoint="https://face.test.myhuaweicloud.com",
        huawei_face_enabled=True,
        huawei_face_max_concurrent=2,
        huawei_face_timeout_seconds=5.0,
        huawei_face_connect_timeout_seconds=2.0,
    )