"""Pure stdlib helpers for forwarded email headers, invisible Unicode stripping, and HTML sanitization."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import re
from typing import Optional
import unicodedata
from zoneinfo import ZoneInfo

SGT_TZ = ZoneInfo("Asia/Singapore")

INVISIBLE_CHARS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad\u034f]")
FORWARDED_BLOCK_RE = re.compile(
    r"(?im)^[ \t]*(?:-{5,}\s*Forwarded message\s*-{5,}|Begin forwarded message:)\s*\r?\n"
    r"((?:[A-Za-z][A-Za-z0-9-]*:[ \t]*.*\r?\n?)+)"
)


class PlainEmailHtmlParser(HTMLParser):
    """Minimal stdlib HTML-to-text converter stripping scripts and styles."""

    _SKIP_TAGS = {"script", "style", "noscript", "head", "title"}
    _BLOCK_TAGS = {
        "p",
        "div",
        "tr",
        "li",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lower = tag.lower()
        if lower in self._SKIP_TAGS:
            self._skip += 1
        elif self._skip == 0:
            if lower in self._BLOCK_TAGS:
                self._chunks.append("\n")
            elif lower in ("td", "th"):
                self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in self._SKIP_TAGS:
            if self._skip > 0:
                self._skip -= 1
        elif self._skip == 0:
            if lower in self._BLOCK_TAGS:
                self._chunks.append("\n")
            elif lower in ("td", "th"):
                self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_plain_text(html_body: Optional[str]) -> str:
    """Convert an HTML email excerpt to clean plain text lines."""
    if not html_body:
        return ""
    parser = PlainEmailHtmlParser()
    parser.feed(html_body)
    parser.close()
    return parser.get_text()


def strip_invisible_unicode(text: str) -> str:
    """Remove invisible Unicode Cf format characters while preserving standard whitespace."""
    cleaned = text.replace("\xa0", " ").replace("\u202f", " ")
    cleaned = INVISIBLE_CHARS_RE.sub("", cleaned)
    return "".join(ch for ch in cleaned if unicodedata.category(ch) != "Cf")


def clean_email_text(raw: str) -> str:
    """Normalize non-breaking spaces, strip invisible chars and markdown asterisks."""
    cleaned = strip_invisible_unicode(raw)
    return re.sub(r"\*+", "", cleaned)


def parse_forwarded_date(raw_date: str) -> Optional[datetime]:
    """Parse RFC 5322 or Gmail UI forwarded Date header values."""
    text = strip_invisible_unicode(raw_date).strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass

    normalized = re.sub(r"\s+at\s+", " ", text, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for fmt in (
        "%a, %b %d, %Y %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%a, %b %d, %Y, %I:%M %p",
        "%a, %d %b %Y %I:%M %p",
        "%d %b %Y %I:%M %p",
        "%a, %d %b %Y %H:%M",
        "%d %b %Y %H:%M",
        "%d %b %Y, %H:%M",
        "%a, %b %d, %Y",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def _parse_forwarded_block_meta(
    header_block: str,
) -> tuple[Optional[datetime], Optional[str]]:
    """Extract Date and Subject from a single forwarded header block."""
    block_date: Optional[datetime] = None
    block_subject: Optional[str] = None
    for line in header_block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key_lower = key.strip().lower()
        val_clean = val.strip()
        if key_lower == "date" and block_date is None:
            block_date = parse_forwarded_date(val_clean)
        elif key_lower == "subject" and not block_subject and val_clean:
            block_subject = val_clean
    return block_date, block_subject


def _strip_forwarded_blocks(
    cleaned_body: str,
) -> tuple[str, Optional[datetime], Optional[str]]:
    """Strip up to 5 nested forwarded blocks, preferring innermost parsed Date and Subject."""
    fwd_date: Optional[datetime] = None
    fwd_subject: Optional[str] = None
    for _ in range(5):
        match = FORWARDED_BLOCK_RE.search(cleaned_body)
        if not match:
            break
        block_date, block_subject = _parse_forwarded_block_meta(match.group(1))
        if block_date is not None:
            fwd_date = block_date
        if block_subject:
            fwd_subject = block_subject
        cleaned_body = (
            cleaned_body[: match.start()] + cleaned_body[match.end() :]
        ).strip()
    return cleaned_body, fwd_date, fwd_subject


def _resolve_reference_datetime(
    fwd_date: Optional[datetime],
    received_at: Optional[datetime],
    target_tz: ZoneInfo,
) -> datetime:
    """Resolve reference target-tz datetime from forwarded Date and received_at."""
    received_local: Optional[datetime] = None
    if received_at is not None:
        aware_rec = (
            received_at.replace(tzinfo=timezone.utc)
            if received_at.tzinfo is None
            else received_at
        )
        received_local = aware_rec.astimezone(target_tz)

    if fwd_date is not None and fwd_date.tzinfo is not None:
        return fwd_date.astimezone(target_tz)
    if fwd_date is not None and received_local is not None:
        if abs((received_local.date() - fwd_date.date()).days) <= 2:
            return received_local
        return fwd_date.replace(tzinfo=target_tz)
    if fwd_date is not None:
        return fwd_date.replace(tzinfo=target_tz)
    if received_local is not None:
        return received_local
    return datetime.now(target_tz)


def extract_and_strip_forwarded_header(
    subject: str,
    text: str,
    received_at: Optional[datetime],
    *,
    target_tz: ZoneInfo = SGT_TZ,
) -> tuple[str, str, datetime]:
    """Strip Gmail/Apple forwarded message blocks and compute reference target-tz datetime."""
    cleaned_body, fwd_date, fwd_subject = _strip_forwarded_blocks(clean_email_text(text))
    clean_subject = clean_email_text(subject).strip()

    if re.match(r"^(?:fwd?|fw)\s*:\s*", clean_subject, re.IGNORECASE):
        stripped_subj = re.sub(
            r"^(?:(?:fwd?|fw)\s*:\s*)+", "", clean_subject, flags=re.IGNORECASE
        ).strip()
        candidate = fwd_subject or stripped_subj
        clean_subject = (
            re.sub(r"^(?:(?:fwd?|fw)\s*:\s*)+", "", candidate, flags=re.IGNORECASE).strip()
            or stripped_subj
        )

    ref_dt = _resolve_reference_datetime(fwd_date, received_at, target_tz)
    return clean_subject, cleaned_body, ref_dt
