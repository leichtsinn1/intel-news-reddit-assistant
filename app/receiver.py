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

COMMAND_PATTERN = re.compile(
    r"^\s*(APPROVE|REJECT|STATUS)\s+#?(\d+)\s*$",
    re.IGNORECASE,
)


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
    command = [
        SIGNAL_CLI,
        "--data-dir",
        str(SIGNAL_DATA),
        "--output=json",
        "receive",
        "--timeout",
        "10",
        "--max-messages",
        "30",
        "--ignore-attachments",
        "--ignore-stories",
        "--ignore-avatars",
        "--ignore-stickers",
    ]

    result = subprocess.run(
        command,
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
            logging.warning("Ungültige JSON Zeile wurde ignoriert.")

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

    # Wenn beide Werte vorhanden sind, nur Notiz an mich akzeptieren.
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
    subprocess.run(
        [
            SIGNAL_CLI,
            "--data-dir",
            str(SIGNAL_DATA),
            "send",
            "--note-to-self",
            "-m",
            message,
        ],
        check=True,
        timeout=90,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def article_status(
    connection: sqlite3.Connection,
    article_id: int,
) -> str:
    article = connection.execute(
        """
        SELECT id, source, title, original_url, relevant, status
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
        f"Relevant: {'ja' if article['relevant'] else 'nein'}\n"
        f"Titel: {article['title']}\n\n"
        f"{article['original_url']}"
    )


def execute_command(
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
            f"Artikel #{article_id} ist nicht als relevant markiert "
            "und kann nicht freigegeben werden."
        )

    if article["status"] == "blocked_source":
        return (
            f"Artikel #{article_id} stammt aus einer gesperrten Quelle "
            "und kann nicht freigegeben werden."
        )

    now = datetime.now(timezone.utc).isoformat()

    if command_name == "APPROVE":
        new_status = "approved_waiting_reddit"
        response = (
            f"FREIGEGEBEN #{article_id}\n\n"
            f"{article['title']}\n\n"
            "Es wurde noch nichts auf Reddit veröffentlicht. "
            "Der Artikel wartet auf Reddit Prüfung und API Freigabe."
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
        SET status = ?, decision_at = ?
        WHERE id = ?
        """,
        (new_status, now, article_id),
    )
    connection.commit()

    return response


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

    if message.strip().upper() == "HELP":
        response = (
            "Verfügbare Befehle:\n\n"
            "APPROVE 24\n"
            "REJECT 24\n"
            "STATUS 24\n"
            "HELP"
        )
        command_name = "HELP"
        article_id = None

    else:
        match = COMMAND_PATTERN.fullmatch(message)

        if not match:
            return

        command_name = match.group(1).upper()
        article_id = int(match.group(2))
        response = execute_command(
            connection,
            command_name,
            article_id,
        )

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
            response,
        ),
    )
    connection.commit()

    send_reply(response)
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
            process_message(connection, timestamp, message)

    except Exception:
        logging.exception("Signal Empfang fehlgeschlagen.")
        return 1

    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
