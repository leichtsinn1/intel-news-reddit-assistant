#!/usr/bin/env python3

import argparse
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/opt/intel-news-bot")
DATABASE_FILE = BASE_DIR / "data" / "news.db"
LOG_DIR = BASE_DIR / "logs"
SIGNAL_DATA = BASE_DIR / "data" / "signal"
SIGNAL_CLI = "/usr/local/bin/signal-cli"
PIPELINE = BASE_DIR / "app" / "run-pipeline.sh"

TIMERS = [
    "intel-news-collector.timer",
    "intel-news-notifier.timer",
    "intel-news-receiver.timer",
    "intel-news-healthcheck.timer",
]

MAX_RESULTS = 12


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def shorten(value: str, length: int = 140) -> str:
    value = " ".join((value or "").split())

    if len(value) <= length:
        return value

    return value[: length - 1].rstrip() + "…"


def run_command(
    command: list[str],
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=False,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def latest_24h() -> str:
    connection = connect_database()

    total = connection.execute(
        """
        SELECT COUNT(*)
        FROM articles
        WHERE relevant = 1
          AND julianday(discovered_at)
              >= julianday('now', '-24 hours')
          AND status IN (
              'ready_notify',
              'notified',
              'approved_waiting_reddit',
              'approved',
              'published'
          )
        """
    ).fetchone()[0]

    articles = connection.execute(
        """
        SELECT
            id,
            source,
            title,
            original_url,
            target_subreddit,
            content_type,
            status
        FROM articles
        WHERE relevant = 1
          AND julianday(discovered_at)
              >= julianday('now', '-24 hours')
          AND status IN (
              'ready_notify',
              'notified',
              'approved_waiting_reddit',
              'approved',
              'published'
          )
        ORDER BY discovered_at DESC
        LIMIT ?
        """,
        (MAX_RESULTS,),
    ).fetchall()

    connection.close()

    if not articles:
        return (
            "INTEL NEWS DER LETZTEN 24 STUNDEN\n\n"
            "Keine passenden Nachrichten gefunden."
        )

    entries = []

    for article in articles:
        target = article["target_subreddit"] or "offen"
        content_type = article["content_type"] or "news"

        entries.append(
            f"#{article['id']} [{target} | {content_type}]\n"
            f"{article['source']}\n"
            f"{shorten(article['title'])}\n"
            f"{article['original_url']}"
        )

    message = (
        "INTEL NEWS DER LETZTEN 24 STUNDEN\n\n"
        f"Treffer: {total}\n\n"
        + "\n\n".join(entries)
    )

    if total > len(articles):
        message += (
            f"\n\nWeitere Treffer: {total - len(articles)}"
        )

    return message


def queue() -> str:
    connection = connect_database()

    articles = connection.execute(
        """
        SELECT
            id,
            source,
            title,
            original_url,
            target_subreddit,
            content_type,
            status
        FROM articles
        WHERE status IN (
            'ready_notify',
            'notified'
        )
          AND relevant = 1
        ORDER BY discovered_at DESC
        LIMIT 15
        """
    ).fetchall()

    total = connection.execute(
        """
        SELECT COUNT(*)
        FROM articles
        WHERE status IN (
            'ready_notify',
            'notified'
        )
          AND relevant = 1
        """
    ).fetchone()[0]

    connection.close()

    if not articles:
        return (
            "INTEL NEWS WARTESCHLANGE\n\n"
            "Keine unentschiedenen Kandidaten vorhanden."
        )

    entries = []

    for article in articles:
        entries.append(
            f"#{article['id']} "
            f"[{article['target_subreddit']} | "
            f"{article['content_type']}]\n"
            f"{article['source']}\n"
            f"{shorten(article['title'])}\n"
            f"{article['original_url']}\n"
            f"APPROVE {article['id']} oder "
            f"REJECT {article['id']}"
        )

    message = (
        "INTEL NEWS WARTESCHLANGE\n\n"
        f"Unentschieden: {total}\n\n"
        + "\n\n".join(entries)
    )

    if total > len(articles):
        message += (
            f"\n\nWeitere Kandidaten: "
            f"{total - len(articles)}"
        )

    return message


def timer_status() -> tuple[int, list[str]]:
    active_count = 0
    details = []

    for timer in TIMERS:
        result = run_command(
            ["systemctl", "is-active", timer]
        )

        status = result.stdout.strip() or "unbekannt"

        if status == "active":
            active_count += 1
        else:
            details.append(f"{timer}: {status}")

    return active_count, details


def database_status() -> str:
    try:
        connection = connect_database()
        result = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()
        connection.close()

        if result and result[0] == "ok":
            return "OK"

        return result[0] if result else "keine Antwort"

    except sqlite3.Error as error:
        return f"FEHLER: {error}"


def available_memory_mb() -> int:
    values = {}

    with Path("/proc/meminfo").open(
        "r",
        encoding="utf-8",
    ) as file:
        for line in file:
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])

    return values.get("MemAvailable", 0) // 1024


def collector_age() -> str:
    log_file = LOG_DIR / "collector.log"

    if not log_file.exists():
        return "Protokoll fehlt"

    modified = datetime.fromtimestamp(
        log_file.stat().st_mtime,
        timezone.utc,
    )

    age = datetime.now(timezone.utc) - modified
    minutes = int(age.total_seconds() // 60)

    if minutes < 60:
        return f"vor {minutes} Minuten"

    return f"vor {minutes // 60} Stunden"


def recent_errors() -> list[str]:
    errors = []

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

        for line in lines:
            if " ERROR " not in line:
                continue

            if (
                "VideoCardz" in line
                and "403 Client Error" in line
            ):
                continue

            errors.append(
                f"{filename}: {shorten(line, 160)}"
            )

    return errors[-3:]


def signal_status() -> str:
    result = run_command(
        [
            SIGNAL_CLI,
            "--data-dir",
            str(SIGNAL_DATA),
            "listAccounts",
        ],
        timeout=45,
    )

    if result.returncode == 0 and result.stdout.strip():
        return "OK"

    return "FEHLER"


def build_health_report() -> str:
    active_count, timer_problems = timer_status()
    memory_mb = available_memory_mb()
    disk_mb = shutil.disk_usage("/").free // 1024 // 1024
    database = database_status()
    signal = signal_status()
    errors = recent_errors()

    healthy = (
        active_count == len(TIMERS)
        and memory_mb >= 100
        and disk_mb >= 1000
        and database == "OK"
        and signal == "OK"
        and not errors
    )

    heading = (
        "INTEL NEWS BOT HEALTH OK"
        if healthy
        else "INTEL NEWS BOT HEALTH WARNUNG"
    )

    message = (
        f"{heading}\n\n"
        f"Timer: {active_count}/{len(TIMERS)} aktiv\n"
        f"Letzter Collector Lauf: {collector_age()}\n"
        f"RAM verfügbar: {memory_mb} MB\n"
        f"Speicher frei: {disk_mb} MB\n"
        f"SQLite: {database}\n"
        f"Signal Konto: {signal}"
    )

    if timer_problems:
        message += "\n\nTimerprobleme:\n"
        message += "\n".join(
            f"• {problem}"
            for problem in timer_problems
        )

    if errors:
        message += "\n\nLetzte Fehler:\n"
        message += "\n".join(
            f"• {error}"
            for error in errors
        )

    return message


def selftest() -> str:
    started = datetime.now().astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )

    pipeline = run_command(
        [str(PIPELINE)],
        timeout=220,
    )

    pipeline_status = (
        "OK"
        if pipeline.returncode == 0
        else f"FEHLER {pipeline.returncode}"
    )

    health = build_health_report()

    return (
        "INTEL NEWS BOT SELFTEST\n\n"
        f"Zeit: {started}\n"
        f"Feed und Verarbeitungskette: "
        f"{pipeline_status}\n"
        f"Signal Antwortweg: OK\n\n"
        f"{health}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "latest",
            "queue",
            "health",
            "selftest",
        ],
    )

    arguments = parser.parse_args()

    if arguments.command == "latest":
        print(latest_24h())

    elif arguments.command == "queue":
        print(queue())

    elif arguments.command == "health":
        print(build_health_report())

    elif arguments.command == "selftest":
        print(selftest())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
