#!/usr/bin/env python3

import logging
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

BASE_DIR = Path("/opt/intel-news-bot")
DATABASE_FILE = BASE_DIR / "data" / "news.db"
LOG_FILE = BASE_DIR / "logs" / "deduplicator.log"

LOOKBACK_DAYS = 7
DUPLICATE_THRESHOLD = 0.72

SOURCE_PRIORITY = {
    "Intel": 100,
    "Intel Newsroom": 100,
    "Intel Investor Relations": 100,
    "SEC": 100,
    "Research Paper": 95,
    "Patent Source": 90,
    "SemiAnalysis": 75,
    "Semiconductor Engineering": 70,
    "TechPowerUp": 55,
    "Tom's Hardware": 50,
    "Wccftech": 40,
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for",
    "from", "has", "have", "how", "in", "into", "is", "it",
    "its", "new", "news", "of", "on", "or", "says", "that",
    "the", "their", "this", "to", "up", "with", "will",
    "intel", "report", "reports", "reportedly",
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


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    )
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def title_tokens(title: str) -> set[str]:
    return {
        token
        for token in normalize_text(title).split()
        if len(token) >= 2 and token not in STOPWORDS
    }


def event_similarity(
    first_title: str,
    second_title: str,
) -> tuple[float, str]:
    first_normalized = normalize_text(first_title)
    second_normalized = normalize_text(second_title)

    if not first_normalized or not second_normalized:
        return 0.0, "empty_title"

    first_tokens = title_tokens(first_title)
    second_tokens = title_tokens(second_title)

    shared_tokens = first_tokens & second_tokens
    all_tokens = first_tokens | second_tokens

    if not all_tokens:
        return 0.0, "no_tokens"

    jaccard = len(shared_tokens) / len(all_tokens)

    sequence = SequenceMatcher(
        None,
        first_normalized,
        second_normalized,
    ).ratio()

    first_numbers = {
        token for token in first_tokens if any(c.isdigit() for c in token)
    }
    second_numbers = {
        token for token in second_tokens if any(c.isdigit() for c in token)
    }

    shared_numbers = first_numbers & second_numbers

    score = 0.65 * jaccard + 0.35 * sequence

    if shared_numbers:
        score += 0.08

    if len(shared_tokens) < 3:
        score *= 0.65

    score = min(round(score, 3), 1.0)

    reason = (
        f"jaccard={jaccard:.3f}; "
        f"sequence={sequence:.3f}; "
        f"shared={','.join(sorted(shared_tokens)) or 'none'}; "
        f"shared_numbers={','.join(sorted(shared_numbers)) or 'none'}"
    )

    return score, reason


def targets_overlap(first: str, second: str) -> bool:
    if not first or not second:
        return False

    if first == "both" or second == "both":
        return True

    return first == second


def ensure_columns(connection: sqlite3.Connection) -> None:
    existing = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(articles)"
        )
    }

    additions = {
        "source_priority": "INTEGER",
        "duplicate_of": "INTEGER",
        "duplicate_score": "REAL",
        "duplicate_reason": "TEXT",
        "duplicate_checked_at": "TEXT",
    }

    for column, data_type in additions.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE articles "
                f"ADD COLUMN {column} {data_type}"
            )

    connection.commit()


def source_priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 30)


def main() -> int:
    configure_logging()

    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    ensure_columns(connection)

    cutoff = datetime.now(timezone.utc) - timedelta(
        days=LOOKBACK_DAYS
    )

    candidates = connection.execute(
        """
        SELECT
            id,
            source,
            title,
            normalized_url,
            target_subreddit,
            content_type,
            discovered_at
        FROM articles
        WHERE relevant = 1
          AND status = 'classified'
          AND target_subreddit != 'unsuitable'
        ORDER BY id ASC
        """
    ).fetchall()

    existing_articles = connection.execute(
        """
        SELECT
            id,
            source,
            title,
            normalized_url,
            target_subreddit,
            content_type,
            discovered_at,
            status
        FROM articles
        WHERE relevant = 1
          AND status IN (
              'ready_notify',
              'notified',
              'approved_waiting_reddit',
              'approved',
              'published'
          )
        ORDER BY id DESC
        """
    ).fetchall()

    comparison_pool = []

    for article in existing_articles:
        try:
            discovered = datetime.fromisoformat(
                article["discovered_at"]
            )

            if discovered >= cutoff:
                comparison_pool.append(article)

        except (TypeError, ValueError):
            comparison_pool.append(article)

    unique_count = 0
    duplicate_count = 0

    ordered_candidates = sorted(
        candidates,
        key=lambda article: (
            source_priority(article["source"]),
            -article["id"],
        ),
        reverse=True,
    )

    for article in ordered_candidates:
        best_duplicate = None
        best_score = 0.0
        best_reason = ""

        for existing in comparison_pool:
            if not targets_overlap(
                article["target_subreddit"],
                existing["target_subreddit"],
            ):
                continue

            if (
                article["normalized_url"]
                and article["normalized_url"]
                == existing["normalized_url"]
            ):
                score = 1.0
                reason = "identical_normalized_url"
            else:
                score, reason = event_similarity(
                    article["title"],
                    existing["title"],
                )

            if score > best_score:
                best_duplicate = existing
                best_score = score
                best_reason = reason

        now = datetime.now(timezone.utc).isoformat()
        priority = source_priority(article["source"])

        if (
            best_duplicate is not None
            and best_score >= DUPLICATE_THRESHOLD
        ):
            connection.execute(
                """
                UPDATE articles
                SET status = 'duplicate_local',
                    source_priority = ?,
                    duplicate_of = ?,
                    duplicate_score = ?,
                    duplicate_reason = ?,
                    duplicate_checked_at = ?
                WHERE id = ?
                """,
                (
                    priority,
                    best_duplicate["id"],
                    best_score,
                    best_reason,
                    now,
                    article["id"],
                ),
            )

            duplicate_count += 1

            logging.info(
                "Artikel %s ist wahrscheinlich Duplikat von %s "
                "mit Score %.3f",
                article["id"],
                best_duplicate["id"],
                best_score,
            )

        else:
            connection.execute(
                """
                UPDATE articles
                SET status = 'ready_notify',
                    source_priority = ?,
                    duplicate_of = NULL,
                    duplicate_score = ?,
                    duplicate_reason = ?,
                    duplicate_checked_at = ?
                WHERE id = ?
                """,
                (
                    priority,
                    best_score,
                    best_reason or "no_similar_event",
                    now,
                    article["id"],
                ),
            )

            unique_count += 1

            comparison_pool.append(
                {
                    "id": article["id"],
                    "source": article["source"],
                    "title": article["title"],
                    "normalized_url": article["normalized_url"],
                    "target_subreddit": article["target_subreddit"],
                    "content_type": article["content_type"],
                    "discovered_at": article["discovered_at"],
                    "status": "ready_notify",
                }
            )

            logging.info(
                "Artikel %s ist lokal eindeutig und bereit.",
                article["id"],
            )

    connection.commit()
    connection.close()

    logging.info(
        "%d Artikel bereit, %d lokale Duplikate.",
        unique_count,
        duplicate_count,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
