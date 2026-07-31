#!/usr/bin/env python3

import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/opt/intel-news-bot")
DATABASE_FILE = BASE_DIR / "data" / "news.db"
LOG_FILE = BASE_DIR / "logs" / "notifier.log"
SIGNAL_DATA = BASE_DIR / "data" / "signal"
SIGNAL_CLI = "/usr/local/bin/signal-cli"

MAX_MESSAGES_PER_RUN = 10


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

    if "notified_at" not in columns:
        connection.execute(
            "ALTER TABLE articles ADD COLUMN notified_at TEXT"
        )

    connection.commit()
    return connection


def build_message(article: sqlite3.Row) -> str:
    return (
        "INTEL NEWS CANDIDATE\n\n"
        f"ID: {article['id']}\n"
        f"Quelle: {article['source']}\n"
        f"Titel: {article['title']}\n"
        f"Treffer: {article['matched_terms'] or 'nicht angegeben'}\n\n"
        f"{article['original_url']}\n\n"
        "Noch nicht auf Reddit geprüft oder veröffentlicht."
    )


def send_signal_message(message: str) -> None:
    command = [
        SIGNAL_CLI,
        "--data-dir",
        str(SIGNAL_DATA),
        "send",
        "--note-to-self",
        "-m",
        message,
    ]

    subprocess.run(
        command,
        check=True,
        timeout=90,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def main() -> int:
    configure_logging()
    connection = connect_database()

    articles = connection.execute(
        """
        SELECT
            id,
            source,
            title,
            original_url,
            matched_terms
        FROM articles
        WHERE relevant = 1
          AND status = 'collected'
        ORDER BY id ASC
        LIMIT ?
        """,
        (MAX_MESSAGES_PER_RUN,),
    ).fetchall()

    if not articles:
        logging.info("Keine neuen relevanten Artikel vorhanden.")
        connection.close()
        return 0

    sent = 0

    for article in articles:
        try:
            send_signal_message(build_message(article))

            connection.execute(
                """
                UPDATE articles
                SET status = 'notified',
                    notified_at = ?
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    article["id"],
                ),
            )
            connection.commit()

            sent += 1
            logging.info(
                "Artikel %s über Signal versendet: %s",
                article["id"],
                article["title"],
            )

        except subprocess.TimeoutExpired:
            logging.error(
                "Zeitüberschreitung beim Versenden von Artikel %s",
                article["id"],
            )
            break

        except subprocess.CalledProcessError as error:
            logging.error(
                "Signal Fehler bei Artikel %s: %s",
                article["id"],
                error.stderr.strip(),
            )
            break

    connection.close()
    logging.info("%d Signal Nachrichten versendet.", sent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
