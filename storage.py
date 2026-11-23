"""SQLite-хранилище: настройки, владелец, карточки, прогресс, сессии и ответы."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS decks (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY,
    subject     TEXT NOT NULL,             -- en | math | ru
    deck_id     INTEGER REFERENCES decks(id),
    topic       TEXT,
    question    TEXT NOT NULL,             -- для английского: русский перевод
    answers     TEXT NOT NULL,             -- JSON-список допустимых ответов
    explanation TEXT,
    choices     TEXT,                      -- JSON-список вариантов или NULL
    source      TEXT NOT NULL,             -- quizlet | user | seed
    active      INTEGER NOT NULL DEFAULT 1,
    dedup_key   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cards_dedup ON cards(subject, dedup_key);
CREATE TABLE IF NOT EXISTS progress (
    card_id       INTEGER PRIMARY KEY REFERENCES cards(id),
    streak        INTEGER NOT NULL,
    due_date      TEXT NOT NULL,
    last_answered TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    quiz_date  TEXT NOT NULL,
    started_at TEXT NOT NULL,
    card_ids   TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'active'   -- active | finished | abandoned
);
CREATE TABLE IF NOT EXISTS answers (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES sessions(id),
    card_id     INTEGER REFERENCES cards(id),
    subject     TEXT NOT NULL,
    given       TEXT,
    correct     INTEGER NOT NULL,
    answer_date TEXT NOT NULL,
    answered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_answers_date ON answers(answer_date);
"""

DEFAULT_REMIND_TIME = "20:45"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ настройки
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str | None) -> None:
        with self.conn:
            if value is None:
                self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self.conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def owner_id(self) -> int | None:
        v = self.get("owner_id")
        return int(v) if v else None

    def set_owner(self, user_id: int) -> None:
        self.set("owner_id", str(user_id))

    def remind_time(self) -> str:
        return self.get("remind_time", DEFAULT_REMIND_TIME)

    def set_remind_time(self, hhmm: str) -> None:
        self.set("remind_time", hhmm)

    def reminders_paused(self) -> bool:
        return self.get("reminders_paused") == "1"

    def set_reminders_paused(self, paused: bool) -> None:
        self.set("reminders_paused", "1" if paused else "0")

    def get_dialog(self) -> dict | None:
        v = self.get("dialog")
        return json.loads(v) if v else None

    def set_dialog(self, dialog: dict | None) -> None:
        self.set("dialog", json.dumps(dialog, ensure_ascii=False) if dialog else None)

    # ------------------------------------------------------------ карточки
    def _deck_id(self, name: str) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO decks(name, created_at) VALUES(?, ?)", (name, _now())
        )
        return self.conn.execute("SELECT id FROM decks WHERE name = ?", (name,)).fetchone()["id"]

    def list_decks(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT d.name, COUNT(c.id) AS n FROM decks d "
            "LEFT JOIN cards c ON c.deck_id = d.id GROUP BY d.id ORDER BY d.name"
        ).fetchall()
        return [(r["name"], r["n"]) for r in rows]

    def count_existing_pairs(self, pairs: list[tuple[str, str]]) -> int:
        n = 0
        for en, ru in pairs:
            if self.conn.execute(
                "SELECT 1 FROM cards WHERE subject = 'en' AND dedup_key = ?", (core.pair_key(en, ru),)
            ).fetchone():
                n += 1
        return n

    def import_pairs(self, deck_name: str, pairs: list[tuple[str, str]]) -> tuple[int, int]:
        """Добавляет только новые пары. Существующие карточки и их прогресс не трогает."""
        added = 0
        with self.conn:
            deck_id = self._deck_id(deck_name)
            for en, ru in pairs:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO cards(subject, deck_id, topic, question, answers, source, "
                    "dedup_key, created_at) VALUES('en', ?, ?, ?, ?, 'quizlet', ?, ?)",
                    (deck_id, deck_name, ru, json.dumps([en], ensure_ascii=False),
                     core.pair_key(en, ru), _now()),
                )
                added += cur.rowcount
        return added, len(pairs) - added

    def add_question(self, subject: str, topic: str, question: str, answers: list[str],
                     explanation: str | None = None, choices: list[str] | None = None,
                     source: str = "user", active: bool = True) -> int | None:
        """Возвращает id новой карточки или None, если такой вопрос уже есть."""
        if subject == "en":
            key = core.pair_key(answers[0], question)
        else:
            key = core.normalize_answer(question)
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO cards(subject, topic, question, answers, explanation, choices, "
                "source, active, dedup_key, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (subject, topic, question, json.dumps(answers, ensure_ascii=False), explanation or None,
                 json.dumps(choices, ensure_ascii=False) if choices else None,
                 source, int(active), key, _now()),
            )
        return cur.lastrowid if cur.rowcount else None

    def topics(self, subject: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT topic FROM cards WHERE subject = ? AND topic IS NOT NULL AND active = 1 "
            "ORDER BY topic", (subject,)
        ).fetchall()
        return [r["topic"] for r in rows]

    def get_card(self, card_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT c.*, d.name AS deck FROM cards c LEFT JOIN decks d ON d.id = c.deck_id WHERE c.id = ?",
            (card_id,),
        ).fetchone()
        if not row:
            return None
        card = dict(row)
        card["answers"] = json.loads(card["answers"])
        card["choices"] = json.loads(card["choices"]) if card["choices"] else None
        return card

    def card_counts(self, today: date) -> dict[str, dict[str, int]]:
        out = {}
        for s in core.SUBJECT_ORDER:
            row = self.conn.execute(
                "SELECT COUNT(*) AS total, COUNT(p.card_id) AS started, "
                "SUM(CASE WHEN p.due_date <= ? THEN 1 ELSE 0 END) AS due "
                "FROM cards c LEFT JOIN progress p ON p.card_id = c.id "
                "WHERE c.subject = ? AND c.active = 1",
                (today.isoformat(), s),
            ).fetchone()
            out[s] = {"total": row["total"], "started": row["started"], "due": row["due"] or 0}
        return out

    # ------------------------------------------------------------ стартовый набор
    def ensure_seed(self, seed: list[dict]) -> None:
        """Один раз кладёт стартовые вопросы в базу выключенными (active = 0)."""
        if self.get("seed_loaded") == "1":
            return
        for q in seed:
            self.add_question(q["subject"], q["topic"], q["question"], q["answers"],
                              q.get("explanation"), q.get("choices"), source="seed", active=False)
        self.set("seed_loaded", "1")

    def seed_confirmed(self) -> bool:
        return self.get("seed_confirmed") == "1"

    def seed_cards(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id FROM cards WHERE source = 'seed' ORDER BY subject, id"
        ).fetchall()
        return [self.get_card(r["id"]) for r in rows]

    def activate_seed(self) -> int:
        with self.conn:
            cur = self.conn.execute("UPDATE cards SET active = 1 WHERE source = 'seed'")
        self.set("seed_confirmed", "1")
        return cur.rowcount

    # ------------------------------------------------------------ подбор вопросов
    def due_ids(self, subject: str, today: date) -> list[int]:
        rows = self.conn.execute(
            "SELECT c.id FROM cards c JOIN progress p ON p.card_id = c.id "
            "WHERE c.subject = ? AND c.active = 1 AND p.due_date <= ? ORDER BY p.due_date, c.id",
            (subject, today.isoformat()),
        ).fetchall()
        return [r["id"] for r in rows]

    def new_ids(self, subject: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT c.id FROM cards c LEFT JOIN progress p ON p.card_id = c.id "
            "WHERE c.subject = ? AND c.active = 1 AND p.card_id IS NULL ORDER BY c.id",
            (subject,),
        ).fetchall()
        return [r["id"] for r in rows]

    def pick_cards(self, today: date) -> list[int]:
        picked: list[int] = []
        for s in core.SUBJECT_ORDER:
            picked += core.plan_subject(s, self.new_ids(s), self.due_ids(s, today))
        return list(dict.fromkeys(picked))

    # ------------------------------------------------------------ сессии
    @staticmethod
    def _session(row) -> dict | None:
        if not row:
            return None
        s = dict(row)
        s["card_ids"] = json.loads(s["card_ids"])
        return s

    def create_session(self, card_ids: list[int], today: date) -> dict:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO sessions(quiz_date, started_at, card_ids) VALUES(?, ?, ?)",
                (today.isoformat(), _now(), json.dumps(card_ids)),
            )
        return self.get_session(cur.lastrowid)

    def get_session(self, session_id: int) -> dict | None:
        return self._session(self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())

    def active_session(self) -> dict | None:
        return self._session(self.conn.execute(
            "SELECT * FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone())

    def close_session(self, session_id: int, status: str = "abandoned") -> None:
        with self.conn:
            self.conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))

    def record_answer(self, session_id: int, position: int, given: str | None,
                      correct: bool, today: date) -> dict | None:
        """Сохраняет ответ на текущий вопрос, обновляет повторение и сдвигает сессию.

        Возвращает обновлённую сессию или None, если вопрос уже не текущий
        (например, повторное нажатие кнопки).
        """
        with self.conn:
            sess = self.get_session(session_id)
            if not sess or sess["status"] != "active" or sess["position"] != position:
                return None
            card_id = sess["card_ids"][position]
            subject = self.conn.execute("SELECT subject FROM cards WHERE id = ?", (card_id,)).fetchone()["subject"]
            self.conn.execute(
                "INSERT INTO answers(session_id, card_id, subject, given, correct, answer_date, answered_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (session_id, card_id, subject, given, int(correct), today.isoformat(), _now()),
            )
            prev = self.conn.execute("SELECT streak FROM progress WHERE card_id = ?", (card_id,)).fetchone()
            streak, due = core.next_review(prev["streak"] if prev else 0, correct, today)
            self.conn.execute(
                "INSERT INTO progress(card_id, streak, due_date, last_answered) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(card_id) DO UPDATE SET streak = excluded.streak, "
                "due_date = excluded.due_date, last_answered = excluded.last_answered",
                (card_id, streak, due.isoformat(), today.isoformat()),
            )
            new_pos = position + 1
            status = "finished" if new_pos >= len(sess["card_ids"]) else "active"
            self.conn.execute("UPDATE sessions SET position = ?, status = ? WHERE id = ?",
                              (new_pos, status, session_id))
        return self.get_session(session_id)

    def session_results(self, session_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT a.subject, a.correct, a.given, c.question, c.answers, c.topic "
            "FROM answers a JOIN cards c ON c.id = a.card_id WHERE a.session_id = ? ORDER BY a.id",
            (session_id,),
        ).fetchall()
        return [{**dict(r), "answers": json.loads(r["answers"])} for r in rows]

    def progress(self, card_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM progress WHERE card_id = ?", (card_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ статистика
    def stats(self, today: date, days: int = 7) -> dict:
        start = (today - timedelta(days=days - 1)).isoformat()
        by_day = {
            (today - timedelta(days=i)).isoformat(): [0, 0] for i in range(days - 1, -1, -1)
        }
        by_subject = {s: [0, 0] for s in core.SUBJECT_ORDER}
        rows = self.conn.execute(
            "SELECT answer_date, subject, SUM(correct) AS ok, COUNT(*) AS n FROM answers "
            "WHERE answer_date >= ? AND answer_date <= ? GROUP BY answer_date, subject",
            (start, today.isoformat()),
        ).fetchall()
        for r in rows:
            by_day[r["answer_date"]][0] += r["ok"]
            by_day[r["answer_date"]][1] += r["n"]
            if r["subject"] in by_subject:
                by_subject[r["subject"]][0] += r["ok"]
                by_subject[r["subject"]][1] += r["n"]
        tests = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE status = 'finished' AND quiz_date >= ?", (start,)
        ).fetchone()["n"]
        return {"start": start, "by_day": by_day, "by_subject": by_subject, "finished_tests": tests}
