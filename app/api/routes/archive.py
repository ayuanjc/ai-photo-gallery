"""归档路由：propose（dry-run）→ commit（写图库）。只读模式下全部拦截。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...core.config import Settings
from ...models.activity import (
    ArchiveCommitRequest,
    ArchiveCommitResponse,
    ArchiveProposeRequest,
    ArchiveProposeResponse,
)
from ...services.archive_service import ArchiveService
from ..deps import get_archive_service, get_settings_dep, rate_limit_guard, verify_api_key
from ...core.readonly import ensure_writable

router = APIRouter(tags=["archive"], dependencies=[Depends(verify_api_key)])


@router.post(
    "/archive/propose",
    response_model=ArchiveProposeResponse,
    summary="归档提案（dry-run，不写图库）",
    description=(
        "只读计算：相册名、是否已存在、将新增多少张。返回 proposal_id，在 ARCHIVE_PROPOSAL_TTL_SECONDS 内可 commit。"
        "比赛演示只读模式下直接 403。"
    ),
)
async def propose_archive(
    payload: ArchiveProposeRequest,
    service: ArchiveService = Depends(get_archive_service),
    settings: Settings = Depends(get_settings_dep),
) -> ArchiveProposeResponse:
    ensure_writable(settings)
    return ArchiveProposeResponse(**await service.propose(payload))


@router.post(
    "/archive/commit",
    response_model=ArchiveCommitResponse,
    summary="确认归档（真正写 Immich 相册）",
    description="按 proposal_id 执行：get_or_create_album + 逐张 add_to_album，返回相册链接与失败清单。",
    dependencies=[Depends(rate_limit_guard)],
)
async def commit_archive(
    payload: ArchiveCommitRequest,
    service: ArchiveService = Depends(get_archive_service),
    settings: Settings = Depends(get_settings_dep),
) -> ArchiveCommitResponse:
    ensure_writable(settings)
    return ArchiveCommitResponse(**await service.commit(payload))


@router.get(
    "/archive/proposals/{proposal_id}",
    response_model=dict,
    summary="查询归档提案剩余有效期",
)
def get_proposal(
    proposal_id: str,
    service: ArchiveService = Depends(get_archive_service),
) -> dict:
    return service.get_proposal(proposal_id)
