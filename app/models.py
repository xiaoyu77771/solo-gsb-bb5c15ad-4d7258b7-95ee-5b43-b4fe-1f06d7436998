"""请求模型（场景存储与排程接口）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data: dict[str, Any]
