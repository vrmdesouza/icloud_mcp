"""Pydantic data models shared across icloud_mail_mcp modules."""

from datetime import date, datetime

from pydantic import BaseModel, Field


class Folder(BaseModel):
    """Represents an IMAP mailbox folder.

    Attributes:
        name: Full folder name (e.g. ``"INBOX"``, ``"Deleted Messages"``).
        delimiter: Hierarchy delimiter returned by IMAP LIST.
        flags: IMAP folder flags (e.g. ``\\Noselect``, ``\\HasChildren``).
    """

    name: str
    delimiter: str = "/"
    flags: list[str] = Field(default_factory=list)


class Attachment(BaseModel):
    """Metadata for an email attachment (no binary content).

    Attributes:
        filename: Original filename of the attachment.
        content_type: MIME type (e.g. ``"application/pdf"``).
        size: Size in bytes, if available.
    """

    filename: str
    content_type: str
    size: int | None = None


class Email(BaseModel):
    """Represents a single email message.

    Attributes:
        uid: IMAP UID of the message.
        folder: Folder where this email lives.
        subject: Email subject, decoded from RFC 2047.
        sender: From header value.
        to: List of To recipients.
        cc: List of CC recipients.
        date: Parsed date from the Date header.
        body_text: Plain text body.
        body_html: HTML body.
        is_read: Whether the ``\\Seen`` flag is set.
        attachments: Attachment metadata list.
    """

    uid: str
    folder: str
    subject: str = ""
    sender: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    date: datetime | None = None
    body_text: str = ""
    body_html: str = ""
    is_read: bool = False
    attachments: list[Attachment] = Field(default_factory=list)


class SearchQuery(BaseModel):
    """Parameters for IMAP SEARCH operations.

    Attributes:
        folder: Folder to search in.
        sender: Maps to IMAP ``FROM`` criterion.
        subject: Maps to IMAP ``SUBJECT`` criterion.
        since: Maps to IMAP ``SINCE`` (inclusive).
        before: Maps to IMAP ``BEFORE`` (exclusive).
        body: Maps to IMAP ``BODY`` criterion.
        limit: Maximum number of results to return (1–100).
    """

    folder: str = "INBOX"
    sender: str | None = None
    subject: str | None = None
    since: date | None = None
    before: date | None = None
    body: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
