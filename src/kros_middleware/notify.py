"""Notifications over ntfy and/or SMTP e-mail. Failures are logged, never raised."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

import httpx

from .config import Settings
from .messages import Messages

log = logging.getLogger(__name__)

PROBLEM_EVENTS = {"upload_result_problem", "send_status_problem", "signature_invalid"}


class Notifier:
    def __init__(self, settings: Settings, messages: Messages, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.messages = messages
        self._http = http
        self._suppressed_until: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    def enabled(self, event: str) -> bool:
        return bool(self.settings.notify_channels) and event in self.settings.notify_events

    def suppress(self, key: str, seconds: float) -> bool:
        """Return True if an alert with this key was already sent within the window."""
        now = time.monotonic()
        if self._suppressed_until.get(key, 0) > now:
            return True
        self._suppressed_until[key] = now + seconds
        return False

    def fire(self, event: str, values: dict[str, Any], link: str | None = None) -> None:
        """Schedule a notification without blocking the caller."""
        if not self.enabled(event):
            return
        task = asyncio.create_task(self.send(event, values, link))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def send(self, event: str, values: dict[str, Any], link: str | None = None) -> None:
        if not self.enabled(event):
            return
        title, body = self.messages.render(event, values)
        jobs = []
        if self.settings.ntfy is not None:
            jobs.append(self._ntfy(event, title, body, link))
        if self.settings.email is not None:
            jobs.append(asyncio.to_thread(self._email, title, body))
        for result in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(result, Exception):
                log.warning("notification %s failed: %s", event, type(result).__name__)

    async def _ntfy(self, event: str, title: str, body: str, link: str | None) -> None:
        cfg = self.settings.ntfy
        assert cfg is not None
        payload: dict[str, Any] = {
            "topic": cfg.topic,
            "title": title,
            "message": body,
            "priority": cfg.priority_problem if event in PROBLEM_EVENTS else cfg.priority,
        }
        if cfg.tags:
            payload["tags"] = cfg.tags
        if link:
            # A "view" action only opens the review page; approving always happens there.
            payload["actions"] = [{"action": "view", "label": "Review", "url": link, "clear": True}]
        headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
        client = self._http or httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False)
        try:
            response = await client.post(cfg.url, json=payload, headers=headers)
            response.raise_for_status()
        finally:
            if self._http is None:
                await client.aclose()

    def _email(self, title: str, body: str) -> None:
        cfg = self.settings.email
        assert cfg is not None
        msg = EmailMessage()
        msg["Subject"] = f"{cfg.subject_prefix} {title}".strip()
        msg["From"] = cfg.sender
        msg["To"] = ", ".join(cfg.recipients)
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain=cfg.sender.rpartition("@")[2] or None)
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body)
        context = ssl.create_default_context()
        if cfg.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=context)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        with server:
            if cfg.security == "starttls":
                server.starttls(context=context)
            if cfg.username and cfg.password:
                server.login(cfg.username, cfg.password)
            server.send_message(msg)
