"""API 响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CommitResponse(BaseModel):
    slice_id: str
    revision: int = Field(..., description="该提交产生的修订号（同时是该片当前版本号）")


class FirstUploadResponse(CommitResponse):
    die_count: int = Field(..., description="首测晶粒数")


class BatchUploadResponse(CommitResponse):
    batch_no: int
    rotation: int
    die_count: int = Field(..., description="本批复测有效落点晶粒数")


class ReorientResponse(CommitResponse):
    batch_no: int
    rotation: int
    affected_dies: int = Field(..., description="本次重算涉及的去重晶粒数")


class DieOut(BaseModel):
    x: int
    y: int
    bin: str
    source: Literal["first", "retest"] = Field(..., description="first=首测，retest=复测")
    batch_no: int | None = Field(None, description="复测来源时的覆盖批号")


class MapPage(BaseModel):
    slice_id: str
    version: int = Field(..., description="本页读取到的片版本号，翻页时可用来校验一致性")
    page: int
    page_size: int
    total: int
    items: list[DieOut]


class BatchInfo(BaseModel):
    batch_no: int
    rotation: int
    revision: int
    die_count: int


class LayoutInfo(BaseModel):
    version: int = Field(..., description="当前启用的布局版号（首次启用为 1）")
    field_count: int = Field(..., description="布局内曝光场数")
    field_width: int = Field(..., description="场宽（格）")
    field_height: int = Field(..., description="场高（格）")
    defect_bins: list[str] = Field(..., description="不良档位集合")
    map_version: int = Field(..., description="统计所对应的判级图版本")


class LayoutEnableResponse(BaseModel):
    slice_id: str
    layout_version: int = Field(..., description="启用后的布局版号")
    map_version: int = Field(..., description="统计所对应的判级图版本")
    field_count: int
    field_width: int
    field_height: int
    defect_bins: list[str]
    recomputed: bool = Field(
        ..., description="读图后启用前判级图有更新，已按最新已提交图重算统计"
    )


class OffsetItem(BaseModel):
    dx: int = Field(..., description="相对场左下格的横向偏移")
    dy: int = Field(..., description="相对场左下格的纵向偏移")
    measured: int = Field(..., description="实测粒数（缺粒不计入）")
    defective: int = Field(..., description="不良粒数")
    defect_fields: list[str] = Field(..., description="该偏移处不良的场号")


class OffsetPage(BaseModel):
    slice_id: str
    layout_version: int = Field(..., description="本页统计所属的布局版号")
    map_version: int = Field(..., description="本页统计对应的判级图版本")
    field_width: int
    field_height: int
    defect_bins: list[str]
    page: int
    page_size: int
    total: int
    items: list[OffsetItem]


class SliceInfo(BaseModel):
    slice_id: str
    version: int
    first_die_count: int
    batches: list[BatchInfo]
    layout: LayoutInfo | None = Field(None, description="启用中的曝光场布局，未启用为 null")


class ErrorBody(BaseModel):
    detail: str
