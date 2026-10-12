"""场景相关路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...scene.dictionary import SceneDict
from ..deps import get_scene_dictionary

router = APIRouter(tags=["scene"])


@router.get(
    "/scene/dict",
    summary="获取场景词典",
    description="返回当前生效的场景词典（大场景 / 小场景 / 视觉属性白名单），只读，便于前端展示。",
)
async def get_scene_dict(
    dictionary: SceneDict = Depends(get_scene_dictionary),
) -> dict:
    return dictionary.to_dict()
