"""Inbound email file format (FR-1). Documented in docs/email-format.md."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Sender(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    name: str | None = None


class Attachment(BaseModel):
    """Metadata only; attachment content is out of scope (section 11)."""

    model_config = ConfigDict(extra="forbid")
    filename: str
    content_type: str
    size_bytes: int = Field(ge=0)

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")


class InboundEmail(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    message_id: str = Field(min_length=3)
    from_: Sender = Field(alias="from")
    to: str
    subject: str = ""
    body: str
    received_at: datetime
    in_reply_to: str | None = None
    thread_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    attachments: list[Attachment] = Field(default_factory=list)

    @field_validator("received_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("received_at must include a UTC offset")
        return v

    @property
    def sender(self) -> str:
        return self.from_.email.strip().lower()

    @property
    def has_photos(self) -> bool:
        return any(a.is_image for a in self.attachments)
