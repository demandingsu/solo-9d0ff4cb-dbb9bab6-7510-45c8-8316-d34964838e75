from __future__ import annotations

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str


class TraceEntryOut(BaseModel):
    """单份公告（原公告或某次更正）的追溯证据。"""

    code: str = Field(description="公告编号")
    kind: str = Field(description="original 原公告 / correction 更正公告")
    title: str
    published_at: str = Field(description="发布时间")
    deadline_text: str | None = Field(
        description="投标截止时间原文（详情页表格单元原始文本；未声明则为空）"
    )
    deadline_row: int | None = Field(
        description="“投标截止时间”字段所在表格行（1 起；未声明则为空）"
    )
    detail_url: str = Field(description="详情链接")
    detail_sha256: str | None = Field(description="采集时详情 HTML 原始字节的 SHA-256")
    collected_run_id: int | None = Field(description="证据来源的采集轮次 id")
    is_effective: bool = Field(description="该公告是否为当前查询值的生效依据")
    note: str = Field(description="生效/取代/沿用说明")


class TraceabilityOut(BaseModel):
    original_code: str = Field(description="查询输入的原公告编号")
    project_name: str
    current_deadline: str = Field(description="当前生效的投标截止时间")
    effective_code: str = Field(description="生效依据公告编号")
    effective_url: str = Field(description="生效依据公告详情链接")
    is_expired: bool
    entries: list[TraceEntryOut] = Field(
        description="原公告及各次更正，按发布时间升序"
    )
