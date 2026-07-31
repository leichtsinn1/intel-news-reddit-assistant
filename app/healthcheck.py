#!/usr/bin/env python3

import argparse
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path("/opt/intel-news-bot")
DATABASE_FILE = BASE_DIR / "data" / "news.db"
LOG_DIR = BASE_DIR / "logs"
SIGNAL_DATA = BASE_DIR / "data" / "signal"
SIGNAL_CLI = "/usr/local/bin/signal-cli"

TIMERS = [
    "intel-news-collector.timer",
    "intel-news-notifier.timer",
    "intel-news-receiver.timer",
]

MIN_AVAILABLE_MEMORY_MB = 100
MIN_FREE_DISK_MB = 1000
MAX_COLLECTOR_AGE_HOURS = 5


def run_command(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )


def check_timers() -> list[str]:
    problems = []

    for timer in TIMERS:
        result = run_command(
            ["systemctl", "is-active", timer]
        )

        if result.stdout.strip() != "active":
            problems.append(
                f"Timer nicht aktiv: {timer}"
            )

    return problems


def check_memory() -> list[str]:
    problems = {}

    values = {}

    with Path("/proc/meminfo").open(
        "r",
        encoding="utf-8",
    ) as file:
        for line in file:
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])

    available_mb = values.get("MemAvailable", 0) // 1024

    if available_mb < MIN_AVAILABLE_MEMORY_MB:
        problems["memory"] = (
            f"Nur noch {available_mb} MB RAM verfügbar"
        )

    return list(problems.values())


def check_disk() -> list[str]:
    free_mb = shutil.disk_usage("/").free // 1024 // 1024

    if free_mb < MIN_FREE_DISK_MB:
        return [
            f"Nur noch {free_mb} MB Speicherplatz frei"
        ]

    return []


def check_collector_freshness() -> list[str]:
    log_file = LOG_DIR / "collector.log"

    if not log_file.exists():
        return ["Collector Protokoll fehlt"]

    modified = datetime.fromtimestamp(
        log_file.stat().st_mtime,
        tz=timezone.utc,
    )

    maximum_age = timedelta(
        hours=MAX_COLLECTOR_AGE_HOURS
    )

    if datetime.now(timezone.utc) - modified > maximum_age:
        return [
            "Collector wurde seit mehr als "
            f"{MAX_COLLECTOR_AGE_HOURS} Stunden "
            "nicht ausgeführt"
        ]

    return []


def check_recent_errors() -> list[str]:
    problems = []

    for filename in [
        "collector.log",
        "classifier.log",
        "deduplicator.log",
        "notifier.log",
        "receiver.log",
    ]:
        path = LOG_DIR / filename

        if not path.exists():
            continue

        lines = path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()[-200:]

        error_lines = [
            line
            for line in lines
            if " ERROR " in line
            and not (
                "VideoCardz" in line
                and "403 Client Error" in line
            )
        ]

        if error_lines:
            problems.append(
                f"{filename}: {error_lines[-1][-180:]}"
            )

    return problems


def check_database() -> list[str]:
    if not DATABASE_FILE.exists():
        return ["SQLite Datenbank fehlt"]

    try:
        connection = sqlite3.connect(DATABASE_FILE)

        result = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()

        connection.close()

        if not result or result[0] != "ok":
            return [
                "SQLite Prüfung fehlgeschlagen: "
                f"{result[0] if result else 'keine Antwort'}"
            ]

    except sqlite3.Error as error:
        return [f"SQLite Fehler: {error}"]

    return []


def send_signal(message: str) -> None:
    result = run_command(
        [
            SIGNAL_CLI,
            "--data-dir",
            str(SIGNAL_DATA),
            "send",
            "--note-to-self",
            "-m",
            message,
        ]
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or "Signal Nachricht konnte nicht gesendet werden"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test",
        action="store_true",
        help="Sendet immer eine Testmeldung",
    )
    arguments = parser.parse_args()

    problems = []

    problems.extend(check_timers())
    problems.extend(check_memory())
    problems.extend(check_disk())
    problems.extend(check_collector_freshness())
    problems.extend(check_recent_errors())
    problems.extend(check_database())

    timestamp = datetime.now().astimezone().strftime(
        "%Y-%m-%d %H:%M %Z"
    )

    if arguments.test:
        message = (
            "INTEL NEWS BOT SYSTEMTEST\n\n"
            f"Zeit: {timestamp}\n"
            f"Erkannte Probleme: {len(problems)}"
        )

        if problems:
            message += "\n\n" + "\n".join(
                f"• {problem}"
                for problem in problems
            )
        else:
            message += "\n\nAlle Prüfungen erfolgreich."

        send_signal(message)
        return 0

    if not problems:
        print("Alle Systemprüfungen erfolgreich.")
        return 0

    message = (
        "INTEL NEWS BOT WARNUNG\n\n"
        f"Zeit: {timestamp}\n\n"
        + "\n".join(
            f"• {problem}"
            for problem in problems
        )
    )

    send_signal(message)
    print(message)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
