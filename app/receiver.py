#!/usr/bin/env python3

import json
import logging
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/opt/intel-news-bot")
DATABASE_FILE = BASE_DIR / "data" / "news.db"
LOG_FILE = BASE_DIR / "logs" / "receiver.log"
SIGNAL_DATA = BASE_DIR / "data" / "signal"

SIGNAL_CLI = "/usr/local/bin/signal-cli"
PIPELINE_SCRIPT = BASE_DIR / "app" / "run-pipeline.sh"

MAX_CHECK_RESULTS = 15
MAX_SIGNAL_LENGTH = 3500

ARTICLE_COMMAND_PATTERN = re.compile(
    r"^\s*(APPROVE|REJECT|STATUS)\s+#?(\d+)\s*$",
    re.IGNORECASE,
)

CHECK_COMMANDS = {
    "CHECK",
    "NEWS",
    "SCAN",
}

QUERY_COMMANDS = {
    "LATEST": "latest",
    "LATEST 24H": "latest",
    "QUEUE": "queue",
    "HEALTH": "health",
    "SELFTEST": "selftest",
}


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row

    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(articles)"
        ).fetchall()
    }

    if "decision_at" not in columns:
        connection.execute(
            "ALTER TABLE articles ADD COLUMN decision_at TEXT"
        )

    if "notified_at" not in columns:
        connection.execute(
            "ALTER TABLE articles ADD COLUMN notified_at TEXT"
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_commands (
            message_timestamp INTEGER PRIMARY KEY,
            command TEXT NOT NULL,
            article_id INTEGER,
            processed_at TEXT NOT NULL,
            result TEXT NOT NULL
        )
        """
    )

    connection.commit()
    return connection


def receive_messages() -> list[dict]:
    result = subprocess.run(
        [
            SIGNAL_CLI,
            "--data-dir",
            str(SIGNAL_DATA),
            "--output=json",
            "receive",
            "--timeout",
            "2",
            "--max-messages",
            "30",
            "--ignore-attachments",
            "--ignore-stories",
            "--ignore-avatars",
            "--ignore-stickers",
        ],
        check=False,
        timeout=45,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or f"signal-cli endete mit Code {result.returncode}"
        )

    messages = []

    for line in result.stdout.splitlines():
        line = line.strip()

        if not line.startswith("{"):
            continue

        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            logging.warning(
                "Ungültige Signal JSON Zeile ignoriert."
            )

    return messages


def extract_note_to_self(
    payload: dict,
) -> tuple[int, str] | None:
    wrapper = payload.get("result", payload)
    envelope = wrapper.get("envelope") or payload.get("envelope")

    if not isinstance(envelope, dict):
        return None

    sync_message = envelope.get("syncMessage")

    if not isinstance(sync_message, dict):
        return None

    sent_message = sync_message.get("sentMessage")

    if not isinstance(sent_message, dict):
        return None

    message = sent_message.get("message")

    if not isinstance(message, str) or not message.strip():
        return None

    account = wrapper.get("account") or payload.get("account")

    destination = (
        sent_message.get("destinationNumber")
        or sent_message.get("destination")
    )

    if account and destination and account != destination:
        return None

    timestamp = (
        sent_message.get("timestamp")
        or envelope.get("timestamp")
    )

    if not isinstance(timestamp, int):
        return None

    return timestamp, message.strip()


def send_reply(message: str) -> None:
    result = subprocess.run(
        [
            SIGNAL_CLI,
            "--data-dir",
            str(SIGNAL_DATA),
            "send",
            "--note-to-self",
            "-m",
            message,
        ],
        check=False,
        timeout=90,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or "Signal Antwort konnte nicht gesendet werden."
        )


def split_messages(
    header: str,
    entries: list[str],
) -> list[str]:
    messages = []
    current = header

    for entry in entries:
        addition = f"\n\n{entry}"

        if len(current) + len(addition) > MAX_SIGNAL_LENGTH:
            messages.append(current)
            current = (
                "AKTUELLE INTEL NACHRICHTEN "
                "FORTSETZUNG"
                + addition
            )
        else:
            current += addition

    messages.append(current)
    return messages


def article_status(
    connection: sqlite3.Connection,
    article_id: int,
) -> str:
    article = connection.execute(
        """
        SELECT
            id,
            source,
            title,
            original_url,
            relevant,
            status,
            target_subreddit,
            content_type
        FROM articles
        WHERE id = ?
        """,
        (article_id,),
    ).fetchone()

    if article is None:
        return f"Artikel #{article_id} wurde nicht gefunden."

    return (
        f"STATUS #{article['id']}\n\n"
        f"Quelle: {article['source']}\n"
        f"Status: {article['status']}\n"
        f"Ziel: {article['target_subreddit'] or 'offen'}\n"
        f"Typ: {article['content_type'] or 'offen'}\n"
        f"Relevant: {'ja' if article['relevant'] else 'nein'}\n\n"
        f"{article['title']}\n\n"
        f"{article['original_url']}"
    )


def execute_article_command(
    connection: sqlite3.Connection,
    command_name: str,
    article_id: int,
) -> str:
    article = connection.execute(
        """
        SELECT id, title, relevant, status
        FROM articles
        WHERE id = ?
        """,
        (article_id,),
    ).fetchone()

    if article is None:
        return f"Artikel #{article_id} wurde nicht gefunden."

    if command_name == "STATUS":
        return article_status(connection, article_id)

    if not article["relevant"]:
        return (
            f"Artikel #{article_id} ist nicht relevant markiert."
        )

    if article["status"] == "blocked_source":
        return (
            f"Artikel #{article_id} stammt aus einer "
            "gesperrten Quelle."
        )

    now = datetime.now(timezone.utc).isoformat()

    if command_name == "APPROVE":
        new_status = "approved_waiting_reddit"

        response = (
            f"FREIGEGEBEN #{article_id}\n\n"
            f"{article['title']}\n\n"
            "Noch nicht auf Reddit veröffentlicht. "
            "Der Artikel wartet auf die Reddit Prüfung."
        )

    elif command_name == "REJECT":
        new_status = "rejected"

        response = (
            f"ABGELEHNT #{article_id}\n\n"
            f"{article['title']}"
        )

    else:
        return "Unbekannter Befehl."

    connection.execute(
        """
        UPDATE articles
        SET status = ?,
            decision_at = ?
        WHERE id = ?
        """,
        (
            new_status,
            now,
            article_id,
        ),
    )

    connection.commit()
    return response


def run_manual_check(
    connection: sqlite3.Connection,
) -> tuple[list[str], list[int]]:
    started_at = datetime.now(timezone.utc).isoformat()

    result = subprocess.run(
        [str(PIPELINE_SCRIPT)],
        check=False,
        timeout=210,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        logging.error(
            "Manuelle Suche fehlgeschlagen:\n%s\n%s",
            result.stdout,
            result.stderr,
        )

        return (
            [
                "INTEL NEWS SUCHE FEHLGESCHLAGEN\n\n"
                "Die außerplanmäßige Suche konnte nicht "
                "vollständig ausgeführt werden. "
                "Bitte den Serverstatus prüfen."
            ],
            [],
        )

    statistics = connection.execute(
        """
        SELECT
            COUNT(*) AS total_new,
            SUM(CASE WHEN relevant = 1 THEN 1 ELSE 0 END)
                AS relevant_new,
            SUM(CASE WHEN status = 'duplicate_local'
                THEN 1 ELSE 0 END)
                AS duplicate_new,
            SUM(CASE WHEN status = 'rejected_local'
                THEN 1 ELSE 0 END)
                AS rejected_new
        FROM articles
        WHERE discovered_at >= ?
        """,
        (started_at,),
    ).fetchone()

    waiting_total = connection.execute(
        """
        SELECT COUNT(*)
        FROM articles
        WHERE status = 'ready_notify'
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
            classification_score
        FROM articles
        WHERE status = 'ready_notify'
        ORDER BY id ASC
        LIMIT ?
        """,
        (MAX_CHECK_RESULTS,),
    ).fetchall()

    total_new = statistics["total_new"] or 0
    relevant_new = statistics["relevant_new"] or 0
    duplicate_new = statistics["duplicate_new"] or 0
    rejected_new = statistics["rejected_new"] or 0

    if not articles:
        message = (
            "AKTUELLE INTEL SUCHE ABGESCHLOSSEN\n\n"
            "Keine neuen passenden Nachrichten gefunden.\n\n"
            f"Neue Feed Einträge: {total_new}\n"
            f"Intel relevant: {relevant_new}\n"
            f"Lokale Duplikate: {duplicate_new}\n"
            f"Lokal ungeeignet: {rejected_new}"
        )

        return [message], []

    header = (
        "AKTUELLE INTEL SUCHE ABGESCHLOSSEN\n\n"
        f"Passende Kandidaten: {waiting_total}\n"
        f"Neue Feed Einträge: {total_new}\n"
        f"Intel relevant: {relevant_new}\n"
        f"Lokale Duplikate: {duplicate_new}"
    )

    entries = []
    article_ids = []

    for article in articles:
        article_ids.append(article["id"])

        target = article["target_subreddit"] or "offen"
        content_type = article["content_type"] or "news"

        entries.append(
            f"#{article['id']} "
            f"[{target} | {content_type}]\n"
            f"{article['source']}\n"
            f"{article['title']}\n"
            f"{article['original_url']}"
        )

    if waiting_total > len(articles):
        entries.append(
            f"Weitere {waiting_total - len(articles)} "
            "Kandidaten warten noch. "
            "Sende CHECK erneut."
        )

    return split_messages(header, entries), article_ids


def mark_check_articles_notified(
    connection: sqlite3.Connection,
    article_ids: list[int],
) -> None:
    if not article_ids:
        return

    now = datetime.now(timezone.utc).isoformat()

    connection.executemany(
        """
        UPDATE articles
        SET status = 'notified',
            notified_at = ?
        WHERE id = ?
          AND status = 'ready_notify'
        """,
        [
            (now, article_id)
            for article_id in article_ids
        ],
    )

    connection.commit()


def run_query_command(command: str) -> str:
    result = subprocess.run(
        [
            str(
                BASE_DIR
                / "venv"
                / "bin"
                / "python"
            ),
            str(
                BASE_DIR
                / "app"
                / "query_commands.py"
            ),
            command,
        ],
        check=False,
        timeout=230,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        logging.error(
            "Signal Abfrage %s fehlgeschlagen: %s",
            command,
            result.stderr.strip(),
        )

        return (
            "Die gewünschte Abfrage ist fehlgeschlagen. "
            "Bitte HEALTH ausführen oder das "
            "Serverprotokoll prüfen."
        )

    return result.stdout.strip()

def process_message(
    connection: sqlite3.Connection,
    timestamp: int,
    message: str,
) -> None:
    already_processed = connection.execute(
        """
        SELECT 1
        FROM signal_commands
        WHERE message_timestamp = ?
        """,
        (timestamp,),
    ).fetchone()

    if already_processed:
        return

    normalized = message.strip().upper()
    article_id = None
    articles_to_mark = []

    if normalized == "HELP":
        command_name = "HELP"

        responses = [
            "VERFÜGBARE BEFEHLE\n\n"
            "CHECK\n"
            "LATEST 24H\n"
            "QUEUE\n"
            "HEALTH\n"
            "SELFTEST\n"
            "APPROVE 24\n"
            "REJECT 24\n"
            "STATUS 24\n"
            "HELP"
        ]

    elif normalized in QUERY_COMMANDS:
        command_name = normalized

        responses = [
            run_query_command(
                QUERY_COMMANDS[normalized]
            )
        ]

    elif normalized in CHECK_COMMANDS:
        command_name = "CHECK"

        responses, articles_to_mark = run_manual_check(
            connection
        )

    else:
        match = ARTICLE_COMMAND_PATTERN.fullmatch(message)

        if not match:
            return

        command_name = match.group(1).upper()
        article_id = int(match.group(2))

        responses = [
            execute_article_command(
                connection,
                command_name,
                article_id,
            )
        ]

    for response in responses:
        send_reply(response)

    if command_name == "CHECK":
        mark_check_articles_notified(
            connection,
            articles_to_mark,
        )

    stored_result = "\n\n".join(responses)

    connection.execute(
        """
        INSERT INTO signal_commands (
            message_timestamp,
            command,
            article_id,
            processed_at,
            result
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            command_name,
            article_id,
            datetime.now(timezone.utc).isoformat(),
            stored_result[:12000],
        ),
    )

    connection.commit()

    logging.info(
        "Signal Befehl verarbeitet: %s %s",
        command_name,
        article_id if article_id is not None else "",
    )


def main() -> int:
    configure_logging()
    connection = connect_database()

    try:
        messages = receive_messages()

        for payload in messages:
            extracted = extract_note_to_self(payload)

            if extracted is None:
                continue

            timestamp, message = extracted

            process_message(
                connection,
                timestamp,
                message,
            )

    except Exception:
        logging.exception(
            "Signal Empfang oder Verarbeitung fehlgeschlagen."
        )
        return 1

    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
