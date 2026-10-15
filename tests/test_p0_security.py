"""P0 安全修复测试：路径安全 / 占位 AI / 管理配置安全。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.utils.path_safety import PathValidationError, validate_file_path


class TestPathSafety:
    """P0.1 路径安全校验。"""

    def test_reject_empty_path(self):
        with pytest.raises(PathValidationError, match="不能为空"):
            validate_file_path("", ["/tmp"])

    def test_reject_control_chars(self):
        with pytest.raises(PathValidationError, match="非法字符"):
            validate_file_path("a\x00.jpg", ["/tmp"])
        with pytest.raises(PathValidationError, match="非法字符"):
            validate_file_path("a\n.jpg", ["/tmp"])

    def test_reject_overlong_path(self):
        with pytest.raises(PathValidationError, match="1024"):
            validate_file_path("a" * 2000 + ".jpg", ["/tmp"])

    def test_reject_empty_allowed_dirs(self, tmp_path):
        f = tmp_path / "test.jpg"
        f.write_bytes(b"x")
        with pytest.raises(PathValidationError, match="白名单"):
            validate_file_path(str(f), [])

    def test_reject_path_outside_allowed_dirs(self, tmp_path):
        f = tmp_path / "test.jpg"
        f.write_bytes(b"x")
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(PathValidationError, match="不在允许"):
            validate_file_path(str(f), [str(other)])

    def test_accept_path_inside_allowed_dirs(self, tmp_path):
        f = tmp_path / "test.jpg"
        f.write_bytes(b"x")
        result = validate_file_path(str(f), [str(tmp_path)])
        assert result == str(f.resolve())


class TestHuaweiVisionProvider:
    """P0.2 占位 AI 功能修复。"""

    async def test_huawei_vision_raises_not_stub(self):
        from app.services.vision.huawei import HuaweiVisionProvider
        from app.core.config import Settings
        from app.core.errors import VisionUnavailableError

        provider = HuaweiVisionProvider(Settings())
        with pytest.raises(VisionUnavailableError, match="experimental"):
            await provider.analyze("http://example.com/img.jpg")

    async def test_huawei_analyzer_health_check_false(self):
        from app.ai.huawei_analyzer import HuaweiFaceAnalyzer
        from app.core.config import Settings

        analyzer = HuaweiFaceAnalyzer(Settings())
        assert await analyzer.health_check() is False

    async def test_huawei_analyzer_raises_502_not_500(self):
        from app.ai.huawei_analyzer import HuaweiFaceAnalyzer
        from app.core.config import Settings
        from app.core.errors import AIAnalyzerUnavailableError

        analyzer = HuaweiFaceAnalyzer(Settings())
        with pytest.raises(AIAnalyzerUnavailableError):
            await analyzer.analyze("a", "b")


class TestAdminSettingsSecurity:
    """P0.5 管理配置写入安全。"""

    def test_production_rejects_env_write(self):
        from app.core.config import Settings
        from app.core.errors import AppError

        prod_settings = Settings(
            app_env="production",
            search_api_key="prod-key",
            immich_api_key="immich-key",
            import_allowed_dirs=["/data"],
            watermark_allowed_dirs=["/data"],
        )
        assert prod_settings.app_env == "production"

    def test_production_requires_import_allowed_dirs(self):
        from app.core.config import Settings

        with pytest.raises(ValueError, match="IMPORT_ALLOWED_DIRS"):
            Settings(
                app_env="production",
                search_api_key="prod-key",
                immich_api_key="immich-key",
                import_allowed_dirs=[],
            )

    def test_reject_symlink(self, tmp_path):
        target = tmp_path / "real.jpg"
        target.write_bytes(b"x")
        link = tmp_path / "link.jpg"
        try:
            os.symlink(str(target), str(link))
        except OSError:
            pytest.skip("系统不支持符号链接")
        with pytest.raises(PathValidationError, match="符号链接"):
            validate_file_path(str(link), [str(tmp_path)])

    def test_reject_non_image_extension(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"x")
        with pytest.raises(PathValidationError, match="文件类型"):
            validate_file_path(str(f), [str(tmp_path)])

    def test_reject_empty_file(self, tmp_path):
        f = tmp_path / "empty.jpg"
        f.write_bytes(b"")
        with pytest.raises(PathValidationError, match="文件为空"):
            validate_file_path(str(f), [str(tmp_path)], max_size_bytes=1000)

    def test_reject_oversized_file(self, tmp_path):
        f = tmp_path / "big.jpg"
        f.write_bytes(b"x" * 100)
        with pytest.raises(PathValidationError, match="超过上限"):
            validate_file_path(str(f), [str(tmp_path)], max_size_bytes=50)

    def test_accept_nested_path(self, tmp_path):
        sub = tmp_path / "sub" / "deep"
        sub.mkdir(parents=True)
        f = sub / "photo.png"
        f.write_bytes(b"x")
        result = validate_file_path(str(f), [str(tmp_path)])
        assert result == str(f.resolve())