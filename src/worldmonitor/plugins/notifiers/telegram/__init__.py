"""Telegram notifier — delivers alerts via the Telegram Bot ``sendMessage`` API (ADR 0067)."""

from worldmonitor.plugins.notifiers.telegram.notifier import TelegramNotifier

__all__ = ["TelegramNotifier"]
