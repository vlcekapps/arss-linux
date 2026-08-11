"""Per-item and summary ``GNotification`` construction for feed updates.

ARSS never plays notification audio itself.  It sends a normal-priority first
item and low-priority remainder/summary through ``GNotification``; GNOME and
the user's system notification settings have final control over sound.

Each of the first eight items gets a stable actionable notification.  For a
multi-item batch, a stable low-priority summary is sent last.  GNOME Shell may
visually group them by application, but ``GNotification`` cannot require
Android-style parent/child grouping.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from typing import Final, Protocol
from urllib.parse import urlsplit

from .monitor import MonitorKind, NewFeedItem


MAX_ITEM_NOTIFICATIONS: Final = 8


class NotificationPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"


@dataclass(frozen=True, slots=True)
class NotificationAction:
    name: str
    signature: str
    value: object


@dataclass(frozen=True, slots=True)
class NotificationSpec:
    identifier: str
    title: str
    body: str
    priority: NotificationPriority
    action: NotificationAction


@dataclass(frozen=True, slots=True)
class NotificationBatch:
    kind: MonitorKind
    items: tuple[NotificationSpec, ...]
    summary: NotificationSpec | None

    @property
    def notifications(self) -> tuple[NotificationSpec, ...]:
        if self.summary is None:
            return self.items
        return (*self.items, self.summary)


class TranslatorProtocol(Protocol):
    def __call__(self, key: str, **values: object) -> str: ...


def build_notification_batch(
    kind: MonitorKind,
    items: Sequence[NewFeedItem],
    translator: TranslatorProtocol,
    *,
    maximum_items: int = MAX_ITEM_NOTIFICATIONS,
) -> NotificationBatch:
    """Build a deterministic feed notification batch without importing Gio."""

    normalized_kind = MonitorKind(kind)
    matching = tuple(item for item in items if item.kind is normalized_kind)
    if not matching:
        raise ValueError("A notification batch requires at least one matching item")
    if maximum_items < 0:
        raise ValueError("maximum_items must not be negative")
    children = tuple(
        _item_spec(
            normalized_kind,
            item,
            translator,
            NotificationPriority.LOW
            if index > 0
            else NotificationPriority.NORMAL,
        )
        for index, item in enumerate(matching[:maximum_items])
    )
    count = len(matching)
    title_key = _count_title_key(normalized_kind, count)
    summary = (
        NotificationSpec(
            identifier=f"arss-{normalized_kind.value}-summary",
            title=translator(title_key, count=count),
            body=translator("notification_body"),
            # The Android summary is silent; LOW is the closest portable
            # GNotification representation and avoids a second prominent alert.
            priority=NotificationPriority.LOW,
            action=_show_kind_action(normalized_kind),
        )
        if count > 1
        else None
    )
    return NotificationBatch(
        kind=normalized_kind,
        items=children,
        summary=summary,
    )


class GNotificationPublisher:
    """Publish pure specs through an application-owned Gio notification API."""

    def __init__(
        self,
        application: object,
        *,
        gio: object | None = None,
        glib: object | None = None,
    ) -> None:
        if gio is None or glib is None:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib

            gio = Gio
            glib = GLib
        self.application = application
        self._gio = gio
        self._glib = glib

    def publish_feed_updates(
        self,
        kind: MonitorKind,
        items: Sequence[NewFeedItem],
        translator: TranslatorProtocol,
        *,
        maximum_items: int = MAX_ITEM_NOTIFICATIONS,
    ) -> NotificationBatch:
        batch = build_notification_batch(
            kind,
            items,
            translator,
            maximum_items=maximum_items,
        )
        self.publish(batch)
        return batch

    def publish(self, batch: NotificationBatch) -> None:
        if batch.summary is None:
            withdraw = getattr(self.application, "withdraw_notification", None)
            if withdraw is not None:
                try:
                    withdraw(f"arss-{batch.kind.value}-summary")
                except Exception:
                    pass
        for spec in batch.notifications:
            notification = self._gio.Notification.new(spec.title)  # type: ignore[attr-defined]
            notification.set_body(spec.body)
            notification.set_priority(
                self._gio.NotificationPriority.LOW  # type: ignore[attr-defined]
                if spec.priority is NotificationPriority.LOW
                else self._gio.NotificationPriority.NORMAL  # type: ignore[attr-defined]
            )
            notification.set_default_action_and_target(
                spec.action.name,
                self._glib.Variant(  # type: ignore[attr-defined]
                    spec.action.signature,
                    spec.action.value,
                ),
            )
            self.application.send_notification(spec.identifier, notification)  # type: ignore[attr-defined]


def _item_spec(
    kind: MonitorKind,
    item: NewFeedItem,
    translator: TranslatorProtocol,
    priority: NotificationPriority,
) -> NotificationSpec:
    fallback_key = "unknown_article" if kind is MonitorKind.RSS else "unknown_episode"
    title = item.article.title.strip() or translator(fallback_key)
    source = item.subscription.title.strip()
    body = source or translator("notification_body")
    identity = "\x1f".join(
        (kind.value, item.subscription.url, item.stable_id)
    )
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
    return NotificationSpec(
        identifier=f"arss-{kind.value}-item-{digest}",
        title=title,
        body=body,
        priority=priority,
        action=_item_action(kind, item),
    )


def _item_action(kind: MonitorKind, item: NewFeedItem) -> NotificationAction:
    article = item.article
    if kind is MonitorKind.RSS and _is_web_url(article.url):
        return NotificationAction("app.open-article", "s", article.url)
    if (
        kind is MonitorKind.PODCAST
        and article.media_url is not None
        and _is_web_url(article.media_url)
    ):
        return NotificationAction(
            "app.play-episode",
            "(ssss)",
            (
                article.title,
                article.url,
                article.media_url,
                article.duration_text or "",
            ),
        )
    return _show_kind_action(kind)


def _show_kind_action(kind: MonitorKind) -> NotificationAction:
    page = "podcast" if kind is MonitorKind.PODCAST else "rss"
    return NotificationAction("app.show-kind", "s", page)


def _count_title_key(kind: MonitorKind, count: int) -> str:
    if kind is MonitorKind.RSS:
        return "new_article_one" if count == 1 else "new_articles"
    return "new_episode_one" if count == 1 else "new_episodes"


def _is_web_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)
    except (AttributeError, ValueError):
        return False
