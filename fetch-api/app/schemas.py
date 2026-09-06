from typing import Literal

from pydantic import BaseModel, Field


PlatformKey = Literal["xiaohongshu", "douyin"]
CreatedTimeFilter = Literal["any", "today", "week", "month", "three_months", "six_months", "year", "older"]
CrawlSpeed = Literal["default", "sloth", "human", "flash"]


class CrawlRequest(BaseModel):
    platform: PlatformKey
    keywords: list[str] = Field(min_length=1, max_length=10)
    count: int = Field(ge=1, le=24)
    created_time: CreatedTimeFilter = "any"
    min_likes: int = Field(default=0, ge=0, le=1_000_000_000)
    speed: CrawlSpeed = "default"


class TaskCreated(BaseModel):
    id: str
    status: str


class AnalysisItemsRequest(BaseModel):
    video_ids: list[int] = Field(min_length=1, max_length=100)


class SharedLinkRequest(BaseModel):
    platform: Literal["xiaohongshu", "douyin", "auto"] = "auto"
    url: str = Field(min_length=1, max_length=4000)


class InspirationUpdate(BaseModel):
    marked: bool


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=8, max_length=128)


class PasswordReset(BaseModel):
    password: str = Field(min_length=8, max_length=128)
