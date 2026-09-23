"""请求模型（场景存储、场景模板与排程接口）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data: dict[str, Any]


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    variables: list[dict[str, Any]] = Field(default_factory=list)
    scenario: dict[str, Any]


class TemplateApplyIn(BaseModel):
    # 两种来源二选一：已保存模板（template_id）或直接提交模板定义（template）
    template_id: int | None = None
    template: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    instance_name: str = ""
