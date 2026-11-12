"""Turns the parser's filter strings (extension:pdf, size:size_gt:5MB, ...) into the Filters message."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import search_pb2

UNITS = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
RELATIVE_DAYS = {"last_week": 7, "last_month": 30, "last_year": 365}
CALENDAR_DAYS_AGO = {"today": 0, "yesterday": 1}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
# type -> (extensions, MIME prefixes)
TYPES = {
    "image": ([], ["image/"]),
    "video": ([], ["video/"]),
    "audio": ([], ["audio/"]),
    "presentation": (["ppt", "pptx"], ["application/vnd.ms-powerpoint"]),
    "spreadsheet": (["xls", "xlsx", "csv"], ["application/vnd.ms-excel"]),
}


def resolve(strings: list[str], now: datetime, tz: str) -> search_pb2.Filters:
    f = search_pb2.Filters()
    for s in strings:
        kind, _, value = s.partition(":")
        if kind == "extension":
            f.extensions.append(value)
        elif kind == "type" and value in TYPES:
            extensions, prefixes = TYPES[value]
            f.extensions.extend(extensions)
            f.mime_prefixes.extend(prefixes)
        elif kind == "size":
            op, _, amount = value.partition(":")
            size = int(float(amount[:-2]) * UNITS[amount[-2:]])
            if op == "size_gt":
                f.min_size_bytes = size
            else:
                f.max_size_bytes = size
        elif kind == "date" and value in RELATIVE_DAYS:
            f.uploaded_from = (now - timedelta(days=RELATIVE_DAYS[value])).isoformat()
            f.uploaded_to = now.isoformat()
        elif kind == "date" and value in CALENDAR_DAYS_AGO:
            midnight = now.astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
            start = midnight - timedelta(days=CALENDAR_DAYS_AGO[value])
            f.uploaded_from = start.isoformat()
            f.uploaded_to = (start + timedelta(days=1)).isoformat()
        elif kind == "exact_date":
            month, day, year = value.split("_")
            try:
                start = datetime(int(year), MONTHS.index(month[:3]) + 1, int(day), tzinfo=ZoneInfo(tz))
            except ValueError:  # e.g. "feb 31"
                continue
            f.uploaded_from = start.isoformat()
            f.uploaded_to = (start + timedelta(days=1)).isoformat()
    return f
