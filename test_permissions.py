#!/usr/bin/env python3
"""Probe which WebUntis data is reachable with the configured account.

This script intentionally tests every public endpoint exposed by the installed
python-webuntis library version. It writes a JSON report with successes,
permission errors, remote errors, counts, samples and the raw data returned by
the library where possible.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import json
import os
import platform
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only used outside this venv
    load_dotenv = None

try:
    import requests
    import webuntis
    from webuntis import errors as webuntis_errors
    from webuntis import objects as webuntis_objects
except ImportError as exc:  # pragma: no cover - handled at runtime
    requests = None
    webuntis = None
    webuntis_errors = None
    webuntis_objects = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


REQUIRED_ENV = {
    "username": ("UNTIS_USER", "UNTIS_USERNAME"),
    "password": ("UNTIS_PASSWORD",),
    "server": ("UNTIS_SERVER",),
    "school": ("UNTIS_SCHOOL",),
}

PLACEHOLDER_VALUES = {
    "",
    "user",
    "username",
    "password",
    "passwort",
    "server",
    "school",
    "example",
    "example.com",
}

SENSITIVE_KEYS = {"password", "pass", "pwd"}


@dataclass(frozen=True)
class SelectedItem:
    element_type: str
    name: str
    item: Any
    raw: dict[str, Any]

    @property
    def id(self) -> int | None:
        value = self.raw.get("id")
        if value is None:
            try:
                return int(self.item)
            except Exception:
                return None
        try:
            return int(value)
        except Exception:
            return None


class TimeoutSession(requests.Session if requests else object):
    """requests.Session with a default timeout for webuntis RPC calls."""

    def __init__(self, timeout: float):
        super().__init__()
        self.default_timeout = timeout

    def request(self, method: str, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", self.default_timeout)
        return super().request(method, url, **kwargs)


def parse_date(value: str) -> dt.date:
    """Accept ISO dates and Untis-style YYYYMMDD dates."""
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return dt.datetime.strptime(value, "%Y%m%d").date()
    return dt.date.fromisoformat(value)


def default_week() -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    start = today - dt.timedelta(days=today.weekday())
    return start, start + dt.timedelta(days=6)


def raw_data(value: Any) -> Any:
    if webuntis_objects is not None and isinstance(value, webuntis_objects.Result):
        return value._data
    return value


def serialize(value: Any, depth: int = 0, max_depth: int = 12) -> Any:
    """JSON-safe serializer that avoids touching lazy webuntis properties."""
    if depth > max_depth:
        return repr(value)

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    value = raw_data(value)

    if isinstance(value, dict):
        return {
            str(key): serialize(item, depth + 1, max_depth)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [serialize(item, depth + 1, max_depth) for item in value]

    return repr(value)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_string = str(key)
            if key_string.lower() in SENSITIVE_KEYS:
                clean[key_string] = "<redacted>"
            else:
                clean[key_string] = sanitize(item)
        return clean
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    return serialize(value)


def count_result(value: Any) -> int | None:
    data = raw_data(value)
    try:
        return len(data)
    except Exception:
        return None


def sample_result(value: Any, limit: int) -> Any:
    data = raw_data(value)
    if isinstance(data, list):
        return serialize(data[:limit])
    if isinstance(data, dict):
        sampled: dict[str, Any] = {}
        for index, (key, item) in enumerate(data.items()):
            if index >= limit:
                break
            sampled[str(key)] = serialize(item)
        return sampled
    return serialize(data)


def iter_items(value: Any) -> list[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except Exception:
        return []


def item_raw(item: Any) -> dict[str, Any]:
    data = raw_data(item)
    return data if isinstance(data, dict) else {}


def item_label(raw: dict[str, Any]) -> str:
    for key in ("name", "longName", "foreName", "key", "id"):
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return "<unknown>"


def select_items(element_type: str, collection: Any, args: argparse.Namespace) -> list[SelectedItem]:
    selected = []
    for item in iter_items(collection):
        raw = item_raw(item)
        selected.append(
            SelectedItem(
                element_type=element_type,
                name=item_label(raw),
                item=item,
                raw=raw,
            )
        )

    if args.all_elements:
        return selected
    return selected[: args.element_limit]


def classify_exception(exc: BaseException) -> str:
    message = str(exc).lower()

    if webuntis_errors is not None:
        if isinstance(exc, webuntis_errors.BadCredentialsError):
            return "auth_bad_credentials"
        if isinstance(exc, webuntis_errors.NotLoggedInError):
            return "auth_not_logged_in"
        if isinstance(exc, webuntis_errors.DateNotAllowed):
            return "date_not_allowed"
        if isinstance(exc, webuntis_errors.MethodNotFoundError):
            return "method_not_found"
        if isinstance(exc, webuntis_errors.AuthError):
            return "auth_error"
        if isinstance(exc, webuntis_errors.RemoteError):
            if any(token in message for token in ("no right", "no rights", "rights", "permission", "forbidden", "access")):
                return "no_right"
            return "remote_error"

    if requests is not None:
        if isinstance(exc, requests.Timeout):
            return "network_timeout"
        if isinstance(exc, requests.ConnectionError):
            return "network_connection"
        if isinstance(exc, requests.RequestException):
            return "network_error"

    return "error"


def exception_details(exc: BaseException, verbose_traceback: bool) -> dict[str, Any]:
    details = {
        "type": type(exc).__name__,
        "status": classify_exception(exc),
        "message": str(exc),
    }

    code = getattr(exc, "code", None)
    if code is not None:
        details["code"] = code

    request = getattr(exc, "request", None)
    if request:
        if isinstance(request, dict):
            details["request"] = {
                "method": request.get("method"),
                "params": sanitize(request.get("params")),
            }
        else:
            details["request"] = {
                "method": getattr(request, "method", None),
                "url": getattr(request, "url", None),
            }

    result = getattr(exc, "result", None)
    if result:
        details["result"] = serialize(result)

    if verbose_traceback:
        details["traceback"] = traceback.format_exception(exc)

    return details


class PermissionProbe:
    def __init__(self, session: Any, args: argparse.Namespace, report: dict[str, Any]):
        self.session = session
        self.args = args
        self.report = report
        self.objects: dict[str, Any] = {}

    def run(
        self,
        name: str,
        func: Callable[[], Any],
        *,
        category: str,
        rpc_method: str | None = None,
        notes: str | None = None,
        include_data: bool | None = None,
    ) -> Any:
        include_data = self.args.full_data if include_data is None else include_data
        print(f"[RUN] {name}")
        started = time.monotonic()

        try:
            value = func()
        except Exception as exc:
            duration = round(time.monotonic() - started, 3)
            error = exception_details(exc, self.args.tracebacks)
            entry = {
                "ok": False,
                "category": category,
                "rpc_method": rpc_method,
                "duration_seconds": duration,
                "error": error,
            }
            if notes:
                entry["notes"] = notes
            self.report["tests"][name] = entry
            print(f"[ERR] {name}: {error['status']} ({error['type']}: {error['message']})")
            return None

        duration = round(time.monotonic() - started, 3)
        count = count_result(value)
        entry = {
            "ok": True,
            "category": category,
            "rpc_method": rpc_method,
            "duration_seconds": duration,
            "result_type": type(value).__name__,
            "count": count,
            "sample": sample_result(value, self.args.sample_limit),
        }
        if include_data:
            entry["data"] = serialize(value)
        if notes:
            entry["notes"] = notes

        self.report["tests"][name] = entry
        if count is None:
            print(f"[OK]  {name}")
        else:
            print(f"[OK]  {name}: count={count}")
        return value


def env_value(names: Iterable[str]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return None


def load_config(args: argparse.Namespace) -> tuple[dict[str, Any], list[str], list[str]]:
    if load_dotenv is not None and args.env_file:
        load_dotenv(args.env_file)

    config: dict[str, Any] = {}
    missing = []
    placeholders = []

    for key, env_names in REQUIRED_ENV.items():
        value = env_value(env_names)
        if value is None:
            missing.append("/".join(env_names))
            continue
        config[key] = value
        if value.strip().lower() in PLACEHOLDER_VALUES:
            placeholders.append("/".join(env_names))

    config["useragent"] = os.getenv("UNTIS_USERAGENT", "UntisPermissionScanner/1.0")
    config["login_repeat"] = args.login_repeat

    return config, missing, placeholders


def report_base(args: argparse.Namespace, config: dict[str, Any], date_range: tuple[dt.date, dt.date]) -> dict[str, Any]:
    try:
        webuntis_version = importlib.metadata.version("webuntis")
    except Exception:
        webuntis_version = getattr(webuntis, "__version__", None) if webuntis else None

    return {
        "metadata": {
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "python": sys.version,
            "platform": platform.platform(),
            "webuntis_version": webuntis_version,
            "date_range": {
                "start": date_range[0].isoformat(),
                "end": date_range[1].isoformat(),
            },
            "element_scan": {
                "all_elements": args.all_elements,
                "element_limit": None if args.all_elements else args.element_limit,
            },
            "config": {
                "server": config.get("server"),
                "school": config.get("school"),
                "username_env_present": bool(config.get("username")),
                "password_env_present": bool(config.get("password")),
                "useragent": config.get("useragent"),
            },
        },
        "tests": {},
        "summary": {},
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def add_summary(report: dict[str, Any]) -> None:
    tests = report.get("tests", {})
    total = len(tests)
    ok = sum(1 for entry in tests.values() if entry.get("ok") is True)
    planned = sum(1 for entry in tests.values() if entry.get("ok") is None)
    failed = total - ok - planned
    statuses: dict[str, int] = {}
    categories: dict[str, dict[str, int]] = {}

    for entry in tests.values():
        category = entry.get("category", "unknown")
        categories.setdefault(category, {"ok": 0, "failed": 0, "planned": 0})
        if entry.get("ok") is True:
            categories[category]["ok"] += 1
            status = "ok"
        elif entry.get("ok") is None:
            categories[category]["planned"] += 1
            status = "planned"
        else:
            categories[category]["failed"] += 1
            status = entry.get("error", {}).get("status", "error")
        statuses[status] = statuses.get(status, 0) + 1

    report["summary"] = {
        "total": total,
        "ok": ok,
        "planned": planned,
        "failed": failed,
        "statuses": statuses,
        "categories": categories,
    }


def dry_run_report(args: argparse.Namespace, config: dict[str, Any], date_range: tuple[dt.date, dt.date]) -> dict[str, Any]:
    report = report_base(args, config, date_range)
    static_tests = [
        ("login", "authenticate"),
        ("login_result", None),
        ("departments", "getDepartments"),
        ("holidays", "getHolidays"),
        ("schoolyears", "getSchoolyears"),
        ("schoolyears_current", "getCurrentSchoolyear"),
        ("klassen", "getKlassen"),
        ("klassen_current_schoolyear", "getKlassen"),
        ("teachers", "getTeachers"),
        ("subjects", "getSubjects"),
        ("rooms", "getRooms"),
        ("students", "getStudents"),
        ("exam_types", "getExamTypes"),
        ("statusdata", "getStatusData"),
        ("statusdata_lesson_types", None),
        ("statusdata_period_codes", None),
        ("timegrid_units", "getTimegridUnits"),
        ("last_import_time", "getLatestImportTime"),
        ("my_timetable", "getTimetable"),
        ("substitutions", "getSubstitutions"),
        ("timetable_with_absences", "getTimetableWithAbsences"),
        ("exams_all_types", "getExams"),
        ("exams_by_exam_type", "getExams"),
        ("class_reg_events", "getClassregEvents"),
        ("class_reg_categories", "getClassregCategories"),
        ("class_reg_category_groups", "getClassregCategoryGroups"),
        ("timetable_by_klasse_teacher_subject_room_student", "getTimetable"),
        ("timetable_extended_by_klasse_teacher_subject_room_student", "getTimetable"),
        ("class_reg_event_for_id_by_klasse_teacher_subject_room_student", "getClassregEvents"),
        ("get_student_from_first_student", "getPersonId"),
        ("get_teacher_from_first_teacher", "getPersonId"),
        ("logout", "logout"),
    ]
    for name, rpc_method in static_tests:
        report["tests"][name] = {
            "ok": None,
            "category": "dry_run",
            "rpc_method": rpc_method,
            "planned": True,
        }
    add_summary(report)
    return report


def first_name_parts(raw: dict[str, Any]) -> tuple[str, str] | None:
    surname = raw.get("longName") or raw.get("surname") or raw.get("name")
    fore_name = raw.get("foreName") or raw.get("forename") or raw.get("fore_name")
    if not surname or not fore_name:
        return None
    return str(surname), str(fore_name)


def run_probe(args: argparse.Namespace, config: dict[str, Any], date_range: tuple[dt.date, dt.date]) -> dict[str, Any]:
    report = report_base(args, config, date_range)
    try:
        session = webuntis.Session(
            username=config["username"],
            password=config["password"],
            server=config["server"],
            school=config["school"],
            useragent=config["useragent"],
            login_repeat=config["login_repeat"],
            _http_session=TimeoutSession(args.request_timeout),
        )
    except Exception as exc:
        report["tests"]["session_config"] = {
            "ok": False,
            "category": "config",
            "rpc_method": None,
            "duration_seconds": 0,
            "error": exception_details(exc, args.tracebacks),
        }
        add_summary(report)
        return report

    probe = PermissionProbe(session, args, report)
    start, end = date_range

    login = probe.run(
        "login",
        session.login,
        category="auth",
        rpc_method="authenticate",
        include_data=False,
    )
    if login is None:
        add_summary(report)
        return report

    probe.run(
        "login_result",
        lambda: getattr(session, "login_result", {}),
        category="auth",
        include_data=True,
    )

    try:
        # Master data and bulk endpoints.
        probe.objects["departments"] = probe.run("departments", session.departments, category="master_data", rpc_method="getDepartments")
        probe.objects["holidays"] = probe.run("holidays", session.holidays, category="master_data", rpc_method="getHolidays")
        probe.objects["schoolyears"] = probe.run("schoolyears", session.schoolyears, category="master_data", rpc_method="getSchoolyears")

        current_schoolyear = None
        if probe.objects["schoolyears"] is not None:
            current_schoolyear = probe.run(
                "schoolyears_current",
                lambda: probe.objects["schoolyears"].current,
                category="master_data",
                rpc_method="getCurrentSchoolyear",
            )

        probe.objects["klassen"] = probe.run("klassen", session.klassen, category="master_data", rpc_method="getKlassen")
        if current_schoolyear is not None:
            probe.objects["klassen_current_schoolyear"] = probe.run(
                "klassen_current_schoolyear",
                lambda: session.klassen(schoolyear=current_schoolyear),
                category="master_data",
                rpc_method="getKlassen",
            )

        probe.objects["teachers"] = probe.run("teachers", session.teachers, category="master_data", rpc_method="getTeachers")
        probe.objects["subjects"] = probe.run("subjects", session.subjects, category="master_data", rpc_method="getSubjects")
        probe.objects["rooms"] = probe.run("rooms", session.rooms, category="master_data", rpc_method="getRooms")
        probe.objects["students"] = probe.run("students", session.students, category="personal_data", rpc_method="getStudents")
        probe.objects["exam_types"] = probe.run("exam_types", session.exam_types, category="master_data", rpc_method="getExamTypes")
        probe.objects["statusdata"] = probe.run("statusdata", session.statusdata, category="master_data", rpc_method="getStatusData")

        if probe.objects["statusdata"] is not None:
            probe.run(
                "statusdata_lesson_types",
                lambda: probe.objects["statusdata"].lesson_types,
                category="local_object_helpers",
                notes="Derived from statusdata without an additional RPC call.",
            )
            probe.run(
                "statusdata_period_codes",
                lambda: probe.objects["statusdata"].period_codes,
                category="local_object_helpers",
                notes="Derived from statusdata without an additional RPC call.",
            )

        probe.objects["timegrid_units"] = probe.run("timegrid_units", session.timegrid_units, category="master_data", rpc_method="getTimegridUnits")
        probe.objects["last_import_time"] = probe.run("last_import_time", session.last_import_time, category="metadata", rpc_method="getLatestImportTime")

        # Date based endpoints.
        probe.objects["my_timetable"] = probe.run(
            "my_timetable",
            lambda: session.my_timetable(start=start, end=end),
            category="timetable",
            rpc_method="getTimetable",
        )
        if probe.objects["my_timetable"] is not None:
            probe.run(
                "my_timetable_combine",
                lambda: probe.objects["my_timetable"].combine(),
                category="local_object_helpers",
                notes="Local PeriodList.combine() helper.",
            )
            probe.run(
                "my_timetable_to_table",
                lambda: probe.objects["my_timetable"].to_table(),
                category="local_object_helpers",
                notes="Local PeriodList.to_table() helper.",
            )

        probe.objects["substitutions"] = probe.run(
            "substitutions",
            lambda: session.substitutions(start=start, end=end, department_id=args.department_id),
            category="timetable",
            rpc_method="getSubstitutions",
        )
        if probe.objects["substitutions"] is not None:
            probe.run(
                "substitutions_combine",
                lambda: probe.objects["substitutions"].combine(),
                category="local_object_helpers",
                notes="Local SubstitutionList.combine() helper.",
            )

        probe.objects["timetable_with_absences"] = probe.run(
            "timetable_with_absences",
            lambda: session.timetable_with_absences(start=start, end=end),
            category="personal_data",
            rpc_method="getTimetableWithAbsences",
        )
        probe.objects["exams_all_types"] = probe.run(
            "exams_all_types",
            lambda: session.exams(start=start, end=end, exam_type_id=0),
            category="exams",
            rpc_method="getExams",
        )
        probe.objects["class_reg_events"] = probe.run(
            "class_reg_events",
            lambda: session.class_reg_events(start=start, end=end),
            category="class_register",
            rpc_method="getClassregEvents",
        )
        probe.objects["class_reg_categories"] = probe.run(
            "class_reg_categories",
            session.class_reg_categories,
            category="class_register",
            rpc_method="getClassregCategories",
        )
        probe.objects["class_reg_category_groups"] = probe.run(
            "class_reg_category_groups",
            session.class_reg_category_groups,
            category="class_register",
            rpc_method="getClassregCategoryGroups",
        )

        for exam_type in select_items("exam_type", probe.objects.get("exam_types"), args):
            if exam_type.id is None:
                continue
            probe.run(
                f"exams_exam_type_{exam_type.id}_{exam_type.name}",
                lambda exam_type_id=exam_type.id: session.exams(start=start, end=end, exam_type_id=exam_type_id),
                category="exams",
                rpc_method="getExams",
            )

        # Element-specific timetable and class register probes.
        collections = {
            "klasse": probe.objects.get("klassen"),
            "teacher": probe.objects.get("teachers"),
            "subject": probe.objects.get("subjects"),
            "room": probe.objects.get("rooms"),
            "student": probe.objects.get("students"),
        }

        for element_type, collection in collections.items():
            for selected in select_items(element_type, collection, args):
                if selected.id is None:
                    continue
                suffix = f"{element_type}_{selected.id}_{selected.name}"
                kwargs = {element_type: selected.item}
                probe.run(
                    f"timetable_{suffix}",
                    lambda kwargs=kwargs: session.timetable(start=start, end=end, **kwargs),
                    category="timetable",
                    rpc_method="getTimetable",
                )
                probe.run(
                    f"timetable_extended_{suffix}",
                    lambda kwargs=kwargs: session.timetable_extended(start=start, end=end, **kwargs),
                    category="timetable",
                    rpc_method="getTimetable",
                )
                probe.run(
                    f"class_reg_event_for_id_{suffix}",
                    lambda kwargs=kwargs: session.class_reg_event_for_id(start=start, end=end, **kwargs),
                    category="class_register",
                    rpc_method="getClassregEvents",
                )

        # Search endpoints need a real name. Use the first accessible object.
        teachers = select_items("teacher", probe.objects.get("teachers"), args)
        if teachers:
            parts = first_name_parts(teachers[0].raw)
            if parts:
                surname, fore_name = parts
                probe.run(
                    f"get_teacher_{teachers[0].id or 'unknown'}",
                    lambda surname=surname, fore_name=fore_name: session.get_teacher(surname=surname, fore_name=fore_name),
                    category="search",
                    rpc_method="getPersonId",
                )

        students = select_items("student", probe.objects.get("students"), args)
        if students:
            parts = first_name_parts(students[0].raw)
            if parts:
                surname, fore_name = parts
                probe.run(
                    f"get_student_{students[0].id or 'unknown'}",
                    lambda surname=surname, fore_name=fore_name: session.get_student(surname=surname, fore_name=fore_name),
                    category="search",
                    rpc_method="getPersonId",
                )

    finally:
        probe.run("logout", lambda: session.logout(suppress_errors=False), category="auth", rpc_method="logout", include_data=False)

    add_summary(report)
    return report


def build_parser() -> argparse.ArgumentParser:
    week_start, week_end = default_week()
    parser = argparse.ArgumentParser(
        description="Test all python-webuntis features and write a permission/data report.",
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file. Default: .env")
    parser.add_argument("--output", default="untis_permission_report.json", help="JSON report path.")
    parser.add_argument("--start", type=parse_date, default=week_start, help="Start date as YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--end", type=parse_date, default=week_end, help="End date as YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--request-timeout", type=float, default=20.0, help="HTTP timeout per Untis request in seconds.")
    parser.add_argument("--login-repeat", type=int, default=1, help="How often webuntis may re-login after session expiry.")
    parser.add_argument("--department-id", type=int, default=0, help="Department id for substitutions; 0 means all/default.")
    parser.add_argument("--sample-limit", type=int, default=5, help="Number of example entries stored in each sample.")
    parser.add_argument("--element-limit", type=int, default=3, help="Per type timetable probes unless --all-elements is used.")
    parser.add_argument("--all-elements", action="store_true", help="Probe timetables/class-register for every class, teacher, subject, room and student.")
    parser.add_argument("--no-full-data", dest="full_data", action="store_false", help="Only write samples, counts and errors.")
    parser.set_defaults(full_data=True)
    parser.add_argument("--allow-placeholder-env", action="store_true", help="Try to run even if .env still looks like placeholder data.")
    parser.add_argument("--dry-run", action="store_true", help="Do not login; write the planned probes only.")
    parser.add_argument("--tracebacks", action="store_true", help="Include Python tracebacks for failed probes.")
    parser.add_argument("--strict-exit", action="store_true", help="Exit 1 if any probe fails. By default permission failures are reported but exit 0.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if IMPORT_ERROR is not None:
        print(f"Missing dependency: {IMPORT_ERROR}", file=sys.stderr)
        print("Run this script with the project venv, e.g. .venv/bin/python test_permissions.py", file=sys.stderr)
        return 2

    if args.end < args.start:
        print("--end must not be earlier than --start", file=sys.stderr)
        return 2

    config, missing, placeholders = load_config(args)
    output = Path(args.output)
    date_range = (args.start, args.end)

    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    if placeholders and not args.allow_placeholder_env and not args.dry_run:
        print(
            "The .env still looks like placeholder data for: "
            + ", ".join(placeholders)
            + ". Replace it or run --dry-run.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        report = dry_run_report(args, config, date_range)
        write_report(output, report)
        print(f"Dry-run written to {output}")
        return 0

    report = run_probe(args, config, date_range)
    add_summary(report)
    write_report(output, report)

    summary = report["summary"]
    print("")
    print(f"Report written to {output}")
    print(f"Summary: {summary['ok']} ok, {summary['failed']} failed, {summary['total']} total")
    print(f"Statuses: {json.dumps(summary['statuses'], ensure_ascii=False, sort_keys=True)}")

    if args.strict_exit and summary["failed"]:
        return 1
    if report["tests"].get("login", {}).get("ok") is not True:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

