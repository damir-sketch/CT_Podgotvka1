"""Чистая логика бота «ЦТ каждый день»: без Telegram и без базы данных.

Здесь всё, что удобно проверять тестами: нормализация и проверка ответов,
разбор экспорта Quizlet, интервальное повторение, подбор вопросов и подсчёт баллов.
"""
from __future__ import annotations

import re
import unicodedata
from collections import namedtuple
from datetime import date, timedelta

SUBJECTS = {"en": "Английский", "math": "Математика", "ru": "Русский"}
SUBJECT_ORDER = ("en", "math", "ru")
PER_SUBJECT = 2  # вопросов по каждому предмету в дневном тесте
INTERVALS = (3, 7, 14)  # дни до повтора после 1-го, 2-го, 3-го и следующих верных ответов

_SPACES = re.compile(r"\s+")
_CHAR_MAP = str.maketrans({
    "’": "'", "‘": "'", "ʼ": "'", "`": "'",
    "“": '"', "”": '"', "«": '"', "»": '"',
    "−": "-", "–": "-", "—": "-",
    "ё": "е",  # ё -> е
    " ": " ",
})


# ---------------------------------------------------------------- ответы
def normalize_answer(text: str | None) -> str:
    """Игнорирует регистр, лишние пробелы, вид кавычек/тире, ё/е и точку в конце.

    Написание слов не исправляется: «recieve» не равно «receive».
    """
    s = unicodedata.normalize("NFC", text or "").translate(_CHAR_MAP)
    s = _SPACES.sub(" ", s).strip().lower()
    s = s.rstrip(".!").strip()
    return s


def check_answer(given: str | None, accepted: list[str]) -> bool:
    g = normalize_answer(given)
    return bool(g) and any(g == normalize_answer(a) for a in accepted)


def split_list(text: str) -> list[str]:
    """«a; b | c» или по строкам -> ['a', 'b', 'c'] без пустых и повторов."""
    items = re.split(r"[;\n|]", text or "")
    out: dict[str, str] = {}
    for item in items:
        item = _SPACES.sub(" ", item).strip()
        if item and normalize_answer(item) not in out:
            out[normalize_answer(item)] = item
    return list(out.values())


# ---------------------------------------------------------------- Quizlet
ParseResult = namedtuple("ParseResult", "pairs skipped duplicates")

# Если Telegram заменил табуляцию пробелами, пробуем запасные разделители.
_FALLBACK_SEPARATORS = (
    re.compile(r"\s{2,}"),
    re.compile(r"\s+[—–]\s+"),
    re.compile(r"\s+-\s+"),
)


def pair_key(term: str, definition: str) -> str:
    return normalize_answer(term) + "\t" + normalize_answer(definition)


def parse_quizlet(text: str) -> ParseResult:
    """Экспорт Quizlet: «термин<TAB>определение», карточка на строке.

    Возвращает уникальные пары (английский, русский), число строк, которые
    не удалось разобрать, и число дублей внутри самого текста.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    skipped = dups = 0
    for raw in (text or "").lstrip("﻿").splitlines():
        line = raw.strip(" \r\n﻿")
        if not line.strip():
            continue
        if "\t" in line:
            term, definition = line.split("\t", 1)
        else:
            for rx in _FALLBACK_SEPARATORS:
                parts = rx.split(line.strip(), maxsplit=1)
                if len(parts) == 2:
                    term, definition = parts
                    break
            else:
                skipped += 1
                continue
        term = _SPACES.sub(" ", term).strip()
        definition = _SPACES.sub(" ", definition).strip()
        if not term or not definition:
            skipped += 1
            continue
        key = pair_key(term, definition)
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        pairs.append((term, definition))
    return ParseResult(pairs, skipped, dups)


def decode_text_file(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1251"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        return text
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- повторение
def next_review(streak: int, correct: bool, today: date) -> tuple[int, date]:
    """Ошибка -> завтра. Верно -> через 3, затем 7, затем 14 дней (и далее 14)."""
    if not correct:
        return 0, today + timedelta(days=1)
    streak += 1
    return streak, today + timedelta(days=INTERVALS[min(streak, len(INTERVALS)) - 1])


def plan_subject(subject: str, new_ids: list[int], due_ids: list[int], n: int = PER_SUBJECT) -> list[int]:
    """Выбирает до n карточек предмета без дублей.

    Английский: по возможности одна новая и одна на повторение; если какой-то
    категории нет, добираем из другой. Остальные предметы: сначала повторение.
    """
    if subject == "en":
        order = new_ids[:1] + due_ids[:1] + due_ids[1:] + new_ids[1:]
    else:
        order = due_ids + new_ids
    return list(dict.fromkeys(order))[:n]


# ---------------------------------------------------------------- итоги
def summarize(results: list[dict]) -> dict:
    """results: [{subject, correct, question, given, answers}] -> счёт и ошибки."""
    by_subject: dict[str, list[int]] = {}
    errors = []
    for r in results:
        stat = by_subject.setdefault(r["subject"], [0, 0])
        stat[1] += 1
        if r["correct"]:
            stat[0] += 1
        else:
            errors.append(r)
    ordered = {s: tuple(by_subject[s]) for s in SUBJECT_ORDER if s in by_subject}
    correct = sum(c for c, _ in ordered.values())
    total = sum(t for _, t in ordered.values())
    return {"correct": correct, "total": total, "by_subject": ordered, "errors": errors}


def parse_time(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text or "")
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mnt <= 59:
        return h, mnt
    return None
