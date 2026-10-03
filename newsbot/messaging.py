"""Outbound delivery: Telegram, Slack, and the broadcast that mirrors both."""

import json
import re
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

import requests.exceptions

from newsbot.config import SETTINGS
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.util import _truncate, _with_retry


# Tags this bot emits that Telegram requires to be balanced within one message.
_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|pre|a)\b([^>]*)>", re.IGNORECASE)
# Headroom for the closers/reopeners added at a split plus the "Part i/n" prefix.
_SPLIT_RESERVE = 96


def _hard_split(line: str, limit: int) -> List[str]:
    """Break one over-long line, preferring a space, never inside a tag or entity."""
    pieces: List[str] = []
    while len(line) > limit:
        cut = line.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        # Do not cut inside "<...>" or "&...;".
        for opener, closer in (("<", ">"), ("&", ";")):
            last_open = line.rfind(opener, 0, cut)
            if last_open != -1 and line.find(closer, last_open, cut) == -1:
                cut = last_open if last_open > 0 else cut
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ")
    if line:
        pieces.append(line)
    return pieces


def _rebalance_tags(chunks: List[str]) -> List[str]:
    """Close tags left open at the end of a chunk and reopen them in the next.

    Splitting at blank lines or newlines can land inside a <pre> table or a
    <b> span. Telegram rejects an unbalanced message outright, which loses the
    whole chunk, so each chunk is made self-contained.
    """
    balanced: List[str] = []
    carried: List[Tuple[str, str]] = []  # (name, full opening tag)
    for chunk in chunks:
        chunk = "".join(opening for _, opening in carried) + chunk
        stack: List[Tuple[str, str]] = []
        for match in _TAG_RE.finditer(chunk):
            name = match.group(2).lower()
            if match.group(1):
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index][0] == name:
                        del stack[index:]
                        break
            else:
                stack.append((name, match.group(0)))
        balanced.append(chunk + "".join(f"</{name}>" for name, _ in reversed(stack)))
        carried = stack
    return balanced


def _split_message_html(message: str, max_chars: int) -> List[str]:
    if len(message) <= max_chars:
        return [message]
    limit = max(1, max_chars - _SPLIT_RESERVE)
    sections = message.split("\n\n")
    chunks: List[str] = []
    current = ""
    for section in sections:
        candidate = section if not current else f"{current}\n\n{section}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(section) <= limit:
            current = section
            continue
        current = ""
        for line in section.splitlines():
            for piece in _hard_split(line, limit):
                candidate_line = piece if not current else f"{current}\n{piece}"
                if len(candidate_line) <= limit:
                    current = candidate_line
                else:
                    if current:
                        chunks.append(current)
                    current = piece
        if current:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return _rebalance_tags(chunks)


def _plain_text(message: str) -> str:
    """Telegram HTML reduced to readable plain text, for the parse-error fallback."""
    return unescape(re.sub(r"<[^>]+>", "", message))


def _telegram_post(chat_id: str, message: str, html: bool = True) -> None:
    def _send() -> requests.Response:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        if html:
            payload["parse_mode"] = "HTML"
        return requests.post(
            f"https://api.telegram.org/bot{SETTINGS.telegram_token}/sendMessage",
            json=payload,
            timeout=SETTINGS.request_timeout_seconds,
        )

    response = _with_retry(_send, "telegram_send")
    if html and response.status_code == 400 and "parse entities" in (response.text or "").lower():
        # Malformed markup must not cost the reader the message: resend it as
        # plain text rather than dropping it.
        APP_LOG.warning("telegram_html_rejected resending_plain")
        _telegram_post(chat_id, _plain_text(message), html=False)
        return
    response.raise_for_status()


def send_telegram_message(message: str, chat_id: Optional[str] = None) -> bool:
    target_chat_id = chat_id or SETTINGS.telegram_chat_id
    if not SETTINGS.telegram_token or not target_chat_id:
        APP_LOG.warning("telegram_skipped missing_token_or_chat")
        return False
    try:
        chunks = _split_message_html(message, SETTINGS.telegram_message_max_chars)
        total = len(chunks)
        for index, chunk in enumerate(chunks, 1):
            if total > 1:
                prefix = f"<b>Part {index}/{total}</b>\n"
                chunk = prefix + chunk
            _telegram_post(target_chat_id, chunk)
        APP_LOG.info("telegram_sent chunks=%s", total)
        return True
    except Exception as exc:
        APP_LOG.error("telegram_send_failed detail=%s", exc)
        return False


def _telegram_post_photo(chat_id: str, image: bytes, caption: str) -> None:
    def _send() -> requests.Response:
        return requests.post(
            f"https://api.telegram.org/bot{SETTINGS.telegram_token}/sendPhoto",
            data={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"photo": ("candidates.png", image, "image/png")},
            timeout=SETTINGS.request_timeout_seconds,
        )

    response = _with_retry(_send, "telegram_send_photo")
    response.raise_for_status()


def send_telegram_photo(image: bytes, caption: str = "", chat_id: Optional[str] = None) -> bool:
    """Upload a rendered chart to Telegram.

    `sendPhoto` takes a multipart upload, so the image never needs to be
    reachable from the internet -- which is the whole reason charts are
    feasible on a home lab without exposing a port. Captions are capped at
    1024 characters by Telegram, so this truncates rather than letting the
    API reject the whole upload; the full text always goes out separately as
    its own message.
    """
    target_chat_id = chat_id or SETTINGS.telegram_chat_id
    if not SETTINGS.telegram_token or not target_chat_id:
        APP_LOG.warning("telegram_photo_skipped missing_token_or_chat")
        return False
    try:
        _telegram_post_photo(target_chat_id, image, _truncate(caption, 1024))
        APP_LOG.info("telegram_photo_sent bytes=%s", len(image))
        return True
    except Exception as exc:
        APP_LOG.error("telegram_photo_failed detail=%s", exc)
        return False


# Telegram HTML tags actually emitted elsewhere in this module (see
# `escape`/`_render_candidate` etc.) mapped to Slack's mrkdwn equivalents.
# Order matters: <a href> must run before the generic tag stripper below.
_SLACK_LINK_RE = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.DOTALL)
_SLACK_BOLD_RE = re.compile(r"<b>(.*?)</b>", re.DOTALL)
_SLACK_ITALIC_RE = re.compile(r"<i>(.*?)</i>", re.DOTALL)
_SLACK_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL)
_SLACK_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_slack_text(message: str) -> str:
    """Best-effort conversion of this bot's Telegram HTML to Slack mrkdwn.

    Telegram messages here only ever use <a>, <b>, <i> and <pre> (see the
    grep-verified tag set); anything else is stripped rather than risking a
    stray literal "<...>" in Slack. HTML entities (produced by `escape()` for
    Telegram) are unescaped back to plain text since Slack mrkdwn is not HTML.

    Links are converted to a NUL-delimited placeholder first and rendered
    into Slack's own "<url|text>" syntax only after the generic tag stripper
    runs -- otherwise that angle-bracketed output would itself look like an
    unknown tag and get stripped.
    """
    text = _SLACK_LINK_RE.sub(lambda m: f"\x00{m.group(1)}\x00{m.group(2)}\x00", message)
    text = _SLACK_PRE_RE.sub(lambda m: f"```{m.group(1)}```", text)
    text = _SLACK_BOLD_RE.sub(r"*\1*", text)
    text = _SLACK_ITALIC_RE.sub(r"_\1_", text)
    text = _SLACK_TAG_RE.sub("", text)
    text = re.sub(r"\x00([^\x00]*)\x00([^\x00]*)\x00", r"<\1|\2>", text)
    return unescape(text)


def _slack_post(text: str) -> None:
    def _send() -> requests.Response:
        return requests.post(
            SETTINGS.slack_webhook_url,
            json={"text": text},
            timeout=SETTINGS.request_timeout_seconds,
        )

    response = _with_retry(_send, "slack_send")
    response.raise_for_status()


def send_slack_message(message: str) -> bool:
    if not SETTINGS.slack_webhook_url:
        return False
    try:
        slack_text = _html_to_slack_text(message)
        chunks = _split_message_html(slack_text, SETTINGS.slack_message_max_chars)
        total = len(chunks)
        for index, chunk in enumerate(chunks, 1):
            if total > 1:
                chunk = f"*Part {index}/{total}*\n{chunk}"
            _slack_post(chunk)
        APP_LOG.info("slack_sent chunks=%s", total)
        return True
    except Exception as exc:
        APP_LOG.error("slack_send_failed detail=%s", exc)
        return False


_SLACK_API_BASE = "https://slack.com/api"


def _slack_api(method: str, payload: Dict[str, Any], as_json: bool = False) -> Dict[str, Any]:
    """Call one Slack Web API method and return its parsed body.

    Slack answers HTTP 200 with `{"ok": false, "error": ...}` for application
    errors, so `raise_for_status` alone would treat a missing scope or a
    channel the bot was never invited to as success. This raises on `ok:
    false` instead, carrying Slack's own error code.
    """
    def _send() -> requests.Response:
        headers = {"Authorization": f"Bearer {SETTINGS.slack_bot_token}"}
        if as_json:
            return requests.post(
                f"{_SLACK_API_BASE}/{method}", headers=headers, json=payload,
                timeout=SETTINGS.request_timeout_seconds,
            )
        return requests.post(
            f"{_SLACK_API_BASE}/{method}", headers=headers, data=payload,
            timeout=SETTINGS.request_timeout_seconds,
        )

    response = _with_retry(_send, f"slack_api_{method}")
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"{method} failed: {body.get('error', 'unknown_error')}")
    return body


def send_slack_photo(image: bytes, title: str = "", comment: str = "") -> bool:
    """Upload an image to Slack via the three-step external upload flow.

    `files.upload` was retired in 2025; the replacement reserves a URL, PUTs
    the bytes to it, then completes the upload against a channel. All three
    are outbound calls.

    Silent no-op unless both a bot token and a channel id are configured, so
    a webhook-only install keeps behaving exactly as it did.
    """
    if not SETTINGS.slack_bot_token or not SETTINGS.slack_channel_id:
        SYS_LOG.debug("slack_photo_skipped reason=no_bot_token_or_channel")
        return False
    try:
        reserved = _slack_api(
            "files.getUploadURLExternal",
            {"filename": "candidates.png", "length": len(image)},
        )
        upload_url = reserved.get("upload_url")
        file_id = reserved.get("file_id")
        if not upload_url or not file_id:
            raise RuntimeError("getUploadURLExternal returned no upload target")

        def _put() -> requests.Response:
            return requests.post(
                upload_url,
                files={"file": ("candidates.png", image, "image/png")},
                timeout=SETTINGS.request_timeout_seconds,
            )

        _with_retry(_put, "slack_upload_bytes").raise_for_status()

        # `files` is a JSON-encoded array even on the form-encoded endpoint.
        _slack_api(
            "files.completeUploadExternal",
            {
                "files": json.dumps([{"id": file_id, "title": title or "Trade candidates"}]),
                "channel_id": SETTINGS.slack_channel_id,
                "initial_comment": comment,
            },
        )
        APP_LOG.info("slack_photo_sent bytes=%s", len(image))
        return True
    except Exception as exc:
        APP_LOG.error("slack_photo_failed detail=%s", exc)
        return False


def _broadcast_message(message: str, chat_id: Optional[str] = None) -> bool:
    """Send to Telegram (the source of truth for delivery/dedup logic) and,
    if configured, mirror the same content to Slack as a best-effort extra
    channel -- a Slack failure never affects the Telegram-derived return
    value callers rely on for headline dedup bookkeeping."""
    telegram_ok = send_telegram_message(message, chat_id=chat_id)
    if SETTINGS.slack_webhook_url:
        send_slack_message(message)
    return telegram_ok
