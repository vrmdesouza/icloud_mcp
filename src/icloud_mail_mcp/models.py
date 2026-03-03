"""Pydantic data models shared across icloud_mail_mcp modules."""

from datetime import date, datetime
from typing import Literal

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


class FolderStats(BaseModel):
    """Statistics for an IMAP mailbox folder.

    Attributes:
        folder: Full folder name.
        total_count: Total number of messages in the folder.
        unread_count: Number of unread (UNSEEN) messages.
    """

    folder: str
    total_count: int
    unread_count: int


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
        message_id: RFC 2822 Message-ID header value.
        in_reply_to: In-Reply-To header (parent message-id).
        references: References header (space-separated chain of message-ids).
        reply_to: Reply-To header (user-specified reply address).
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
    message_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    reply_to: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class EmailListResult(BaseModel):
    """Paginated result for email listing.

    Attributes:
        emails: List of Email models for the current page.
        total_count: Total number of emails in the folder.
    """

    emails: list[Email]
    total_count: int


class SearchQuery(BaseModel):
    """Parameters for IMAP SEARCH operations.

    Attributes:
        folder: Folder to search in.
        sender: Maps to IMAP ``FROM`` criterion.
        subject: Maps to IMAP ``SUBJECT`` criterion.
        since: Maps to IMAP ``SINCE`` (inclusive).
        before: Maps to IMAP ``BEFORE`` (exclusive).
        body: Maps to IMAP ``BODY`` criterion.
        is_read: ``True`` → SEEN, ``False`` → UNSEEN, ``None`` → no filter.
        is_flagged: ``True`` → FLAGGED, ``False`` → UNFLAGGED, ``None`` → no filter.
        min_size: Minimum message size in bytes (maps to IMAP ``LARGER``).
        has_attachments: ``True`` → HEADER Content-Type multipart/mixed heuristic.
        limit: Maximum number of results to return (1–100).
    """

    folder: str = "INBOX"
    sender: str | None = None
    subject: str | None = None
    since: date | None = None
    before: date | None = None
    body: str | None = None
    is_read: bool | None = None
    is_flagged: bool | None = None
    min_size: int | None = Field(default=None, ge=0)
    has_attachments: bool | None = None
    limit: int = Field(default=20, ge=1, le=100)


class RuleCondition(BaseModel):
    """A single condition for matching emails in a rule.

    Attributes:
        field: Email field to match against (sender, subject, body).
        operator: Comparison operator (equals, contains, starts_with, ends_with).
        value: Value to compare against.
    """

    field: Literal["sender", "subject", "body"]
    operator: Literal["equals", "contains", "starts_with", "ends_with"]
    value: str


class RuleAction(BaseModel):
    """An action to apply when a rule matches.

    Attributes:
        action_type: Type of action (move, flag, mark_as_read, delete).
        destination: Target folder, required only for 'move' action.
    """

    action_type: Literal["move", "flag", "mark_as_read", "delete"]
    destination: str | None = None


class Rule(BaseModel):
    """An email filtering rule with conditions (AND) and actions.

    Attributes:
        name: Unique name for this rule.
        enabled: Whether this rule is active.
        conditions: List of conditions that must all match (AND logic).
        actions: List of actions to apply when all conditions match.
        created_at: ISO 8601 timestamp of when the rule was created.
    """

    name: str
    enabled: bool = True
    conditions: list[RuleCondition] = Field(default_factory=list)
    actions: list[RuleAction] = Field(default_factory=list)
    created_at: str | None = None
