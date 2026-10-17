"""Data models for CAD sales bot (闲鱼CAD代画销转)."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class CadMessage(BaseModel):
    role: str = Field(..., description="buyer / seller / user / assistant")
    content: str = Field(..., description="message content")
    timestamp: str | None = None


class CadSalesRequest(BaseModel):
    conversation: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of messages in conversation with 'role' and 'content'",
    )
    attachments: list[str] = Field(
        default_factory=list,
        description="List of attachment filenames or URLs",
    )
    account_id: str = "default_account"
    conversation_id: str = "default_conv"


class CadSalesResult(BaseModel):
    tier: str = Field(..., description="Order tier: A, B, or C")
    missing_fields: list[str] = Field(
        default_factory=list,
        description="List of missing checklist fields",
    )
    intent: str = Field(..., description="Classified intent or scenario")
    reply_draft: str = Field(..., description="Short Chinese Xianyu reply draft")
    quote_range: str | None = Field(
        None,
        description="Price range placeholder or estimate in RMB, only for Tier A",
    )
    needs_human: bool = Field(
        False,
        description="Whether operator intervention is required",
    )
    reason: str = Field(..., description="Reasoning for tier and draft")


class RequirementsRequest(BaseModel):
    conversation: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    order_id: str | None = None
    output_root: str | None = None


class RequirementsResponse(BaseModel):
    order_id: str
    order_dir: str
    requirements_file: str
    tier: str
    summary: dict[str, Any]
