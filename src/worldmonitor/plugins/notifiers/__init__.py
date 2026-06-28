"""Notifier plugins — output sinks that deliver a ``Notification`` to a channel.

A notifier is the mirror of a connector on the output side (ADR 0067): it ``send()``s a
channel-agnostic :class:`worldmonitor.plugins.base.Notification` to an external channel (Telegram,
and later Slack / email / webhook). Each lives in its own subpackage
(``notifiers/<name>/notifier.py`` + ``config.schema.json``) so the registry can auto-discover it,
just like connectors. The first instance is the Telegram notifier.
"""
