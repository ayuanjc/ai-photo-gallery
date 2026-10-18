"""T1-T5 原生嵌入测试：回调地址解析、工作流同步、hook 加固、回填服务。

全部使用 respx mock，不联网。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx

from app.core.config import Settings
from app.services.backfill_service import BackfillService
from app.services.callback_resolver import (
    resolve_callback_base_url,
    resolve_hook_url,
    verify_container_reachability,
)
from app.services.workflow_manager import WorkflowManager

# ============ T1: 回调地址解析 ============


class TestCallbackResolver:
    """T1: 回调基地址解析三种分支。"""

    @pytest.mark.asyncio
    async def test_explicit_config_used(self):
        """显式配置 plugin_callback_base_url 时直接使用。"""
        settings = Settings(
            plugin_callback_base_url="http://my-host:9000",
            immich_base_url="http://127.0.0.1:3200",
        )
        result = await resolve_callback_base_url(settings)
        assert result == "http://my-host:9000"

    @pytest.mark.asyncio
    async def test_auto_detect_container(self):
        """自动探测：Immich 在容器中 → host.docker.internal。"""
        settings = Settings(
            plugin_callback_base_url="",
            immich_base_url="http://127.0.0.1:3200",
        )
        result = await resolve_callback_base_url(settings, immich_reachable=True)
        assert "host.docker.internal" in result
        assert "3200" in result

    @pytest.mark.asyncio
    async def test_auto_detect_non_container(self):
        """自动探测：Immich 不可达 → 127.0.0.1。"""
        settings = Settings(
            plugin_callback_base_url="",
            immich_base_url="http://192.168.1.100:2283",
        )
        result = await resolve_callback_base_url(settings, immich_reachable=True)
        assert "127.0.0.1" in result

    def test_resolve_hook_url(self):
        """拼接完整 hook URL。"""
        url = resolve_hook_url("http://host.docker.internal:8000")
        assert url == "http://host.docker.internal:8000/api/v1/plugin/hook"

    def test_resolve_hook_url_trailing_slash(self):
        url = resolve_hook_url("http://host.docker.internal:8000/")
        assert url == "http://host.docker.internal:8000/api/v1/plugin/hook"

    @pytest.mark.asyncio
    async def test_verify_reachability_ok(self):
        with respx.mock:
            respx.get("http://test:8000/health").respond(200, json={"status": "ok"})
            result = await verify_container_reachability("http://test:8000/api/v1/plugin/hook")
            assert result is True

    @pytest.mark.asyncio
    async def test_verify_reachability_fail(self):
        result = await verify_container_reachability("http://nonexistent:9999/api/v1/plugin/hook")
        assert result is False


# ============ T2: 工作流幂等 upsert ============


class TestWorkflowSync:
    """T2: 工作流自愈式同步。"""

    @pytest.mark.asyncio
    async def test_sync_creates_missing_workflows(self):
        """所有工作流不存在时全部创建。"""
        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [
            [],
            {"id": "wf1"},
            {"id": "wf1", "name": "yunying-on-upload"},
            {"id": "wf2"},
            {"id": "wf2", "name": "yunying-on-metadata"},
            {"id": "wf3"},
            {"id": "wf3", "name": "yunying-on-tagged"},
        ]
        settings = Settings()
        mgr = WorkflowManager(immich, settings)

        result = await mgr.sync_default_workflows(
            "http://host.docker.internal:8000/api/v1/plugin/hook"
        )

        assert len(result["created"]) == 3
        assert len(result["updated"]) == 0
        assert len(result["unchanged"]) == 0

    @pytest.mark.asyncio
    async def test_sync_unchanged_when_already_correct(self):
        """工作流已存在且配置正确时标记 unchanged。"""
        def make_wf(wf_id, name, trigger, desc):
            return {
                "id": wf_id,
                "name": name,
                "trigger": trigger,
                "description": desc,
                "enabled": True,
                "logging": True,
                "steps": [
                    {
                        "method": "immich-plugin-core#webhook",
                        "config": {
                            "url": "http://host.docker.internal:8000/api/v1/plugin/hook",
                            "method": "POST",
                        },
                        "enabled": True,
                    }
                ],
            }

        existing = [
            make_wf("wf1", "yunying-on-upload", "AssetCreate", "上传即分析：新资产上传后回调云影图鉴进行场景分析+标签写回"),
            make_wf("wf2", "yunying-on-metadata", "AssetMetadataExtraction", "元数据补全：EXIF 提取完成后回调云影图鉴补全场景描述"),
            make_wf("wf3", "yunying-on-tagged", "AssetTagged", "打标后归档：标签添加完成后回调云影图鉴检查是否需要自动归档"),
        ]
        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [existing]
        settings = Settings()
        mgr = WorkflowManager(immich, settings)

        result = await mgr.sync_default_workflows(
            "http://host.docker.internal:8000/api/v1/plugin/hook"
        )

        assert len(result["unchanged"]) == 3
        assert len(result["created"]) == 0

    @pytest.mark.asyncio
    async def test_sync_updates_when_url_wrong(self):
        """工作流存在但 url 错误时更新。"""
        def make_wf(wf_id, name, trigger, desc):
            return {
                "id": wf_id,
                "name": name,
                "trigger": trigger,
                "description": desc,
                "enabled": True,
                "logging": False,
                "steps": [
                    {
                        "method": "immich-plugin-core#webhook",
                        "config": {
                            "url": "http://127.0.0.1:8000/api/v1/plugin/hook",
                            "method": "POST",
                        },
                        "enabled": True,
                    }
                ],
            }

        existing = [
            make_wf("wf1", "yunying-on-upload", "AssetCreate", "上传即分析"),
            make_wf("wf2", "yunying-on-metadata", "AssetMetadataExtraction", "元数据补全"),
            make_wf("wf3", "yunying-on-tagged", "AssetTagged", "打标后归档"),
        ]
        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [
            existing,
            {"id": "wf1"},
            {"id": "wf2"},
            {"id": "wf3"},
        ]
        settings = Settings()
        mgr = WorkflowManager(immich, settings)

        result = await mgr.sync_default_workflows(
            "http://host.docker.internal:8000/api/v1/plugin/hook"
        )

        assert len(result["updated"]) == 3

    @pytest.mark.asyncio
    async def test_delete_stale_workflows(self):
        """删除陈旧工作流（yunying 前缀但不在默认列表中）。"""
        stale_wf = {"id": "stale1", "name": "yunying-old-removed"}
        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [
            [stale_wf],
            None,
        ]
        settings = Settings()
        mgr = WorkflowManager(immich, settings)

        deleted = await mgr.delete_stale_workflows()
        assert "yunying-old-removed" in deleted

    def test_annotate_reachability(self):
        """标注工作流 url 可达性。"""
        workflows = [
            {
                "name": "yunying-on-upload",
                "steps": [
                    {
                        "config": {"url": "http://host.docker.internal:8000/api/v1/plugin/hook"},
                    }
                ],
            },
            {
                "name": "other",
                "steps": [
                    {
                        "config": {"url": "http://127.0.0.1:8000/api/v1/plugin/hook"},
                    }
                ],
            },
        ]
        result = WorkflowManager.annotate_workflow_reachability(
            workflows, "http://host.docker.internal:8000"
        )
        assert result[0]["url_reachable"] is True
        assert result[1]["url_reachable"] is False


# ============ T4: hook 鉴权与幂等 ============


class TestPluginHookSecurity:
    """T4: hook 端点鉴权与幂等。"""

    @pytest.mark.asyncio
    async def test_hook_rejects_wrong_token(self):
        """PLUGIN_HOOK_TOKEN 非空时，错误 token 返回 401。"""
        from app.api.routes.plugin import PluginHookPayload

        settings = Settings(plugin_hook_token="secret123")
        with patch("app.api.routes.plugin.get_settings", return_value=settings):
            from app.api.routes.plugin import plugin_hook

            request = MagicMock()
            request.state.request_id = "test-req"
            request.app.state = MagicMock()
            request.app.state.redis_client = None

            response = MagicMock()
            response.status_code = 200

            payload = PluginHookPayload(event="AssetCreate", asset={"id": "a1"})

            result = await plugin_hook(
                payload=payload,
                request=request,
                response=response,
                x_plugin_token="wrong",
            )

            assert response.status_code == 401
            assert result.continue_ is False

    @pytest.mark.asyncio
    async def test_hook_accepts_correct_token(self):
        """正确 token 通过鉴权。"""
        from app.api.routes.plugin import PluginHookPayload

        settings = Settings(plugin_hook_token="secret123", writeback_enabled=False)
        with patch("app.api.routes.plugin.get_settings", return_value=settings):
            from app.api.routes.plugin import plugin_hook

            request = MagicMock()
            request.state.request_id = "test-req"
            request.app.state = MagicMock()
            request.app.state.redis_client = None

            response = MagicMock()
            response.status_code = 200

            payload = PluginHookPayload(event="AssetCreate", asset={"id": "a1"})

            result = await plugin_hook(
                payload=payload,
                request=request,
                response=response,
                x_plugin_token="secret123",
            )

            assert response.status_code == 200
            assert result.continue_ is True

    @pytest.mark.asyncio
    async def test_hook_no_token_when_not_configured(self):
        """PLUGIN_HOOK_TOKEN 为空时不鉴权。"""
        from app.api.routes.plugin import PluginHookPayload

        settings = Settings(plugin_hook_token="", writeback_enabled=False)
        with patch("app.api.routes.plugin.get_settings", return_value=settings):
            from app.api.routes.plugin import plugin_hook

            request = MagicMock()
            request.state.request_id = "test-req"
            request.app.state = MagicMock()
            request.app.state.redis_client = None

            response = MagicMock()
            response.status_code = 200

            payload = PluginHookPayload(event="AssetCreate", asset={"id": "a1"})

            result = await plugin_hook(
                payload=payload,
                request=request,
                response=response,
                x_plugin_token=None,
            )

            assert result.continue_ is True

    def test_dedup_key_deterministic(self):
        """相同输入生成相同 dedup key。"""
        from app.api.routes.plugin import _dedup_key

        k1 = _dedup_key("AssetCreate", {"id": "a1", "updatedAt": "2026-01-01"})
        k2 = _dedup_key("AssetCreate", {"id": "a1", "updatedAt": "2026-01-01"})
        assert k1 == k2

    def test_dedup_key_different_input(self):
        """不同输入生成不同 dedup key。"""
        from app.api.routes.plugin import _dedup_key

        k1 = _dedup_key("AssetCreate", {"id": "a1"})
        k2 = _dedup_key("AssetTagged", {"id": "a1"})
        assert k1 != k2


# ============ T5: 回填分页与跳过已打标（T7 改造后） ============


class TestBackfillService:
    """T7 改造后的历史资产回填：真实分析接线 + skip 集合 + Redis 进度。"""

    @pytest.mark.asyncio
    async def test_backfill_status_initial(self):
        """初始状态为 idle。"""
        immich = MagicMock()
        settings = Settings()
        svc = BackfillService(immich, settings)
        status = await svc.get_status()
        assert status["status"] == "idle"
        assert status["processed"] == 0

    @pytest.mark.asyncio
    async def test_backfill_skips_tagged_assets(self):
        """skip_tagged=True：批量加载 AI· 标签资产集合，命中即跳过。"""
        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [
            # 1) GET /api/tags → 一个 AI· 标签一个普通标签
            [{"id": "t1", "value": "AI·自然"}, {"id": "t2", "value": "旅行"}],
            # 2) GET /api/tags/t1/assets → a1 已打标
            {"assets": {"items": [{"id": "a1"}]}},
            # 3) POST /api/search/metadata 第一页
            {"assets": {"items": [{"id": "a1"}, {"id": "a2"}]}},
            # 4) 第二页空
            {"assets": {"items": []}},
        ]
        settings = Settings(writeback_enabled=False)
        svc = BackfillService(immich, settings)

        result = await svc.run_backfill(limit=10, skip_tagged=True)

        assert result["status"] == "completed"
        # a1 在 skip 集合；a2 无 analyzer（未接线）→ skipped
        assert result["skipped"] >= 2

    @pytest.mark.asyncio
    async def test_backfill_analyzer_writes_analyzed_only(self):
        """接线 analyzer：source=analyzed 才交给 SceneWriteBack，其他 skipped。"""
        from app.scene.models import SceneTags

        analyzed = SceneTags(
            primary_scene="nature", scenes=["nature"], sub_scenes=[],
            visual_attrs={}, confidence={"nature": 0.9}, source="analyzed",
        )
        fallback = SceneTags(
            primary_scene="life", scenes=["life"], sub_scenes=[],
            visual_attrs={}, confidence={}, source="fallback",
        )
        analyzer = MagicMock()
        analyzer.analyze = AsyncMock(side_effect=[analyzed, fallback])

        writeback = MagicMock()
        writeback.writeback_scene_tags = AsyncMock(return_value=[])

        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [
            {"assets": {"items": [{"id": "a1"}, {"id": "a2"}]}},
            {"assets": {"items": []}},
        ]
        settings = Settings(writeback_enabled=True)
        svc = BackfillService(
            immich, settings, scene_analyzer=analyzer, scene_writeback=writeback
        )

        result = await svc.run_backfill(skip_tagged=False)

        assert result["status"] == "completed"
        assert result["succeeded"] == 1  # 仅 analyzed
        assert result["skipped"] == 1  # fallback 被白名单拦截
        # 只把 analyzed 资产交给写回
        write_args = writeback.writeback_scene_tags.call_args[0][0]
        assert write_args[0]["asset_id"] == "a1"
        assert write_args[0]["scene"]["source"] == "analyzed"

    @pytest.mark.asyncio
    async def test_backfill_dry_run_analyzes_without_writeback(self):
        """dry_run=True：跑分析但不调用写回。"""
        from app.scene.models import SceneTags

        analyzer = MagicMock()
        analyzer.analyze = AsyncMock(return_value=SceneTags(
            primary_scene="nature", scenes=["nature"], sub_scenes=[],
            visual_attrs={}, confidence={"nature": 0.9}, source="analyzed",
        ))
        writeback = MagicMock()
        writeback.writeback_scene_tags = AsyncMock(return_value=[])

        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [
            {"assets": {"items": [{"id": "a1"}]}},
            {"assets": {"items": []}},
        ]
        settings = Settings(writeback_enabled=True)
        svc = BackfillService(
            immich, settings, scene_analyzer=analyzer, scene_writeback=writeback
        )

        result = await svc.run_backfill(dry_run=True, skip_tagged=False)

        assert result["status"] == "completed"
        assert result["dry_run"] is True
        assert result["succeeded"] == 1
        writeback.writeback_scene_tags.assert_not_called()

    @pytest.mark.asyncio
    async def test_backfill_pagination(self):
        """分页拉取：第一页满页，第二页空。"""
        immich = MagicMock()
        immich.request_json = AsyncMock()
        page1_items = [{"id": f"a{i}"} for i in range(1000)]
        immich.request_json.side_effect = [
            [{"id": "t1", "value": "AI·自然"}],  # GET /api/tags（skip 集合）
            {"assets": {"items": []}},  # GET /api/tags/t1/assets → 空
            {"assets": {"items": page1_items}},
            {"assets": {"items": []}},
        ]
        settings = Settings(writeback_enabled=False, backfill_page_size=1000)
        svc = BackfillService(immich, settings)

        result = await svc.run_backfill(skip_tagged=True)

        assert result["status"] == "completed"
        assert result["total"] == 1000

    @pytest.mark.asyncio
    async def test_backfill_concurrent_running_rejected(self):
        """回填任务运行中时拒绝第二次启动。"""
        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.return_value = {"assets": {"items": []}}
        settings = Settings()
        svc = BackfillService(immich, settings)
        svc._running = True

        result = await svc.run_backfill()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_backfill_progress_to_redis(self):
        """进度快照写 Redis；任务不在内存时从 Redis 恢复展示。"""
        import json as _json

        store: dict[str, str] = {}

        class _FakeRedis:
            async def set(self, key, value, **kwargs):
                store[key] = value

            async def get(self, key):
                return store.get(key)

        class _FakeRedisClient:
            connected = True

            @property
            def redis(self):
                return _FakeRedis()

        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [
            {"assets": {"items": [{"id": "a1"}]}},
            {"assets": {"items": []}},
        ]
        settings = Settings(writeback_enabled=False)
        rc = _FakeRedisClient()
        svc = BackfillService(immich, settings, redis_client=rc)

        result = await svc.run_backfill(skip_tagged=True)
        assert result["status"] == "completed"

        snapshot = _json.loads(store["backfill:progress"])
        assert snapshot["status"] == "completed"
        # 内存不在运行时 → 从 Redis 读快照
        assert (await svc.get_status())["status"] == "completed"

    @pytest.mark.asyncio
    async def test_backfill_tag_load_failure_degrades(self):
        """skip 集合加载失败：降级为空集合继续跑，不抛异常。"""
        immich = MagicMock()
        immich.request_json = AsyncMock()
        immich.request_json.side_effect = [
            RuntimeError("tags down"),  # GET /api/tags 失败
            {"assets": {"items": [{"id": "a1"}]}},
            {"assets": {"items": []}},
        ]
        settings = Settings(writeback_enabled=False)
        svc = BackfillService(immich, settings)

        result = await svc.run_backfill(skip_tagged=True)
        assert result["status"] == "completed"


# ============ T3: 相册写回被调用 ============


class TestAlbumWritebackIntegration:
    """T3: 验证 album_writeback 在 hook 中被调用。"""

    @pytest.mark.asyncio
    async def test_album_writeback_called_on_asset_create(self):
        """AssetCreate 事件触发 album_writeback.writeback_albums。"""
        from app.api.routes.plugin import PluginHookPayload
        from app.services.immich_writeback import WriteBackEntry

        settings = Settings(plugin_hook_token="", writeback_enabled=True)
        with patch("app.api.routes.plugin.get_settings", return_value=settings):
            from app.api.routes.plugin import plugin_hook

            request = MagicMock()
            request.state.request_id = "test-req"
            request.app.state = MagicMock()
            request.app.state.redis_client = None

            scene_wb = AsyncMock()
            scene_wb.writeback_scene_tags = AsyncMock(return_value=[])

            album_wb = AsyncMock()
            album_wb.writeback_albums = AsyncMock(return_value=[
                WriteBackEntry(action="add_to_album", target="album1", success=True)
            ])

            desc_wb = AsyncMock()
            desc_wb.writeback_descriptions = AsyncMock(return_value=[])

            request.app.state.scene_writeback = scene_wb
            request.app.state.album_writeback = album_wb
            request.app.state.description_writeback = desc_wb

            response = MagicMock()
            response.status_code = 200

            payload = PluginHookPayload(
                event="AssetCreate",
                asset={
                    "id": "a1",
                    "scene": {"primary_scene": "nature", "source": "analyzed"},
                },
            )

            result = await plugin_hook(
                payload=payload,
                request=request,
                response=response,
                x_plugin_token=None,
            )

            album_wb.writeback_albums.assert_called_once()
            assert "albums_updated" in result.changes

    @pytest.mark.asyncio
    async def test_album_writeback_not_called_for_unanalyzed_scene(self):
        """自定义 payload 的 scene 无 source=analyzed 标记时不写回（猜测值禁写）。"""
        from app.api.routes.plugin import PluginHookPayload

        settings = Settings(plugin_hook_token="", writeback_enabled=True)
        with patch("app.api.routes.plugin.get_settings", return_value=settings):
            from app.api.routes.plugin import plugin_hook

            request = MagicMock()
            request.state.request_id = "test-req"
            request.app.state = MagicMock()
            request.app.state.redis_client = None

            album_wb = AsyncMock()
            album_wb.writeback_albums = AsyncMock(return_value=[])

            request.app.state.scene_writeback = AsyncMock()
            request.app.state.album_writeback = album_wb
            request.app.state.description_writeback = AsyncMock()

            payload = PluginHookPayload(
                event="AssetCreate",
                asset={"id": "a1", "scene": {"primary_scene": "nature"}},
            )

            await plugin_hook(
                payload=payload,
                request=request,
                response=MagicMock(),
                x_plugin_token=None,
            )

            album_wb.writeback_albums.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_writeback_called_on_asset_tagged(self):
        """AssetTagged 事件不再触发 album_writeback（防事件循环放大）。"""
        from app.api.routes.plugin import PluginHookPayload

        settings = Settings(plugin_hook_token="", writeback_enabled=True)
        with patch("app.api.routes.plugin.get_settings", return_value=settings):
            from app.api.routes.plugin import plugin_hook

            request = MagicMock()
            request.state.request_id = "test-req"
            request.app.state = MagicMock()
            request.app.state.redis_client = None

            scene_wb = AsyncMock()
            album_wb = AsyncMock()
            album_wb.writeback_albums = AsyncMock(return_value=[])
            desc_wb = AsyncMock()

            request.app.state.scene_writeback = scene_wb
            request.app.state.album_writeback = album_wb
            request.app.state.description_writeback = desc_wb

            response = MagicMock()
            response.status_code = 200

            payload = PluginHookPayload(
                event="AssetTagged",
                asset={"id": "a1", "tags": ["AI·自然"]},
            )

            await plugin_hook(
                payload=payload,
                request=request,
                response=response,
                x_plugin_token=None,
            )

            album_wb.writeback_albums.assert_not_called()
