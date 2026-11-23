import calendar
import re
from datetime import date
from typing import Annotated
from uuid import UUID

from pydantic import Field

from .models import Model


class Submission(Model):
    prompt: Annotated[str, Field(min_length=1, max_length=6000)]
    model: Annotated[str, Field(min_length=1, max_length=150)] | None = None
    conversation: UUID | None = None


def has_window(text, quote=None):
    """Recognize explicit calendar language even when a model paraphrases a date range."""
    months = (*calendar.month_name[1:], *calendar.month_abbr[1:])
    calendar_words = "|".join(
        (
            *months,
            *calendar.day_name,
            *calendar.day_abbr,
            "today",
            "tomorrow",
            "weekend",
            "spring",
            "summer",
            "autumn",
            "fall",
            "winter",
            "christmas",
            "thanksgiving",
            "easter",
            "whenever",
        )
    )
    if re.search(
        r"\b("
        + calendar_words
        + r")\b|\b\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\b|\b(next|this|coming) (week|month|year)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if quote and quote.casefold() in text.casefold():
        return bool(
            re.search(
                r"\b(after|before|during|over|flexible|anytime|festival|holiday|break|choose)\b",
                quote,
                re.IGNORECASE,
            )
        )
    return False


def explicit_travel_dates(text):
    """Read exact endpoints when the traveler labels both departure and return."""
    months = {
        name.casefold(): number
        for names in (calendar.month_name, calendar.month_abbr)
        for number, name in enumerate(names)
        if name
    }
    date_text = (
        rf"\d{{4}}-\d{{2}}-\d{{2}}|(?:{'|'.join(months)})\.?\s+\d{{1,2}},?\s+\d{{4}}"
    )

    def read_after(label):
        matches = re.findall(
            rf"\b(?:{label})\s*(?:on\s+|:\s*)?({date_text})\b", text, re.IGNORECASE
        )
        if len(matches) != 1:
            return None
        raw = matches[0]
        try:
            if "-" in raw:
                return date.fromisoformat(raw)
            month, day, year = raw.replace(",", "").replace(".", "").split()
            return date(int(year), months[month.casefold()], int(day))
        except ValueError:
            return None

    start = read_after("depart(?:ing|ure)?|leaving")
    end = read_after("return(?:ing)?")
    return (start, end) if start and end and start <= end else None
