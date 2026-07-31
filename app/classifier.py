#!/usr/bin/env python3

import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/opt/intel-news-bot")
DATABASE_FILE = BASE_DIR / "data" / "news.db"
LOG_FILE = BASE_DIR / "logs" / "classifier.log"

ARC_TERMS = {
    "intel arc": 5,
    "arc graphics": 4,
    "arc gpu": 4,
    "battlemage": 4,
    "celestial": 4,
    "alchemist": 3,
    "b580": 3,
    "b570": 3,
    "a770": 3,
    "a750": 3,
    "a580": 3,
    "a380": 3,
    "xe2": 3,
    "xe3": 3,
    "graphics driver": 3,
    "gpu driver": 3,
    "integrated graphics": 3,
    "discrete graphics": 3,
    "graphics card": 2,
    "igpu": 2,
    "directx": 1,
    "vulkan": 1,
}

STOCK_TERMS = {
    "intc": 5,
    "earnings": 5,
    "revenue": 4,
    "guidance": 4,
    "gross margin": 4,
    "operating margin": 4,
    "raise capital": 5,
    "capital raise": 5,
    "shares": 3,
    "stock": 3,
    "dividend": 4,
    "debt": 3,
    "layoff": 3,
    "restructuring": 4,
    "acquisition": 3,
    "investment": 2,
    "subsidy": 3,
    "government funding": 3,
    "intel foundry": 5,
    "foundry": 3,
    "18a": 4,
    "14a": 4,
    "process node": 3,
    "semiconductor manufacturing": 3,
    "wafer": 2,
    "fab": 2,
    "emib": 3,
    "advanced packaging": 3,
    "market share": 3,
    "chief executive": 2,
    "ceo": 2,
}

TYPE_TERMS = {
    "leak": [
        "leak",
        "leaked",
        "exclusive leak",
    ],
    "rumor": [
        "rumor",
        "rumour",
        "reportedly",
        "unconfirmed",
        "allegedly",
    ],
    "driver": [
        "driver",
        "graphics driver",
        "linux driver",
    ],
    "benchmark": [
        "benchmark",
        "geekbench",
        "3dmark",
        "performance test",
    ],
    "foundry": [
        "intel foundry",
        "foundry",
        "18a",
        "14a",
        "process node",
        "wafer",
        "fab",
        "emib",
        "advanced packaging",
    ],
    "financial": [
        "earnings",
        "revenue",
        "guidance",
        "margin",
        "shares",
        "stock",
        "capital",
        "dividend",
        "debt",
    ],
    "analysis": [
        "analysis",
        "deep dive",
        "teardown",
        "outlook",
        "forecast",
    ],
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


def phrase_present(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase).replace(r"\ ", r"[\s\-]+")
    pattern = rf"(?<!\w){escaped}(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def weighted_matches(
    text: str,
    terms: dict[str, int],
) -> tuple[int, list[str]]:
    matches = [
        phrase
        for phrase in terms
        if phrase_present(text, phrase)
    ]

    score = sum(terms[phrase] for phrase in matches)
    return score, sorted(matches)


def determine_content_type(
    title: str,
    summary: str,
) -> str:
    # Leaks und Gerüchte nur direkt aus dem Titel übernehmen.
    for content_type in ("leak", "rumor"):
        if any(
            phrase_present(title, phrase)
            for phrase in TYPE_TERMS[content_type]
        ):
            return content_type

    # Fachlich eindeutige Kategorien dürfen auch aus der Beschreibung
    # erkannt werden.
    combined_text = f"{title} {summary}"

    for content_type in (
        "driver",
        "benchmark",
        "foundry",
        "financial",
        "analysis",
    ):
        if any(
            phrase_present(combined_text, phrase)
            for phrase in TYPE_TERMS[content_type]
        ):
            return content_type

    return "news"


def classify_article(
    title: str,
    summary: str,
) -> tuple[str, str, float, str]:
    text = f"{title} {summary}"

    arc_score, arc_matches = weighted_matches(text, ARC_TERMS)
    stock_score, stock_matches = weighted_matches(text, STOCK_TERMS)

    if arc_score >= 3 and stock_score >= 3:
        target = "both"
    elif arc_score >= 3:
        target = "intelarc"
    elif stock_score >= 3:
        target = "intelstock"
    else:
        target = "unsuitable"

    content_type = determine_content_type(title, summary)

    strongest_score = max(arc_score, stock_score)
    confidence = min(
        0.99,
        round(0.50 + strongest_score / 20, 2),
    )

    reason = (
        f"arc_score={arc_score}; "
        f"arc_terms={','.join(arc_matches) or 'none'}; "
        f"stock_score={stock_score}; "
        f"stock_terms={','.join(stock_matches) or 'none'}"
    )

    return target, content_type, confidence, reason


def ensure_columns(connection: sqlite3.Connection) -> None:
    existing = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(articles)"
        )
    }

    additions = {
        "target_subreddit": "TEXT",
        "content_type": "TEXT",
        "classification_score": "REAL",
        "classification_reason": "TEXT",
        "classified_at": "TEXT",
    }

    for column, data_type in additions.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE articles "
                f"ADD COLUMN {column} {data_type}"
            )

    connection.commit()


def main() -> int:
    configure_logging()

    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    ensure_columns(connection)

    articles = connection.execute(
        """
        SELECT id, title, summary, status
        FROM articles
        WHERE relevant = 1
          AND status != 'blocked_source'
          AND classified_at IS NULL
        ORDER BY id ASC
        """
    ).fetchall()

    classified = 0
    unsuitable = 0

    for article in articles:
        target, content_type, confidence, reason = classify_article(
            article["title"] or "",
            article["summary"] or "",
        )

        new_status = article["status"]

        if article["status"] == "collected":
            if target == "unsuitable":
                new_status = "rejected_local"
                unsuitable += 1
            else:
                new_status = "classified"

        connection.execute(
            """
            UPDATE articles
            SET target_subreddit = ?,
                content_type = ?,
                classification_score = ?,
                classification_reason = ?,
                classified_at = ?,
                status = ?
            WHERE id = ?
            """,
            (
                target,
                content_type,
                confidence,
                reason,
                datetime.now(timezone.utc).isoformat(),
                new_status,
                article["id"],
            ),
        )

        classified += 1

        logging.info(
            "Artikel %s: Ziel=%s Typ=%s Sicherheit=%.2f",
            article["id"],
            target,
            content_type,
            confidence,
        )

    connection.commit()
    connection.close()

    logging.info(
        "%d Artikel klassifiziert, %d lokal abgelehnt.",
        classified,
        unsuitable,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
