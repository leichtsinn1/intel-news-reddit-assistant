#!/usr/bin/env python3

import hashlib
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests


BASE_DIR = Path("/opt/intel-news-bot")
CONFIG_FILE = BASE_DIR / "config" / "sources.json"
DATABASE_FILE = BASE_DIR / "data" / "news.db"
LOG_FILE = BASE_DIR / "logs" / "collector.log"

USER_AGENT = (
    "IntelNewsScout/0.1 "
    "(private non-commercial RSS reader; contact configured by administrator)"
)

KEYWORDS = {
    "intel",
    "intc",
    "intel arc",
    "arc battlemage",
    "arc celestial",
    "arc alchemist",
    "xe2",
    "xe3",
    "xeon",
    "gaudi",
    "intel foundry",
    "intel foundry services",
    "18a",
    "14a",
    "panther lake",
    "nova lake",
    "lunar lake",
    "arrow lake",
    "clearwater forest",
    "diamond rapids",
    "granite rapids",
    "falcon shores",
}

TRACKING_PARAMETERS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def configure_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def connect_database() -> sqlite3.Connection:
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_FILE)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            original_url TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            published TEXT,
            summary TEXT,
            relevant INTEGER NOT NULL,
            matched_terms TEXT,
            status TEXT NOT NULL DEFAULT 'collected',
            discovered_at TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_status
        ON articles(status)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_relevant
        ON articles(relevant)
        """
    )

    connection.commit()
    return connection


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    clean_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS
    ]

    path = parts.path.rstrip("/") or "/"

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urlencode(clean_query),
            "",
        )
    )


def make_fingerprint(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def classify(title: str, summary: str) -> tuple[bool, list[str]]:
    def find_matches(text: str) -> set[str]:
        matches: set[str] = set()

        for keyword in KEYWORDS:
            escaped = re.escape(keyword).replace(r"\ ", r"[\s\-]+")
            pattern = rf"(?<!\w){escaped}(?!\w)"

            if re.search(pattern, text, flags=re.IGNORECASE):
                matches.add(keyword)

        return matches

    title_matches = find_matches(title)
    summary_matches = find_matches(summary)
    matches = sorted(title_matches | summary_matches)

    # Pressemitteilungen ohne direkten Intel-Bezug im Titel
    # werden zunächst nicht weitergeleitet.
    is_press_release = title.strip().casefold().startswith("(pr)")

    if is_press_release and not title_matches:
        return False, matches

    # Ein Treffer im Titel genügt.
    # Im Beschreibungstext werden mindestens zwei Intel-Begriffe verlangt.
    relevant = bool(title_matches) or len(summary_matches) >= 2

    return relevant, matches


def load_sources() -> list[dict]:
    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        sources = json.load(file)

    if not isinstance(sources, list):
        raise ValueError("sources.json muss eine Liste enthalten.")

    return sources


def fetch_feed(source: dict, session: requests.Session) -> list[dict]:
    name = source["name"]
    url = source["url"]

    logging.info("Prüfe Quelle: %s", name)

    response = session.get(url, timeout=30)
    response.raise_for_status()

    parsed = feedparser.parse(response.content)

    if parsed.bozo:
        logging.warning(
            "Feed Warnung bei %s: %s",
            name,
            parsed.bozo_exception,
        )

    return parsed.entries[:50]


def store_entries(
    connection: sqlite3.Connection,
    source_name: str,
    entries: list[dict],
) -> tuple[int, int]:
    inserted = 0
    relevant_inserted = 0

    for entry in entries:
        title = clean_html(entry.get("title", ""))
        original_url = entry.get("link", "").strip()
        summary = clean_html(
            entry.get("summary", entry.get("description", ""))
        )
        published = entry.get("published", entry.get("updated", ""))

        if not title or not original_url:
            continue

        normalized_url = normalize_url(original_url)
        fingerprint = make_fingerprint(normalized_url)
        relevant, matched_terms = classify(title, summary)

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO articles (
                fingerprint,
                source,
                title,
                original_url,
                normalized_url,
                published,
                summary,
                relevant,
                matched_terms,
                discovered_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                source_name,
                title,
                original_url,
                normalized_url,
                published,
                summary,
                int(relevant),
                json.dumps(matched_terms, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        if cursor.rowcount == 1:
            inserted += 1

            if relevant:
                relevant_inserted += 1
                print()
                print(f"[NEU] {source_name}")
                print(f"Titel: {title}")
                print(f"Treffer: {', '.join(matched_terms)}")
                print(f"Link: {original_url}")

    connection.commit()
    return inserted, relevant_inserted


def main() -> int:
    configure_logging()
    connection = connect_database()
    sources = load_sources()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml, text/xml;q=0.9, */*;q=0.1"
            ),
        }
    )

    total_inserted = 0
    total_relevant = 0
    failures = 0

    for source in sources:
        if not source.get("enabled", True):
            logging.info(
                "Quelle deaktiviert: %s (%s)",
                source.get("name", "unbekannt"),
                source.get("reason", "kein Grund angegeben"),
            )
            continue

        try:
            entries = fetch_feed(source, session)
            inserted, relevant = store_entries(
                connection,
                source["name"],
                entries,
            )

            total_inserted += inserted
            total_relevant += relevant

            logging.info(
                "%s: %d Einträge gelesen, %d neu, %d relevant",
                source["name"],
                len(entries),
                inserted,
                relevant,
            )

        except Exception as error:
            failures += 1
            logging.exception(
                "Quelle %s konnte nicht verarbeitet werden: %s",
                source.get("name", "unbekannt"),
                error,
            )

    connection.close()

    logging.info(
        "Fertig: %d neue Artikel, davon %d relevant, %d Fehler",
        total_inserted,
        total_relevant,
        failures,
    )

    return 1 if failures == len(sources) else 0


if __name__ == "__main__":
    raise SystemExit(main())
