"""Личный Telegram-бот «ЦТ каждый день».

Запуск: python bot.py  (токен берётся из BOT_TOKEN в файле .env рядом с этим файлом)
"""
from __future__ import annotations

import html
import logging
import os
import random
import secrets
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, ApplicationBuilder, ApplicationHandlerStop,
                          CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, TypeHandler, filters)

import core
from seed_data import SEED_QUESTIONS
from storage import Storage

BASE_DIR = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Minsk")
REMINDER_JOB = "daily_reminder"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PAIRING_ATTEMPTS = 5

BTN_QUIZ = "▶️ Начать тест"
BTN_IMPORT = "📥 Импорт из Quizlet"
BTN_ADD = "➕ Добавить вопрос"
BTN_STATS = "📊 Статистика"
BTN_SETTINGS = "⚙️ Настройки"
MENU = ReplyKeyboardMarkup(
    [[BTN_QUIZ], [BTN_IMPORT, BTN_ADD], [BTN_STATS, BTN_SETTINGS]], resize_keyboard=True
)

HELP = (
    "<b>ЦТ каждый день</b>\n\n"
    "/quiz — тест прямо сейчас (до 6 вопросов)\n"
    "/stats — результаты за 7 дней\n"
    "/import — импорт набора Quizlet\n"
    "/add — добавить вопрос\n"
    "/settings — настройки\n"
    "/time 20:45 — время напоминания (Минск)\n"
    "/pause и /resume — пауза напоминаний\n"
    "/testreminder — прислать напоминание сейчас\n"
    "/seed — стартовые вопросы\n"
    "/cancel — отменить ввод"
)

log = logging.getLogger("ct_bot")
h = html.escape
PAIRING = {"code": None, "attempts": 0}


# ====================================================================== утилиты
def today():
    return datetime.now(TZ).date()


def ikb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )


def store(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.application.bot_data["store"]


async def say(context, chat_id: int, text: str, markup=None) -> None:
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                   reply_markup=markup, disable_web_page_preview=True)


def short(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class RedactingFormatter(logging.Formatter):
    """Вырезает токен из любых строк лога, включая трассировки ошибок."""

    def __init__(self, fmt: str, secret: str):
        super().__init__(fmt)
        self.secret = secret

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text.replace(self.secret, "<BOT_TOKEN>") if self.secret else text


def setup_logging(token: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", token))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # httpx на уровне INFO пишет URL запросов, а в них есть токен.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ====================================================================== владелец
def new_pairing_code() -> None:
    PAIRING["code"] = secrets.token_hex(4).upper()
    PAIRING["attempts"] = 0
    print("\n" + "=" * 60, flush=True)
    print(f"  Одноразовый код привязки владельца:  {PAIRING['code']}", flush=True)
    print("  Отправьте его боту в личном чате Telegram.", flush=True)
    print("=" * 60 + "\n", flush=True)


async def try_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip().upper()
    code = PAIRING["code"]
    if code and secrets.compare_digest(text.encode(), code.encode()):
        st = store(context)
        st.set_owner(update.effective_user.id)
        PAIRING["code"] = None
        log.info("Владелец привязан (Telegram ID %s). Доступ ограничен этим ID.", update.effective_user.id)
        await update.message.reply_text(
            "✅ Вы привязаны как владелец бота.\n\n" + HELP, parse_mode=ParseMode.HTML, reply_markup=MENU
        )
        if not st.seed_confirmed():
            await say(context, update.effective_chat.id,
                      "Есть стартовый набор вопросов по тригонометрии и фонетике. "
                      "Он выключен, пока вы его не подтвердите: /seed")
        return
    PAIRING["attempts"] += 1
    if PAIRING["attempts"] >= MAX_PAIRING_ATTEMPTS:
        log.warning("Слишком много неверных попыток привязки, код заменён.")
        new_pairing_code()
    await update.message.reply_text(
        "Бот ещё не привязан. Отправьте одноразовый код из консоли, где запущен бот."
    )


async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Первый обработчик: пропускает дальше только владельца в личном чате."""
    st = store(context)
    owner = st.owner_id()
    user, chat = update.effective_user, update.effective_chat
    if owner is None:
        if chat and chat.type == ChatType.PRIVATE and update.message and update.message.text:
            await try_pair(update, context)
        raise ApplicationHandlerStop
    if user is None or user.id != owner or chat is None or chat.type != ChatType.PRIVATE:
        raise ApplicationHandlerStop


# ====================================================================== тест
async def start_quiz(context, chat_id: int) -> None:
    st = store(context)
    st.set_dialog(None)
    sess = st.active_session()
    if sess and sess["quiz_date"] == today().isoformat():
        await say(context, chat_id, "Продолжаем начатый тест.")
        await send_question(context, chat_id, sess)
        return
    if sess:
        st.close_session(sess["id"], "abandoned")
    ids = st.pick_cards(today())
    if not ids:
        hint = "" if st.seed_confirmed() else "\n• включите стартовые вопросы: /seed"
        await say(context, chat_id,
                  "На сегодня вопросов нет: новые карточки закончились, а повторения ещё не наступили.\n"
                  "• импортируйте набор Quizlet или добавьте вопрос" + hint, MENU)
        return
    sess = st.create_session(ids, today())
    note = "" if len(ids) >= 3 * core.PER_SUBJECT else \
        f" (материала хватило на {len(ids)} из {3 * core.PER_SUBJECT})"
    await say(context, chat_id, f"📝 Начинаем тест: {len(ids)} вопр.{note}")
    await send_question(context, chat_id, sess)


async def send_question(context, chat_id: int, sess: dict) -> None:
    st = store(context)
    pos, total, sid = sess["position"], len(sess["card_ids"]), sess["id"]
    card = st.get_card(sess["card_ids"][pos])
    header = f"<b>Вопрос {pos + 1}/{total}</b> · {core.SUBJECTS[card['subject']]}"
    if card["topic"]:
        header += f" · {h(card['topic'])}"
    if card["subject"] == "en":
        body = f"Напишите по-английски:\n<b>{h(card['question'])}</b>"
    else:
        body = h(card["question"])
    rows = []
    if card["choices"]:
        order = list(range(len(card["choices"])))
        random.Random(f"{sid}:{card['id']}").shuffle(order)
        rows = [[(card["choices"][i], f"a:{sid}:{pos}:{i}")] for i in order]
        hint = "Выберите вариант:"
    else:
        hint = "Напишите ответ сообщением."
    rows.append([("🤷 Не знаю", f"s:{sid}:{pos}")])
    await say(context, chat_id, f"{header}\n\n{body}\n\n<i>{hint}</i>", ikb(rows))


async def process_answer(context, chat_id: int, sid: int, pos: int, given: str | None) -> bool:
    """given=None означает «Не знаю». Возвращает False, если вопрос уже закрыт."""
    st = store(context)
    sess = st.get_session(sid)
    if not sess or sess["status"] != "active" or sess["position"] != pos:
        return False
    card = st.get_card(sess["card_ids"][pos])
    ok = given is not None and core.check_answer(given, card["answers"])
    sess = st.record_answer(sid, pos, given, ok, today())
    if sess is None:
        return False
    answers = " / ".join(card["answers"])
    if ok:
        prog = st.progress(card["id"])
        days = (datetime.fromisoformat(prog["due_date"]).date() - today()).days
        lines = ["✅ <b>Верно!</b>"]
        if len(card["answers"]) > 1:
            lines.append(f"Принимаются ответы: {h(answers)}")
        lines.append(f"Повторим через {days} дн.")
    else:
        lines = ["❌ <b>Неверно.</b>" if given is not None else "🤷 Пропущено."]
        if given is not None:
            lines.append(f"Ваш ответ: {h(given)}")
        lines.append(f"Правильный ответ: <b>{h(answers)}</b>")
        lines.append("Карточка вернётся завтра.")
    if card["explanation"]:
        lines.append(f"\n💡 {h(card['explanation'])}")
    await say(context, chat_id, "\n".join(lines))
    if sess["status"] == "finished":
        await send_summary(context, chat_id, sid)
    else:
        await send_question(context, chat_id, sess)
    return True


def format_summary(results: list[dict]) -> str:
    res = core.summarize(results)
    lines = [f"🏁 <b>Тест завершён: {res['correct']}/{res['total']}</b>", ""]
    for subj, (c, t) in res["by_subject"].items():
        lines.append(f"{core.SUBJECTS[subj]}: {c}/{t}")
    if res["errors"]:
        lines += ["", "<b>Ошибки:</b>"]
        for i, e in enumerate(res["errors"], 1):
            lines.append(
                f"{i}. [{core.SUBJECTS[e['subject']]}] {h(short(e['question']))}\n"
                f"    ваш ответ: {h(e['given'] or '—')}; верно: <b>{h(' / '.join(e['answers']))}</b>"
            )
        lines += ["", "Ошибки вернутся в завтрашнем тесте."]
    else:
        lines += ["", "Без ошибок 🎉"]
    return "\n".join(lines)


async def send_summary(context, chat_id: int, sid: int) -> None:
    await say(context, chat_id, format_summary(store(context).session_results(sid)), MENU)


# ====================================================================== статистика
def format_stats(st: Storage) -> str:
    d = today()
    data = st.stats(d)
    total_ok = sum(c for c, _ in data["by_day"].values())
    total = sum(n for _, n in data["by_day"].values())
    pct = f" ({round(100 * total_ok / total)}%)" if total else ""
    lines = [f"📊 <b>Последние 7 дней</b> ({data['start'][8:10]}.{data['start'][5:7]}–{d:%d.%m})",
             f"Ответов: {total_ok}/{total}{pct} · завершено тестов: {data['finished_tests']}", ""]
    for subj, (c, n) in data["by_subject"].items():
        lines.append(f"{core.SUBJECTS[subj]}: {c}/{n}" if n else f"{core.SUBJECTS[subj]}: —")
    lines += ["", "<b>По дням:</b>"]
    for day, (c, n) in data["by_day"].items():
        lines.append(f"{day[8:10]}.{day[5:7]}: {f'{c}/{n}' if n else '—'}")
    lines += ["", "<b>Карточки</b> (всего / начато / к повторению сегодня):"]
    for subj, cnt in st.card_counts(d).items():
        lines.append(f"{core.SUBJECTS[subj]}: {cnt['total']} / {cnt['started']} / {cnt['due']}")
    return "\n".join(lines)


async def show_stats(context, chat_id: int) -> None:
    await say(context, chat_id, format_stats(store(context)), MENU)


# ====================================================================== импорт Quizlet
async def start_import(context, chat_id: int) -> None:
    st = store(context)
    st.set_dialog({"type": "import", "step": "name"})
    decks = st.list_decks()
    known = ""
    if decks:
        known = "\n\nУже есть наборы (то же название — дополнить набор):\n" + \
                "\n".join(f"• {h(n)} — {c}" for n, c in decks)
    await say(context, chat_id, "📥 <b>Импорт из Quizlet</b>\nВведите название набора." + known
              + "\n\n/cancel — отмена")


async def import_preview(context, chat_id: int, dialog: dict, text: str) -> None:
    st = store(context)
    res = core.parse_quizlet(text)
    if not res.pairs:
        await say(context, chat_id,
                  "Не нашёл ни одной пары. Нужен формат: английское выражение, табуляция, перевод; "
                  "каждая карточка на новой строке. Попробуйте ещё раз или пришлите .txt файл.")
        return
    existing = st.count_existing_pairs(res.pairs)
    dialog.update(step="confirm", pairs=res.pairs)
    st.set_dialog(dialog)
    lines = [f"Набор «<b>{h(dialog['deck'])}</b>»",
             f"Карточек: <b>{len(res.pairs)}</b>",
             f"Новых будет добавлено: {len(res.pairs) - existing}"]
    if existing:
        lines.append(f"Уже есть в базе: {existing} (пропустим, прогресс сохранится)")
    if res.duplicates:
        lines.append(f"Повторов внутри текста: {res.duplicates} (учтены один раз)")
    if res.skipped:
        lines.append(f"Строк без разделителя пропущено: {res.skipped}")
    lines += ["", "<b>Первые три пары:</b>"]
    lines += [f"• {h(en)} — {h(ru)}" for en, ru in res.pairs[:3]]
    await say(context, chat_id, "\n".join(lines),
              ikb([[("✅ Подтвердить", "imp:ok"), ("✖️ Отмена", "imp:cancel")]]))


async def import_confirm(context, chat_id: int) -> None:
    st = store(context)
    dialog = st.get_dialog()
    if not dialog or dialog.get("type") != "import" or dialog.get("step") != "confirm":
        await say(context, chat_id, "Нечего подтверждать — начните импорт заново.", MENU)
        return
    added, existed = st.import_pairs(dialog["deck"], dialog["pairs"])
    st.set_dialog(None)
    log.info("Импорт Quizlet: добавлено %s, пропущено как существующие %s", added, existed)
    await say(context, chat_id, f"✅ Импорт завершён: добавлено {added}, уже было {existed}.", MENU)


# ====================================================================== добавление вопроса
ADD_PROMPTS = {
    "topic": "Тема (например: «Тригонометрические тождества»).",
    "question": "Текст вопроса.",
    "answers": "Правильный ответ. Если допустимых ответов несколько — через «;».",
    "explanation": "Объяснение (или «-», чтобы пропустить).",
    "choices": "Варианты для кнопок через «;» (правильный тоже включите) "
               "или «-», если ответ вводится текстом.",
}
ADD_PROMPTS_EN = {
    "question": "Русский перевод — его бот покажет в тесте.",
    "answers": "Английский ответ. Несколько допустимых — через «;».",
}


def add_prompt(dialog: dict, step: str, st: Storage) -> str:
    subj = dialog["data"]["subject"]
    text = (ADD_PROMPTS_EN if subj == "en" else {}).get(step, ADD_PROMPTS[step])
    if step == "topic":
        topics = st.topics(subj)
        if topics:
            text += "\nУже есть: " + "; ".join(h(t) for t in topics[:15])
    return text + "\n\n/cancel — отмена"


async def start_add(context, chat_id: int) -> None:
    store(context).set_dialog({"type": "add", "step": "subject", "data": {}})
    await say(context, chat_id, "➕ <b>Новый вопрос</b>\nВыберите предмет:",
              ikb([[("Математика", "add:subj:math"), ("Русский", "add:subj:ru")],
                   [("Английский", "add:subj:en"), ("✖️ Отмена", "add:cancel")]]))


async def add_step(context, chat_id: int, dialog: dict, text: str) -> None:
    st = store(context)
    data, step = dialog["data"], dialog["step"]
    text = text.strip()
    if step == "subject":
        await say(context, chat_id, "Выберите предмет кнопкой выше.")
        return
    if step == "topic":
        data["topic"], nxt = text[:100], "question"
    elif step == "question":
        data["question"], nxt = text, "answers"
    elif step == "answers":
        answers = core.split_list(text)
        if not answers:
            await say(context, chat_id, "Нужен хотя бы один ответ.")
            return
        data["answers"], nxt = answers, "explanation"
    elif step == "explanation":
        data["explanation"], nxt = (None if text in ("-", "—") else text), "choices"
    elif step == "choices":
        choices = None if text in ("-", "—") else core.split_list(text)
        if choices is not None:
            if len(choices) < 2 or not any(core.check_answer(c, data["answers"]) for c in choices):
                await say(context, chat_id,
                          "Нужно минимум 2 варианта, и один из них должен совпадать с правильным ответом. "
                          "Введите варианты ещё раз или «-».")
                return
        data["choices"], nxt = choices, "confirm"
    else:
        await say(context, chat_id, "Нажмите «Сохранить» или «Отмена».")
        return
    dialog["step"] = nxt
    st.set_dialog(dialog)
    if nxt != "confirm":
        await say(context, chat_id, add_prompt(dialog, nxt, st))
        return
    lines = [f"<b>Проверьте вопрос</b>",
             f"Предмет: {core.SUBJECTS[data['subject']]}",
             f"Тема: {h(data['topic'])}",
             f"Вопрос: {h(data['question'])}",
             f"Ответы: {h(' / '.join(data['answers']))}",
             f"Объяснение: {h(data['explanation'] or '—')}",
             f"Варианты: {h(' | '.join(data['choices'])) if data['choices'] else 'ввод текстом'}"]
    await say(context, chat_id, "\n".join(lines),
              ikb([[("💾 Сохранить", "add:ok"), ("✖️ Отмена", "add:cancel")]]))


async def add_save(context, chat_id: int) -> None:
    st = store(context)
    dialog = st.get_dialog()
    if not dialog or dialog.get("type") != "add" or dialog.get("step") != "confirm":
        await say(context, chat_id, "Нечего сохранять — начните заново.", MENU)
        return
    d = dialog["data"]
    card_id = st.add_question(d["subject"], d["topic"], d["question"], d["answers"],
                              d["explanation"], d["choices"])
    st.set_dialog(None)
    if card_id:
        await say(context, chat_id, "💾 Сохранено. Вопрос попадёт в тест как новый.", MENU)
    else:
        await say(context, chat_id, "Такой вопрос уже есть — дубль не добавлен.", MENU)


# ====================================================================== настройки и напоминания
async def show_settings(context, chat_id: int) -> None:
    st = store(context)
    paused = st.reminders_paused()
    text = (f"⚙️ <b>Настройки</b>\n\n"
            f"Напоминание: <b>{st.remind_time()}</b> (Europe/Minsk) — "
            f"{'⏸ на паузе' if paused else '🔔 включено'}\n"
            f"Стартовые вопросы: {'включены' if st.seed_confirmed() else 'ждут подтверждения'}")
    rows = [[("🕘 Изменить время", "set:time")],
            [("▶️ Возобновить напоминания", "set:resume") if paused else ("⏸ Пауза напоминаний", "set:pause")],
            [("🔔 Проверить напоминание", "set:test")]]
    if not st.seed_confirmed():
        rows.append([("📚 Стартовые вопросы", "set:seed")])
    await say(context, chat_id, text, ikb(rows))


def schedule_reminder(app: Application) -> None:
    st: Storage = app.bot_data["store"]
    for job in app.job_queue.get_jobs_by_name(REMINDER_JOB):
        job.schedule_removal()
    hh, mm = core.parse_time(st.remind_time()) or (20, 45)
    app.job_queue.run_daily(reminder_job, time=dtime(hh, mm, tzinfo=TZ), name=REMINDER_JOB)
    log.info("Напоминание запланировано на %02d:%02d Europe/Minsk%s", hh, mm,
             " (сейчас на паузе)" if st.reminders_paused() else "")


async def send_reminder(context, chat_id: int) -> None:
    await say(context, chat_id, "🔔 <b>ЦТ каждый день</b>\nПора на тест: до 6 вопросов — "
                                "английский, математика, русский.",
              ikb([[("▶️ Начать тест", "q:start")]]))


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    st = store(context)
    owner = st.owner_id()
    if owner and not st.reminders_paused():
        await send_reminder(context, owner)
        log.info("Напоминание отправлено.")


async def set_time(context, chat_id: int, text: str) -> bool:
    parsed = core.parse_time(text)
    if not parsed:
        await say(context, chat_id, "Не понял время. Формат ЧЧ:ММ, например 20:45.")
        return False
    st = store(context)
    st.set_remind_time(f"{parsed[0]:02d}:{parsed[1]:02d}")
    st.set_dialog(None)
    schedule_reminder(context.application)
    await say(context, chat_id, f"🕘 Напоминание будет приходить в {st.remind_time()} по Минску.", MENU)
    return True


async def set_paused(context, chat_id: int, paused: bool) -> None:
    store(context).set_reminders_paused(paused)
    await say(context, chat_id, "⏸ Напоминания на паузе. Вернуть: /resume" if paused
              else "🔔 Напоминания включены.", MENU)


async def show_seed(context, chat_id: int) -> None:
    st = store(context)
    cards = st.seed_cards()
    if st.seed_confirmed():
        await say(context, chat_id, f"Стартовые вопросы уже включены ({len(cards)} шт.).", MENU)
        return
    lines = [f"📚 <b>Стартовый набор: {len(cards)} вопросов</b>"]
    topic = None
    for c in cards:
        if c["topic"] != topic:
            topic = c["topic"]
            lines.append(f"\n<b>{core.SUBJECTS[c['subject']]} — {h(topic)}</b>")
        lines.append(f"• {h(short(c['question'], 90))}")
    lines.append("\nВключить их в ежедневные тесты?")
    await say(context, chat_id, "\n".join(lines),
              ikb([[("✅ Включить", "seed:ok"), ("Не сейчас", "seed:no")]]))


# ====================================================================== маршрутизация
MENU_ACTIONS = {
    BTN_QUIZ: start_quiz, BTN_IMPORT: start_import, BTN_ADD: start_add,
    BTN_STATS: show_stats, BTN_SETTINGS: show_settings,
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = store(context)
    chat_id = update.effective_chat.id
    text = update.message.text
    action = MENU_ACTIONS.get(text.strip())
    if action:
        st.set_dialog(None)
        await action(context, chat_id)
        return
    dialog = st.get_dialog()
    if dialog:
        if dialog["type"] == "import":
            if dialog["step"] == "name":
                dialog.update(step="text", deck=text.strip()[:100])
                st.set_dialog(dialog)
                await say(context, chat_id,
                          "Вставьте текст экспорта Quizlet (английский<TAB>перевод, карточка на строке) "
                          "или пришлите файл .txt.\nДля длинных наборов надёжнее файл: Telegram режет "
                          "сообщения длиннее 4096 символов.")
            elif dialog["step"] == "text":
                await import_preview(context, chat_id, dialog, text)
            else:
                await say(context, chat_id, "Нажмите «Подтвердить» или «Отмена» под предпросмотром.")
        elif dialog["type"] == "add":
            await add_step(context, chat_id, dialog, text)
        elif dialog["type"] == "time":
            await set_time(context, chat_id, text)
        return
    sess = st.active_session()
    if sess:
        await process_answer(context, chat_id, sess["id"], sess["position"], text)
        return
    await say(context, chat_id, "Нажмите «Начать тест» или /help.", MENU)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = store(context)
    chat_id = update.effective_chat.id
    dialog = st.get_dialog()
    doc = update.message.document
    if not dialog or dialog["type"] != "import" or dialog["step"] != "text":
        await say(context, chat_id, "Файл принимается во время импорта: кнопка «Импорт из Quizlet».")
        return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await say(context, chat_id, "Нужен файл с расширением .txt.")
        return
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await say(context, chat_id, "Файл слишком большой (максимум 2 МБ).")
        return
    tg_file = await doc.get_file()
    data = bytes(await tg_file.download_as_bytearray())
    await import_preview(context, chat_id, dialog, core.decode_text_file(data))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id
    st = store(context)
    parts = data.split(":")

    async def drop_buttons():
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass

    if parts[0] in ("a", "s") and len(parts) >= 3:
        sid, pos = int(parts[1]), int(parts[2])
        accepted = False
        if parts[0] == "s":
            accepted = await process_answer(context, chat_id, sid, pos, None)
        else:
            sess = st.get_session(sid)
            if sess and sess["position"] == pos and sess["status"] == "active":
                card = st.get_card(sess["card_ids"][pos])
                given = card["choices"][int(parts[3])]
                accepted = await process_answer(context, chat_id, sid, pos, given)
        await query.answer() if accepted else await query.answer("Этот вопрос уже закрыт.")
        await drop_buttons()
        return

    await query.answer()
    if data == "q:start":
        await drop_buttons()
        await start_quiz(context, chat_id)
    elif data == "imp:ok":
        await drop_buttons()
        await import_confirm(context, chat_id)
    elif data in ("imp:cancel", "add:cancel"):
        st.set_dialog(None)
        await drop_buttons()
        await say(context, chat_id, "Отменено.", MENU)
    elif data.startswith("add:subj:"):
        dialog = st.get_dialog()
        if not dialog or dialog.get("type") != "add":
            return
        dialog["data"]["subject"] = parts[2]
        dialog["step"] = "topic"
        st.set_dialog(dialog)
        await drop_buttons()
        await say(context, chat_id, f"Предмет: {core.SUBJECTS[parts[2]]}\n" + add_prompt(dialog, "topic", st))
    elif data == "add:ok":
        await drop_buttons()
        await add_save(context, chat_id)
    elif data == "set:time":
        st.set_dialog({"type": "time"})
        await say(context, chat_id, "Введите время напоминания по Минску в формате ЧЧ:ММ, например 20:45.")
    elif data in ("set:pause", "set:resume"):
        await drop_buttons()
        await set_paused(context, chat_id, data == "set:pause")
    elif data == "set:test":
        await send_reminder(context, chat_id)
    elif data == "set:seed":
        await show_seed(context, chat_id)
    elif data == "seed:ok":
        n = st.activate_seed()
        await drop_buttons()
        await say(context, chat_id, f"✅ Стартовые вопросы включены: {n}.", MENU)
    elif data == "seed:no":
        await drop_buttons()
        await say(context, chat_id, "Хорошо, стартовые вопросы остаются выключенными. Включить: /seed", MENU)


def command(func, *pass_args):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await func(context, update.effective_chat.id, *pass_args)
    return handler


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=MENU)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store(context).set_dialog(None)
    await update.message.reply_text("Ввод отменён.", reply_markup=MENU)


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if context.args:
        await set_time(context, chat_id, context.args[0])
    else:
        store(context).set_dialog({"type": "time"})
        await say(context, chat_id, f"Сейчас: {store(context).remind_time()}. "
                                    "Введите новое время ЧЧ:ММ (по Минску).")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Ошибка при обработке обновления", exc_info=context.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("quiz", "Тест сейчас"), BotCommand("stats", "Результаты за 7 дней"),
        BotCommand("import", "Импорт из Quizlet"), BotCommand("add", "Добавить вопрос"),
        BotCommand("settings", "Настройки"), BotCommand("time", "Время напоминания"),
        BotCommand("pause", "Пауза напоминаний"), BotCommand("resume", "Включить напоминания"),
        BotCommand("testreminder", "Проверить напоминание"), BotCommand("seed", "Стартовые вопросы"),
        BotCommand("cancel", "Отменить ввод"), BotCommand("help", "Справка"),
    ])
    schedule_reminder(app)


# ====================================================================== сборка и запуск
def resolve_db_path() -> str:
    """DB_PATH, иначе файл на томе Railway, иначе ct_bot.sqlite3 рядом с bot.py."""
    if os.getenv("DB_PATH"):
        return os.environ["DB_PATH"]
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if volume:
        return str(Path(volume) / "ct_bot.sqlite3")
    if os.getenv("RAILWAY_ENVIRONMENT"):
        log.warning("На Railway не подключён том: база пропадёт при каждом деплое. См. README.")
    return str(BASE_DIR / "ct_bot.sqlite3")


def build_app(token: str, db_path: str | None = None) -> Application:
    db_path = db_path or resolve_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    log.info("База SQLite: %s", db_path)
    st = Storage(db_path)
    st.ensure_seed(SEED_QUESTIONS)
    app = ApplicationBuilder().token(token).post_init(post_init).build()
    if app.job_queue is None:
        raise RuntimeError("Нет JobQueue: установите зависимости командой pip install -r requirements.txt")
    app.bot_data["store"] = st

    app.add_handler(TypeHandler(Update, access_gate), group=-1)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("quiz", command(start_quiz)))
    app.add_handler(CommandHandler("stats", command(show_stats)))
    app.add_handler(CommandHandler("import", command(start_import)))
    app.add_handler(CommandHandler("add", command(start_add)))
    app.add_handler(CommandHandler("settings", command(show_settings)))
    app.add_handler(CommandHandler("seed", command(show_seed)))
    app.add_handler(CommandHandler("pause", command(set_paused, True)))
    app.add_handler(CommandHandler("resume", command(set_paused, False)))
    app.add_handler(CommandHandler("testreminder", command(send_reminder)))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    token = (os.getenv("BOT_TOKEN") or "").strip()
    setup_logging(token)
    if not token:
        print("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather.")
        sys.exit(1)
    app = build_app(token)
    if app.bot_data["store"].owner_id() is None:
        new_pairing_code()
    else:
        log.info("Владелец уже привязан, бот отвечает только ему.")
    log.info("Бот запущен (long polling). Остановка: Ctrl+C")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
