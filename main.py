#!/usr/bin/env python3
"""Print all WebUntis data this account can currently access.

Credentials are loaded from .env:
  UNTIS_USER or UNTIS_USERNAME
  UNTIS_PASSWORD
  UNTIS_SERVER
  UNTIS_SCHOOL
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

import requests
import webuntis
from dotenv import load_dotenv


ENV_NAMES = {
    "username": ("UNTIS_USER", "UNTIS_USERNAME"),
    "password": ("UNTIS_PASSWORD",),
    "server": ("UNTIS_SERVER",),
    "school": ("UNTIS_SCHOOL",),
}

PERSON_TYPE_TO_TIMETABLE_KIND = {
    1: "klasse",
    2: "teacher",
    3: "subject",
    4: "room",
    5: "student",
}

DAY_NAMES = {
    1: "So",
    2: "Mo",
    3: "Di",
    4: "Mi",
    5: "Do",
    6: "Fr",
    7: "Sa",
}

PY_WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


class TimeoutSession(requests.Session):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def request(self, method: str, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", self.timeout)
        return super().request(method, url, **kwargs)


def parse_date(value: str) -> dt.date:
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return dt.datetime.strptime(value, "%Y%m%d").date()
    return dt.date.fromisoformat(value)


def default_week() -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    return monday, monday + dt.timedelta(days=6)


def raw_data(value: Any) -> Any:
    return getattr(value, "_data", value)


def serialize(value: Any) -> Any:
    value = raw_data(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize(item) for item in value]
    return repr(value)


def env_value(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def load_config(env_file: str) -> dict[str, str]:
    load_dotenv(env_file)
    config = {}
    missing = []

    for key, names in ENV_NAMES.items():
        value = env_value(names)
        if value is None:
            missing.append("/".join(names))
        else:
            config[key] = value

    if missing:
        raise RuntimeError("Missing required env vars: " + ", ".join(missing))

    config["useragent"] = os.getenv("UNTIS_USERAGENT", "UntisDataPrinter/1.0")
    return config


def format_untis_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        date = dt.datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError:
        return str(value)
    return f"{PY_WEEKDAYS[date.weekday()]} {date:%d.%m.%Y}"


def format_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{numeric // 100:02d}:{numeric % 100:02d}"


def format_timestamp_ms(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        timestamp = int(value) / 1000
    except (TypeError, ValueError):
        return str(value)
    return dt.datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M:%S")


def compact(value: Any, max_len: int = 48) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def section(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def print_kv(title: str, rows: list[tuple[str, Any]]) -> None:
    section(title)
    width = max((len(label) for label, _ in rows), default=0)
    for label, value in rows:
        print(f"{label:<{width}} : {value}")


def print_table(
    title: str,
    rows: list[dict[str, Any]],
    columns: list[tuple[str, str]],
    *,
    limit: int | None = None,
    max_col_width: int = 42,
) -> None:
    section(f"{title} ({len(rows)})")
    if not rows:
        print("Keine Daten.")
        return

    shown = rows if not limit else rows[:limit]
    headers = [header for _, header in columns]
    widths = []
    for key, header in columns:
        values = [compact(row.get(key, ""), max_col_width) for row in shown]
        widths.append(min(max([len(header), *[len(value) for value in values]]), max_col_width))

    header_line = " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    for row in shown:
        values = []
        for index, (key, _) in enumerate(columns):
            values.append(compact(row.get(key, ""), widths[index]).ljust(widths[index]))
        print(" | ".join(values))

    if limit and len(rows) > limit:
        print(f"... {len(rows) - limit} weitere Zeilen ausgeblendet. Nutze --limit 0 fuer alles.")


def index_by_id(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    index = {}
    for item in items:
        try:
            index[int(item["id"])] = item
        except (KeyError, TypeError, ValueError):
            continue
    return index


def person_name(item: dict[str, Any]) -> str:
    title = item.get("title", "")
    fore_name = item.get("foreName", "")
    long_name = item.get("longName", "")
    full = " ".join(part for part in (title, fore_name, long_name) if part).strip()
    return full or item.get("name", "") or str(item.get("id", ""))


def object_label(kind: str, object_id: Any, indexes: dict[str, dict[int, dict[str, Any]]]) -> str:
    try:
        numeric_id = int(object_id)
    except (TypeError, ValueError):
        return str(object_id)

    item = indexes.get(kind, {}).get(numeric_id)
    if not item:
        return str(numeric_id)

    if kind in {"teacher", "student"}:
        short = item.get("name") or str(numeric_id)
        full = person_name(item)
        return f"{short} ({full})" if full and full != short else short

    short = item.get("name") or str(numeric_id)
    long_name = item.get("longName", "")
    return f"{short} ({long_name})" if long_name and long_name != short else short


def resolve_refs(period: dict[str, Any], field: str, kind: str, indexes: dict[str, dict[int, dict[str, Any]]]) -> str:
    refs = period.get(field) or []
    labels = []
    for ref in refs:
        if isinstance(ref, dict):
            object_id = ref.get("id")
            original_id = ref.get("orgid")
            label = object_label(kind, object_id, indexes)
            if original_id and original_id != object_id:
                label += f" statt {object_label(kind, original_id, indexes)}"
            labels.append(label)
        else:
            labels.append(object_label(kind, ref, indexes))
    return ", ".join(labels)


def build_indexes(data: dict[str, Any]) -> dict[str, dict[int, dict[str, Any]]]:
    return {
        "klasse": index_by_id(data.get("klassen", [])),
        "teacher": index_by_id(data.get("teachers", [])),
        "subject": index_by_id(data.get("subjects", [])),
        "room": index_by_id(data.get("rooms", [])),
        "student": index_by_id(data.get("students", [])),
    }


def timetable_rows(periods: list[dict[str, Any]], indexes: dict[str, dict[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for period in sorted(periods, key=lambda item: (item.get("date", 0), item.get("startTime", 0), item.get("endTime", 0), item.get("id", 0))):
        text_parts = [
            period.get("lstext", ""),
            period.get("info", ""),
            period.get("substText", ""),
            period.get("bkRemark", ""),
            period.get("bkText", ""),
        ]
        rows.append(
            {
                "date": format_untis_date(period.get("date")),
                "time": f"{format_time(period.get('startTime'))}-{format_time(period.get('endTime'))}",
                "classes": resolve_refs(period, "kl", "klasse", indexes),
                "subject": resolve_refs(period, "su", "subject", indexes),
                "teachers": resolve_refs(period, "te", "teacher", indexes),
                "rooms": resolve_refs(period, "ro", "room", indexes),
                "group": period.get("sg", ""),
                "activity": period.get("activityType", "") or period.get("lstype", ""),
                "code": period.get("code", ""),
                "text": " | ".join(str(part) for part in text_parts if part),
                "lesson": period.get("lsnumber", ""),
                "id": period.get("id", ""),
            }
        )
    return rows


def load_permission_targets(path: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    targets: set[tuple[str, int]] = set()
    tests = report.get("tests", {})
    pattern = re.compile(r"^timetable_(klasse|teacher|subject|room|student)_(\d+)(?:_|$)")
    for name, entry in tests.items():
        if entry.get("ok") is not True:
            continue
        match = pattern.match(name)
        if match:
            targets.add((match.group(1), int(match.group(2))))
    return targets


def default_targets(login_result: dict[str, Any]) -> set[tuple[str, int]]:
    targets = set()
    klasse_id = login_result.get("klasseId")
    if klasse_id:
        targets.add(("klasse", int(klasse_id)))

    person_id = login_result.get("personId")
    person_type = login_result.get("personType")
    kind = PERSON_TYPE_TO_TIMETABLE_KIND.get(int(person_type)) if person_type else None
    if person_id and kind:
        targets.add((kind, int(person_id)))

    return targets


def call_optional(
    data: dict[str, Any],
    errors: dict[str, str],
    name: str,
    func: Callable[[], Any],
) -> Any | None:
    try:
        value = func()
    except Exception as exc:
        errors[name] = f"{type(exc).__name__}: {exc}"
        return None
    data[name] = serialize(value)
    return value


def fetch_live(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, str]]:
    config = load_config(args.env_file)
    data: dict[str, Any] = {
        "metadata": {
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": "live",
            "date_range": {
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
            },
            "server": config["server"],
            "school": config["school"],
            "username_env_present": True,
        }
    }
    errors: dict[str, str] = {}

    session = webuntis.Session(
        username=config["username"],
        password=config["password"],
        server=config["server"],
        school=config["school"],
        useragent=config["useragent"],
        login_repeat=1,
        _http_session=TimeoutSession(args.timeout),
    )

    with session.login() as s:
        data["login_result"] = serialize(getattr(s, "login_result", {}))

        schoolyears = call_optional(data, errors, "schoolyears", s.schoolyears)
        if schoolyears is not None:
            call_optional(data, errors, "schoolyears_current", lambda: schoolyears.current)

        call_optional(data, errors, "departments", s.departments)
        call_optional(data, errors, "holidays", s.holidays)
        call_optional(data, errors, "klassen", s.klassen)

        current_schoolyear = data.get("schoolyears_current")
        if current_schoolyear:
            call_optional(data, errors, "klassen_current_schoolyear", lambda: s.klassen(schoolyear=current_schoolyear["id"]))

        call_optional(data, errors, "teachers", s.teachers)
        call_optional(data, errors, "subjects", s.subjects)
        call_optional(data, errors, "rooms", s.rooms)
        call_optional(data, errors, "students", s.students)
        call_optional(data, errors, "statusdata", s.statusdata)
        call_optional(data, errors, "timegrid_units", s.timegrid_units)
        call_optional(data, errors, "last_import_time", s.last_import_time)
        call_optional(data, errors, "my_timetable", lambda: s.my_timetable(start=args.start, end=args.end))

        targets = load_permission_targets(Path(args.permission_report))
        if not targets:
            targets = default_targets(data["login_result"])

        for kind, object_id in sorted(targets):
            call_optional(
                data,
                errors,
                f"timetable_{kind}_{object_id}",
                lambda kind=kind, object_id=object_id: s.timetable(start=args.start, end=args.end, **{kind: object_id}),
            )
            call_optional(
                data,
                errors,
                f"timetable_extended_{kind}_{object_id}",
                lambda kind=kind, object_id=object_id: s.timetable_extended(start=args.start, end=args.end, **{kind: object_id}),
            )

        if not args.skip_extra_probes:
            call_optional(data, errors, "exam_types", s.exam_types)
            call_optional(data, errors, "substitutions", lambda: s.substitutions(start=args.start, end=args.end))
            call_optional(data, errors, "timetable_with_absences", lambda: s.timetable_with_absences(start=args.start, end=args.end))
            call_optional(data, errors, "exams", lambda: s.exams(start=args.start, end=args.end))
            call_optional(data, errors, "class_reg_events", lambda: s.class_reg_events(start=args.start, end=args.end))
            call_optional(data, errors, "class_reg_categories", s.class_reg_categories)
            call_optional(data, errors, "class_reg_category_groups", s.class_reg_category_groups)

    return data, errors


def load_from_report(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    metadata = report.get("metadata", {})
    config = metadata.get("config", {})
    data: dict[str, Any] = {
        "metadata": {
            **metadata,
            "source": str(path),
            "server": config.get("server", metadata.get("server", "")),
            "school": config.get("school", metadata.get("school", "")),
        }
    }
    errors: dict[str, str] = {}

    for name, entry in report.get("tests", {}).items():
        if entry.get("ok") is True and "data" in entry:
            data[name] = entry["data"]
        elif entry.get("ok") is False:
            error = entry.get("error", {})
            errors[name] = f"{error.get('status', 'error')}: {error.get('message', '')}"

    return data, errors


def render(data: dict[str, Any], errors: dict[str, str], args: argparse.Namespace) -> None:
    limit = None if args.limit == 0 else args.limit
    indexes = build_indexes(data)
    login = data.get("login_result", {})
    current_schoolyear = data.get("schoolyears_current", {})
    metadata = data.get("metadata", {})
    date_range = metadata.get("date_range", {})
    if isinstance(date_range, dict):
        range_text = f"{date_range.get('start', '')} bis {date_range.get('end', '')}"
    else:
        range_text = str(date_range)
    if not range_text.strip():
        range_text = f"{args.start.isoformat()} bis {args.end.isoformat()}"

    print_kv(
        "Uebersicht",
        [
            ("Quelle", metadata.get("source", "live")),
            ("Server", metadata.get("server", "")),
            ("Schule", metadata.get("school", "")),
            ("Zeitraum", range_text),
            ("Person-Typ", login.get("personType", "")),
            ("Person-ID", login.get("personId", "")),
            ("Klassen-ID", login.get("klasseId", "")),
            ("Aktuelles Schuljahr", current_schoolyear.get("name", "")),
            ("Letzter Import", format_timestamp_ms(data.get("last_import_time"))),
        ],
    )

    print_table(
        "Schuljahre",
        [
            {
                **item,
                "start": format_untis_date(item.get("startDate")),
                "end": format_untis_date(item.get("endDate")),
                "current": "ja" if item.get("id") == current_schoolyear.get("id") else "",
            }
            for item in data.get("schoolyears", [])
        ],
        [("id", "ID"), ("name", "Name"), ("start", "Start"), ("end", "Ende"), ("current", "Aktuell")],
        limit=limit,
    )

    print_table(
        "Klassen",
        data.get("klassen_current_schoolyear") or data.get("klassen", []),
        [("id", "ID"), ("name", "Name"), ("longName", "Langname"), ("teacher1", "Tutor 1"), ("teacher2", "Tutor 2"), ("active", "Aktiv")],
        limit=limit,
    )

    print_table(
        "Lehrer",
        [
            {
                **item,
                "full": person_name(item),
            }
            for item in data.get("teachers", [])
        ],
        [("id", "ID"), ("name", "Kuerzel"), ("full", "Name"), ("foreName", "Vorname"), ("longName", "Nachname"), ("active", "Aktiv")],
        limit=limit,
    )

    print_table(
        "Faecher",
        data.get("subjects", []),
        [("id", "ID"), ("name", "Kuerzel"), ("longName", "Langname"), ("alternateName", "Alternativ"), ("active", "Aktiv"), ("foreColor", "Text"), ("backColor", "Farbe")],
        limit=limit,
    )

    print_table(
        "Raeume",
        data.get("rooms", []),
        [("id", "ID"), ("name", "Name"), ("longName", "Langname"), ("building", "Gebaeude"), ("active", "Aktiv")],
        limit=limit,
    )

    print_table(
        "Schueler",
        [
            {
                **item,
                "full": person_name(item),
            }
            for item in data.get("students", [])
        ],
        [("id", "ID"), ("key", "Key"), ("name", "Kuerzel"), ("full", "Name"), ("gender", "Geschlecht")],
        limit=limit,
    )

    print_table(
        "Ferien und Feiertage",
        [
            {
                **item,
                "start": format_untis_date(item.get("startDate")),
                "end": format_untis_date(item.get("endDate")),
            }
            for item in data.get("holidays", [])
        ],
        [("id", "ID"), ("name", "Name"), ("longName", "Langname"), ("start", "Start"), ("end", "Ende")],
        limit=limit,
    )

    print_table(
        "Abteilungen",
        data.get("departments", []),
        [("id", "ID"), ("name", "Name"), ("longName", "Langname")],
        limit=limit,
    )

    timegrid_rows = []
    for day in data.get("timegrid_units", []):
        day_name = DAY_NAMES.get(day.get("day"), str(day.get("day", "")))
        for unit in day.get("timeUnits", []):
            timegrid_rows.append(
                {
                    "day": day_name,
                    "lesson": unit.get("name", ""),
                    "start": format_time(unit.get("startTime")),
                    "end": format_time(unit.get("endTime")),
                }
            )
    print_table(
        "Zeitraster",
        timegrid_rows,
        [("day", "Tag"), ("lesson", "Std"), ("start", "Start"), ("end", "Ende")],
        limit=limit,
    )

    status_rows = []
    statusdata = data.get("statusdata", {})
    for group_name, entries in (("Unterrichtstyp", statusdata.get("lstypes", [])), ("Periodencode", statusdata.get("codes", []))):
        for entry in entries:
            for name, colors in entry.items():
                status_rows.append(
                    {
                        "group": group_name,
                        "name": name,
                        "fore": colors.get("foreColor", ""),
                        "back": colors.get("backColor", ""),
                    }
                )
    print_table(
        "Statusdaten",
        status_rows,
        [("group", "Gruppe"), ("name", "Name"), ("fore", "Textfarbe"), ("back", "Hintergrund")],
        limit=limit,
    )

    render_timetable("Mein Stundenplan", data.get("my_timetable", []), indexes, limit)

    rendered = {"my_timetable"}
    timetable_pattern = re.compile(r"^timetable_(extended_)?(klasse|teacher|subject|room|student)_(\d+)")
    timetable_keys = sorted(
        key for key, value in data.items()
        if isinstance(value, list) and timetable_pattern.match(key)
    )
    preferred_keys = []
    for key in timetable_keys:
        if key.startswith("timetable_extended_"):
            preferred_keys.append(key)
        else:
            extended = key.replace("timetable_", "timetable_extended_", 1)
            if extended not in data:
                preferred_keys.append(key)

    for key in preferred_keys:
        if key in rendered:
            continue
        match = timetable_pattern.match(key)
        if not match:
            continue
        kind = match.group(2)
        object_id = int(match.group(3))
        label = object_label(kind, object_id, indexes)
        render_timetable(f"Stundenplan {kind} {label}", data.get(key, []), indexes, limit)
        rendered.add(key)

    if "substitutions" in data:
        render_timetable("Vertretungen", data.get("substitutions", []), indexes, limit)
    if "exams" in data:
        render_timetable("Pruefungen", data.get("exams", []), indexes, limit)
    if "timetable_with_absences" in data:
        print_table("Abwesenheiten", data.get("timetable_with_absences", []), [("id", "ID"), ("date", "Datum"), ("studentId", "Schueler"), ("checked", "Geprueft"), ("excuseStatus", "Status")], limit=limit)
    if "class_reg_events" in data:
        print_table("Klassenbuch-Ereignisse", data.get("class_reg_events", []), [("id", "ID"), ("date", "Datum"), ("surname", "Nachname"), ("forname", "Vorname"), ("reason", "Grund"), ("text", "Text")], limit=limit)
    if "exam_types" in data:
        print_table("Pruefungsarten", data.get("exam_types", []), [("id", "ID"), ("name", "Name"), ("longName", "Langname"), ("showInTimetable", "Im Plan")], limit=limit)

    unavailable = {
        key: value
        for key, value in errors.items()
        if key in {
            "exam_types",
            "exams_all_types",
            "substitutions",
            "timetable_with_absences",
            "exams",
            "class_reg_events",
            "class_reg_categories",
            "class_reg_category_groups",
        }
    }
    if unavailable:
        print_table(
            "Nicht verfuegbar mit diesem Account",
            [{"name": key, "reason": value} for key, value in sorted(unavailable.items())],
            [("name", "Bereich"), ("reason", "Grund")],
            limit=None,
            max_col_width=90,
        )


def render_timetable(
    title: str,
    periods: list[dict[str, Any]],
    indexes: dict[str, dict[int, dict[str, Any]]],
    limit: int | None,
) -> None:
    print_table(
        title,
        timetable_rows(periods, indexes),
        [
            ("date", "Datum"),
            ("time", "Zeit"),
            ("subject", "Fach"),
            ("teachers", "Lehrer"),
            ("rooms", "Raum"),
            ("classes", "Klasse"),
            ("group", "Gruppe"),
            ("activity", "Art"),
            ("code", "Code"),
            ("text", "Text"),
            ("lesson", "Lesson"),
            ("id", "ID"),
        ],
        limit=limit,
        max_col_width=36,
    )


def build_parser() -> argparse.ArgumentParser:
    start, end = default_week()
    parser = argparse.ArgumentParser(description="Print all accessible WebUntis data.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--start", type=parse_date, default=start)
    parser.add_argument("--end", type=parse_date, default=end)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--permission-report", default="report.json", help="Uses successful timetable targets from this report if present.")
    parser.add_argument("--from-report", help="Render an existing test_permissions report instead of contacting Untis.")
    parser.add_argument("--output-json", default="untis_all_accessible_data.json")
    parser.add_argument("--no-json", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Rows per table. 0 means no limit.")
    parser.add_argument("--skip-extra-probes", action="store_true", help="Do not try endpoints that were denied in the permission report.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.end < args.start:
        print("--end must not be earlier than --start", file=sys.stderr)
        return 2

    try:
        if args.from_report:
            data, errors = load_from_report(Path(args.from_report))
        else:
            data, errors = fetch_live(args)
    except Exception as exc:
        print(f"Fehler: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    render(data, errors, args)

    if not args.no_json:
        output_path = Path(args.output_json)
        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print()
        print(f"Rohdaten gespeichert in: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

