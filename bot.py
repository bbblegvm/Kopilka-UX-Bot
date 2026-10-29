"""Telegram-бот «Копилка» с маскотом, монетами и магазином аксессуаров.

Структура:
  /start -> маскот приветствует -> «Добавить цель» (название, сумма, срок кнопкой или текстом)
  -> ГЛАВНОЕ МЕНЮ (маскот, цель, прогресс, срок, монеты) с кнопками:
       «Пополнить копилку» / «Прогресс» (расписание по дням) / «Магазин»
  + уведомление раз в день в REMIND_TIME.
"""
import asyncio
import html
import logging
import math
import os
import re
import sqlite3
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)  # значения из .env важнее переменных системы
except ImportError:  # python-dotenv необязателен
    pass

# ---------------------------------------------------------------- настройки
TOKEN = os.getenv("BOT_TOKEN", "")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
REMIND_TIME = os.getenv("REMIND_TIME", "10:00")  # во сколько приходит ежедневное уведомление (ЧЧ:ММ)
REMIND_WINDOW_MIN = int(os.getenv("REMIND_WINDOW_MIN", "180"))  # сколько минут после REMIND_TIME ещё можно отправить
COINS_PER_TOPUP = int(os.getenv("COINS_PER_TOPUP", "50"))  # монет за пополнение (не чаще раза в день)
CURRENCY = os.getenv("CURRENCY", "₽")
DB_PATH = os.getenv("DB_PATH", "piggybank.db")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.isdigit()}

_h, _m = (int(x) for x in REMIND_TIME.strip().replace(".", ":").split(":"))  # «19:00», «19.00», «9:05»
REMIND_AT = dtime(_h, _m)
REMIND_TIME = f"{_h:02d}:{_m:02d}"

ASSETS = Path(__file__).parent / "assets"
# ключ: (название, цена в монетах, файл с маскотом в этом аксессуаре)
SHOP = {
    "bow": ("бантик", 30, "mascot_bow.png"),
    "hat": ("шапочка", 50, "mascot_hat.png"),
    "glasses": ("очки", 70, "mascot_glasses.png"),
}
PLAIN_MASCOT = "mascot.png"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
# Готовые сроки: (подпись, дней). «Неделя (тест)» нужна на время пилота.
TERM_CHOICES = [
    [("Неделя (тест)", 7)],
    [("1 месяц", 30), ("3 месяца", 91)],
    [("6 месяцев", 182), ("1 год", 365)],
]

# Текст приветствия — поменяйте как нужно
GREETING = "Привет! Я крутой маскот, который поможет тебе накопить на мечту! 🌟"

log = logging.getLogger("piggy")
router = Router()


# ---------------------------------------------------------------- база данных
def _run(sql: str, args: tuple = (), fetch: bool = False):
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall() if fetch else cur.rowcount
        c.commit()
        return rows
    finally:
        c.close()


def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return _run(sql, args, fetch=True)


def ex(sql: str, args: tuple = ()) -> int:
    """Выполняет запрос и возвращает число затронутых строк."""
    return _run(sql, args)


def init_db() -> None:
    c = sqlite3.connect(DB_PATH)
    try:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                coins INTEGER NOT NULL DEFAULT 0,
                intro_seen INTEGER NOT NULL DEFAULT 0,   -- уже получал монеты и знает, на что их тратить
                equipped TEXT,                            -- какой аксессуар надет
                last_notified TEXT,                       -- дата последнего ежедневного уведомления
                blocked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS goals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                target INTEGER NOT NULL,
                saved INTEGER NOT NULL DEFAULT 0,
                deadline TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',    -- active | done | archived
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                day TEXT NOT NULL,
                at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS owned(
                user_id INTEGER NOT NULL,
                item TEXT NOT NULL,
                PRIMARY KEY(user_id, item)
            );
            """
        )
        c.commit()
    finally:
        c.close()


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def ensure_user(user_id: int, chat_id: int) -> sqlite3.Row:
    ex(
        "INSERT INTO users(user_id, chat_id, created_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, blocked=0",
        (user_id, chat_id, now().isoformat()),
    )
    return q("SELECT * FROM users WHERE user_id=?", (user_id,))[0]


def active_goal(user_id: int) -> sqlite3.Row | None:
    rows = q("SELECT * FROM goals WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1", (user_id,))
    return rows[0] if rows else None


def last_goal(user_id: int) -> sqlite3.Row | None:
    rows = q("SELECT * FROM goals WHERE user_id=? AND status!='archived' ORDER BY id DESC LIMIT 1", (user_id,))
    return rows[0] if rows else None


def user_topped_up_today(user_id: int) -> bool:
    """Было ли сегодня хоть одно пополнение у пользователя (по любой его цели)."""
    return bool(
        q(
            "SELECT 1 FROM topups t JOIN goals g ON g.id=t.goal_id WHERE g.user_id=? AND t.day=? LIMIT 1",
            (user_id, today().isoformat()),
        )
    )


def has_topups(goal_id: int) -> bool:
    return bool(q("SELECT 1 FROM topups WHERE goal_id=? LIMIT 1", (goal_id,)))


def owned_items(user_id: int) -> set[str]:
    return {r["item"] for r in q("SELECT item FROM owned WHERE user_id=?", (user_id,))}


# ---------------------------------------------------------------- форматирование и расчёты
def num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def money(n: int) -> str:
    return f"{num(n)} {CURRENCY}"


def coins_word(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return f"{n} монета"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return f"{n} монеты"
    return f"{n} монет"


def fmt_date(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%d.%m.%Y")


def parse_amount(text: str) -> int | None:
    t = re.sub(r"[\s_]", "", text).replace(",", ".")
    t = re.sub(r"[^\d.]", "", t)
    try:
        v = int(float(t))
    except ValueError:
        return None
    return v if 0 < v <= 1_000_000_000 else None


def parse_term(text: str) -> int | None:
    """'3', '3 мес', '10 недель', '90 дней', '1 год' -> дни (без единицы — месяцы)."""
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*([а-яёa-z]*)\s*$", text.lower())
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    unit = m.group(2)
    if unit.startswith(("д", "d")):
        mult = 1
    elif unit.startswith(("н", "w")):
        mult = 7
    elif unit.startswith(("г", "л", "y")):
        mult = 365
    else:
        mult = 30.4
    days = round(n * mult)
    return days if 1 <= days <= 3650 else None


def progress_bar(saved: int, target: int) -> str:
    pct = min(100, int(saved * 100 / target)) if target else 0
    filled = pct // 10
    return f"{'▓' * filled}{'░' * (10 - filled)} {pct}%"


def plan_for(goal: sqlite3.Row) -> dict:
    """Сколько откладывать в день. Если сегодня уже пополнял — расписание идёт с завтрашнего дня."""
    remaining = max(0, goal["target"] - goal["saved"])
    days_left = (date.fromisoformat(goal["deadline"]) - today()).days
    start = 1 if (days_left > 1 and user_topped_up_today(goal["user_id"])) else 0
    n = max(1, days_left - start)
    return {
        "remaining": remaining,
        "days_left": days_left,
        "start": start,
        "n": n,
        "daily": math.ceil(remaining / n) if remaining else 0,
    }


def day_label(d: date) -> str:
    delta = (d - today()).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    return f"{d.strftime('%d.%m')}, {WEEKDAYS[d.weekday()]}"


def progress_short(g: sqlite3.Row) -> str:
    return f"{num(g['saved'])} / {num(g['target'])} {CURRENCY}"


def menu_caption(g: sqlite3.Row, u: sqlite3.Row, title: str | None = None, footer: str | None = None) -> str:
    lines = [
        f"🎯 Цель: <b>{html.escape(g['name'])}</b>",
        f"📊 Прогресс: {progress_short(g)}",
        f"📅 Срок: до {fmt_date(g['deadline'])}",
        f"🪙 Монеты: {u['coins']}",
    ]
    if g["status"] == "done":
        lines.append("\n🏆 Цель достигнута!")
    return (f"{title}\n\n" if title else "") + "\n".join(lines) + (f"\n\n{footer}" if footer else "")


def progress_text(g: sqlite3.Row) -> str:
    p = plan_for(g)
    lines = [
        f"📊 <b>Прогресс: «{html.escape(g['name'])}»</b>",
        progress_bar(g["saved"], g["target"]),
        f"Накоплено: {progress_short(g)}",
    ]
    if g["status"] == "done" or p["remaining"] == 0:
        lines.append("\n🏆 Цель достигнута!")
    elif p["days_left"] <= 0:
        lines.append(
            f"Осталось накопить: {money(p['remaining'])}\n\n"
            f"⏰ Срок вышел ({fmt_date(g['deadline'])}), но копить можно и дальше: пополняй, сколько получится."
        )
    else:
        lines.append(f"Осталось накопить: {money(p['remaining'])}")
        lines.append(f"До цели: {p['days_left']} дн. (до {fmt_date(g['deadline'])})")
        lines.append(f"\nОткладывай по <b>{money(p['daily'])}</b> в день. Расписание:")
        cum_prev = g["saved"]
        for i in range(min(7, p["n"])):
            d = today() + timedelta(days=p["start"] + i)
            cum = min(g["target"], g["saved"] + p["daily"] * (i + 1))
            lines.append(f"• {day_label(d)} — {money(cum - cum_prev)} (всего {money(cum)})")
            cum_prev = cum
        if p["n"] > 7:
            lines.append(f"… и так каждый день до {fmt_date(g['deadline'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- клавиатуры
def kb_of(*rows: list[tuple[str, str]]):
    kb = InlineKeyboardBuilder()
    for row in rows:
        for text, data in row:
            kb.button(text=text, callback_data=data)
    kb.adjust(*[len(r) for r in rows])
    return kb.as_markup()


BTN_ADD = [("➕ Добавить цель", "new")]
BTN_BACK = [("↩️ Вернуться к копилке", "back")]
KB_ADD = kb_of(BTN_ADD)
KB_MENU = kb_of([("💰 Пополнить копилку", "topup")], [("📊 Прогресс", "progress"), ("🛍 Магазин", "shop")])
KB_MENU_DONE = kb_of(BTN_ADD, [("📊 Прогресс", "progress"), ("🛍 Магазин", "shop")])
KB_TERMS = kb_of(*[[(label, f"term:{days}") for label, days in row] for row in TERM_CHOICES])


def kb_notify():
    return kb_of([("💰 Пополнить", "topup"), ("😴 Пока не хочу", "later")])


async def drop_buttons(cb: CallbackQuery) -> None:
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


def mascot_path(equipped: str | None, mood: str | None = None) -> Path:
    """Файл маскота в нужном наряде. Для настроения (happy / sad) ищет <наряд>_<настроение>.png,
    если такого файла нет — берёт обычную картинку."""
    base = SHOP[equipped][2] if equipped in SHOP else PLAIN_MASCOT
    if mood:
        candidate = ASSETS / f"{Path(base).stem}_{mood}.png"
        if candidate.exists():
            return candidate
    return ASSETS / base


async def send_mascot(msg: Message, equipped: str | None, caption: str, markup, mood: str | None = None) -> None:
    await msg.answer_photo(FSInputFile(mascot_path(equipped, mood)), caption=caption, reply_markup=markup)


async def show_menu(
    msg: Message, user_id: int, chat_id: int, title: str | None = None, footer: str | None = None
) -> None:
    """Главное меню: маскот + цель, прогресс, срок, монеты + кнопки."""
    u = ensure_user(user_id, chat_id)
    g = last_goal(user_id)
    if not g:
        await send_mascot(msg, u["equipped"], GREETING, KB_ADD)
        return
    kb = KB_MENU_DONE if g["status"] == "done" else KB_MENU
    await send_mascot(msg, u["equipped"], menu_caption(g, u, title, footer), kb)


async def show_progress(msg: Message, user_id: int, chat_id: int) -> None:
    ensure_user(user_id, chat_id)
    g = last_goal(user_id)
    if not g:
        await msg.answer("У тебя пока нет цели. Давай добавим!", reply_markup=KB_ADD)
        return
    top = BTN_ADD if g["status"] == "done" else [("💰 Пополнить копилку", "topup")]
    await msg.answer(progress_text(g), reply_markup=kb_of(top, BTN_BACK))


# ---------------------------------------------------------------- состояния
class NewGoal(StatesGroup):
    name = State()
    target = State()
    term = State()


class TopUp(StatesGroup):
    amount = State()


# Любое нажатие кнопки сбрасывает незаконченный ввод (кроме выбора срока,
# которому нужны уже введённые название и сумма).
@router.callback_query.outer_middleware()
async def reset_state(handler, event, data):
    state = data.get("state")
    if state and not (event.data or "").startswith("term:"):
        await state.clear()
    return await handler(event, data)


# ---------------------------------------------------------------- старт и команды
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m, m.from_user.id, m.chat.id)


@router.message(Command("new"))
async def cmd_new(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user.id, m.chat.id)
    await begin_new_goal(m, m.from_user.id, state)


@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m, m.from_user.id, m.chat.id)


@router.message(Command("progress"))
async def cmd_progress(m: Message, state: FSMContext):
    await state.clear()
    await show_progress(m, m.from_user.id, m.chat.id)


@router.message(Command("shop"))
async def cmd_shop(m: Message, state: FSMContext):
    await state.clear()
    u = ensure_user(m.from_user.id, m.chat.id)
    await show_shop(m, u)


@router.message(Command("mascot"))
async def cmd_mascot(m: Message, state: FSMContext):
    await state.clear()
    u = ensure_user(m.from_user.id, m.chat.id)
    await show_mascot(m, u)


@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>")


# ---------------------------------------------------------------- создание цели
async def begin_new_goal(msg: Message, user_id: int, state: FSMContext) -> None:
    g = active_goal(user_id)
    if g:
        await msg.answer(
            f"У тебя уже есть цель «{html.escape(g['name'])}» ({progress_short(g)}).\n"
            f"Закрыть её и создать новую? Накопленное в ней при этом не сохранится.",
            reply_markup=kb_of([("Да, создать новую", "new_yes")], [("Нет, оставить", "back")]),
        )
        return
    await state.set_state(NewGoal.name)
    await msg.answer("Введи название цели 🎯")


@router.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await drop_buttons(cb)
    ensure_user(cb.from_user.id, cb.message.chat.id)
    await begin_new_goal(cb.message, cb.from_user.id, state)


@router.callback_query(F.data == "new_yes")
async def cb_new_yes(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await drop_buttons(cb)
    ex("UPDATE goals SET status='archived' WHERE user_id=? AND status='active'", (cb.from_user.id,))
    await state.set_state(NewGoal.name)
    await cb.message.answer("Введи название цели 🎯")


@router.message(NewGoal.name, F.text)
async def got_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not 1 <= len(name) <= 60:
        await m.answer("Название должно быть до 60 символов. Попробуй ещё раз 🙂")
        return
    await state.update_data(name=name)
    await state.set_state(NewGoal.target)
    await m.answer("Отлично! Сколько денег тебе нужно?")


@router.message(NewGoal.target, F.text)
async def got_target(m: Message, state: FSMContext):
    amount = parse_amount(m.text)
    if amount is None:
        await m.answer("Не разобрал сумму 🤔 Напиши число, например: 45000")
        return
    data = await state.update_data(target=amount)
    await state.set_state(NewGoal.term)
    await m.answer(
        f"Отлично! За какой срок хочешь накопить на {html.escape(data['name'])}? "
        "Выбери из предложенных вариантов или напиши сам в формате число / единица измерения\n"
        "Например: 3 месяца, 10 недель, 90 дней",
        reply_markup=KB_TERMS,
    )


async def finish_goal(msg: Message, user_id: int, chat_id: int, data: dict, days: int) -> None:
    ensure_user(user_id, chat_id)
    deadline = today() + timedelta(days=days)
    ex(
        "INSERT INTO goals(user_id, name, target, saved, deadline, status, created_at) VALUES(?,?,?,0,?,'active',?)",
        (user_id, data["name"], data["target"], deadline.isoformat(), now().isoformat()),
    )
    daily = math.ceil(data["target"] / days)
    await show_menu(
        msg,
        user_id,
        chat_id,
        title="Готово! Твоя копилка создана 🎉",
        footer=f"💡 Чтобы достичь цели к выбранному сроку, нужно откладывать по <b>{money(daily)}</b> в день!",
    )


@router.message(NewGoal.term, F.text)
async def got_term_text(m: Message, state: FSMContext):
    days = parse_term(m.text)
    if days is None:
        await m.answer(
            "Не понял срок 🤔 Выбери кнопку или напиши, например: «3 месяца», «12 недель» или «60 дней».",
            reply_markup=KB_TERMS,
        )
        return
    data = await state.get_data()
    await state.clear()
    await finish_goal(m, m.from_user.id, m.chat.id, data, days)


@router.callback_query(NewGoal.term, F.data.startswith("term:"))
async def got_term_button(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await drop_buttons(cb)
    days = int(cb.data.split(":")[1])
    data = await state.get_data()
    await state.clear()
    await finish_goal(cb.message, cb.from_user.id, cb.message.chat.id, data, days)


# ---------------------------------------------------------------- пополнение
@router.callback_query(F.data == "topup")
async def cb_topup(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await drop_buttons(cb)
    ensure_user(cb.from_user.id, cb.message.chat.id)
    if not active_goal(cb.from_user.id):
        await cb.message.answer("Активной цели нет. Давай добавим новую!", reply_markup=KB_ADD)
        return
    await state.set_state(TopUp.amount)  # число можно и просто написать, не нажимая кнопки
    g = active_goal(cb.from_user.id)
    daily = plan_for(g)["daily"]
    await cb.message.answer(
        "Сколько денег ты отложил? 💰\n"
        "Нажми на сумму по плану или выбери «Другая сумма».",
        reply_markup=kb_of([(f"💰 {money(daily)}", f"amt:{daily}"), ("✏️ Другая сумма", "amt:other")]),
    )


@router.callback_query(F.data.startswith("amt:"))
async def cb_amount(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await drop_buttons(cb)
    value = cb.data.split(":", 1)[1]
    if value == "other":
        if not active_goal(cb.from_user.id):
            await cb.message.answer("Активной цели нет. Давай добавим новую!", reply_markup=KB_ADD)
            return
        await state.set_state(TopUp.amount)
        await cb.message.answer("Введи сумму, которую ты отложил ✍️")
        return
    await apply_topup(cb.message, cb.from_user.id, cb.message.chat.id, int(value))


@router.message(TopUp.amount, F.text)
async def got_topup(m: Message, state: FSMContext):
    amount = parse_amount(m.text)
    if amount is None:
        await m.answer("Не разобрал сумму 🤔 Напиши число, например: 500")
        return
    await state.clear()
    await apply_topup(m, m.from_user.id, m.chat.id, amount)


async def apply_topup(m: Message, user_id: int, chat_id: int, amount: int) -> None:
    """Записывает пополнение, начисляет монеты (раз в день) и показывает РАДОСТНОГО маскота."""
    u = ensure_user(user_id, chat_id)
    g = active_goal(user_id)
    if not g:
        await m.answer("Активной цели нет. Давай добавим новую!", reply_markup=KB_ADD)
        return

    first_today = not user_topped_up_today(user_id)  # монеты — только за первое пополнение дня
    saved = g["saved"] + amount
    done = saved >= g["target"]
    ex(
        "INSERT INTO topups(goal_id, amount, day, at) VALUES(?,?,?,?)",
        (g["id"], amount, today().isoformat(), now().isoformat()),
    )
    ex("UPDATE goals SET saved=?, status=? WHERE id=?", (saved, "done" if done else "active", g["id"]))

    text = (
        "Отлично! Ты стал на шаг ближе к своей цели. 🎉\n"
        f"Твой прогресс: {num(saved)} / {money(g['target'])}."
    )
    if first_today:
        ex("UPDATE users SET coins=coins+? WHERE user_id=?", (COINS_PER_TOPUP, user_id))
        text += f"\nЗа пополнение ты получаешь {coins_word(COINS_PER_TOPUP)}! 🪙"
        if not u["intro_seen"]:
            ex("UPDATE users SET intro_seen=1 WHERE user_id=?", (user_id,))
            text += "\n\nМонеты можно потратить в магазине на одежду и аксессуары для маскота 🛍"
    else:
        text += "\nМонеты за сегодня ты уже получил, новые будут за пополнение завтра 🪙"
    if done:
        text += f"\n\n🏆 Цель «{html.escape(g['name'])}» достигнута! Ты молодец!"

    rows = [[("📊 Прогресс", "progress"), ("🛍 Магазин", "shop")]]
    if done:
        rows.append(BTN_ADD)
    rows.append(BTN_BACK)
    await send_mascot(m, u["equipped"], text, kb_of(*rows), mood="happy")


@router.callback_query(F.data.in_({"back", "menu"}))
async def cb_back(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    await show_menu(cb.message, cb.from_user.id, cb.message.chat.id)


@router.callback_query(F.data == "progress")
async def cb_progress(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    await show_progress(cb.message, cb.from_user.id, cb.message.chat.id)


@router.callback_query(F.data == "later")
async def cb_later(cb: CallbackQuery):
    """«Пока не хочу» в уведомлении: маскот расстраивается, но оставляет путь к пополнению."""
    await cb.answer()
    await drop_buttons(cb)
    u = ensure_user(cb.from_user.id, cb.message.chat.id)
    await send_mascot(
        cb.message,
        u["equipped"],
        "Ох… Маскот немного расстроился 😢\nНо он подождёт, пока ты будешь готов!",
        kb_of([("💰 Пополнить копилку", "topup")], [("📊 Прогресс", "progress")]),
        mood="sad",
    )


# ---------------------------------------------------------------- магазин и маскот
async def show_shop(msg: Message, u: sqlite3.Row) -> None:
    owned = owned_items(u["user_id"])
    lines = ["🛍 <b>Магазин</b>\nВыбери, что хочешь купить:"]
    rows = []
    for key, (name, price, _) in SHOP.items():
        if key in owned:
            lines.append(f"• {name} — куплено ✅")
            rows.append([(f"✅ {name.capitalize()} (надеть)", f"buy:{key}")])
        else:
            lines.append(f"• {name} — {coins_word(price)}")
            rows.append([(f"{name.capitalize()} — {price} 🪙", f"buy:{key}")])
    lines.append(f"\nБаланс: {coins_word(u['coins'])}")
    rows.append(BTN_BACK)
    await msg.answer("\n".join(lines), reply_markup=kb_of(*rows))


@router.callback_query(F.data == "shop")
async def cb_shop(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    u = ensure_user(cb.from_user.id, cb.message.chat.id)
    await show_shop(cb.message, u)


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    key = cb.data.split(":", 1)[1]
    u = ensure_user(cb.from_user.id, cb.message.chat.id)
    if key not in SHOP:
        return
    name, price, _ = SHOP[key]
    uid = cb.from_user.id
    after = kb_of([("🐭 Посмотреть маскота", "mascot")], BTN_BACK)

    if key in owned_items(uid):  # уже куплено — просто надеваем
        ex("UPDATE users SET equipped=? WHERE user_id=?", (key, uid))
        await send_mascot(cb.message, key, f"Надел: {name}! Вот как маскот выглядит сейчас. ✨", after)
        return

    # атомарное списание: не уйдём в минус даже при двойном нажатии
    if ex("UPDATE users SET coins=coins-? WHERE user_id=? AND coins>=?", (price, uid, price)) == 0:
        await cb.message.answer(
            f"Пока не хватает монет: нужно {price}, а у тебя {u['coins']}.\n"
            f"Их можно получить за новые пополнения копилки.",
            reply_markup=kb_of([("👀 Посмотреть другие предметы", "shop")], BTN_BACK),
        )
        return
    ex("INSERT OR IGNORE INTO owned(user_id, item) VALUES(?,?)", (uid, key))
    ex("UPDATE users SET equipped=? WHERE user_id=?", (key, uid))
    balance = q("SELECT coins FROM users WHERE user_id=?", (uid,))[0]["coins"]
    await send_mascot(
        cb.message,
        key,
        f"Покупка прошла успешно! 🎉 Твой маскот теперь выглядит по-новому.\nБаланс: {coins_word(balance)}",
        after,
    )


async def show_mascot(msg: Message, u: sqlite3.Row) -> None:
    await send_mascot(
        msg,
        u["equipped"],
        f"Вот твой маскот! 🐭\nБаланс: {coins_word(u['coins'])}",
        kb_of([("🛍 Магазин", "shop")], BTN_BACK),
    )


@router.callback_query(F.data == "mascot")
async def cb_mascot(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    u = ensure_user(cb.from_user.id, cb.message.chat.id)
    await show_mascot(cb.message, u)


# ---------------------------------------------------------------- команды для тестирования (только ADMIN_IDS)
class IsAdmin(Filter):
    async def __call__(self, m: Message) -> bool:
        return m.from_user.id in ADMIN_IDS


@router.message(Command("reset"), IsAdmin())
async def adm_reset(m: Message, state: FSMContext):
    """Стереть все свои данные: бот снова видит тебя впервые."""
    await state.clear()
    uid = m.from_user.id
    ex("DELETE FROM topups WHERE goal_id IN (SELECT id FROM goals WHERE user_id=?)", (uid,))
    ex("DELETE FROM goals WHERE user_id=?", (uid,))
    ex("DELETE FROM owned WHERE user_id=?", (uid,))
    ex("DELETE FROM users WHERE user_id=?", (uid,))
    await m.answer("🧪 Данные стёрты, для бота ты снова новичок. Отправь /start")


@router.message(Command("notify"), IsAdmin())
async def adm_notify(m: Message):
    """Сразу прислать себе оба варианта ежедневного уведомления."""
    u = ensure_user(m.from_user.id, m.chat.id)
    g = active_goal(m.from_user.id)
    if not g:
        await m.answer("🧪 Сначала добавь цель (/new)")
        return
    for label, flag in (("вариант 1 — пока не получал монеты", 0), ("вариант 2 — уже получал монеты", 1)):
        fake = dict(u)
        fake["intro_seen"] = flag
        await m.answer(f"🧪 <i>{label}</i>\n\n{notification_text(fake, g)}", reply_markup=kb_notify())


@router.message(Command("notifystatus"), IsAdmin())
async def adm_notifystatus(m: Message):
    """Объясняет, придёт ли вам уведомление сегодня, а если нет — почему."""
    u = ensure_user(m.from_user.id, m.chat.id)
    g = active_goal(m.from_user.id)
    n = now()
    start = datetime.combine(n.date(), REMIND_AT, tzinfo=TZ)
    end = start + timedelta(minutes=REMIND_WINDOW_MIN)
    problems = notify_problems(u, g, n)
    blockers = [t for kind, t in problems if kind == "block"]
    timing = [t for kind, t in problems if kind == "time"]
    lines = [
        "🧪 <b>Диагностика уведомлений</b>",
        f"Время бота: {n:%d.%m %H:%M} ({TZ.key})",
        f"Уведомление в {REMIND_TIME}, окно до {end:%H:%M} (REMIND_TIME, REMIND_WINDOW_MIN в .env)",
        f"Последнее уведомление вам: {u['last_notified'] or 'ещё не было'}",
        "",
    ]
    if blockers:
        lines.append("❌ Сегодня уведомление НЕ придёт:")
        lines += [f"• {t}" for t in blockers]
    elif n >= end:
        lines.append(f"❌ {timing[0]}. Следующее будет завтра.")
    elif n < start:
        lines.append(f"✅ Всё в порядке: уведомление придёт в {REMIND_TIME} (бот должен быть запущен в это время).")
    else:
        lines.append("✅ Всё в порядке: уведомление уйдёт в течение 30 секунд.")
    await m.answer("\n".join(lines))


@router.message(Command("nextday"), IsAdmin())
async def adm_nextday(m: Message):
    """Притвориться, что наступил следующий день: сегодняшние пополнения становятся вчерашними."""
    uid = m.from_user.id
    ex("UPDATE topups SET day=date(day,'-1 day') WHERE goal_id IN (SELECT id FROM goals WHERE user_id=?)", (uid,))
    ex("UPDATE users SET last_notified=NULL WHERE user_id=?", (uid,))
    await m.answer("🧪 Перемотал на день вперёд: монеты за пополнение снова можно получить.")


@router.message(Command("givecoins"), IsAdmin())
async def adm_givecoins(m: Message, command: CommandObject):
    n = int(command.args) if command.args and command.args.lstrip("-").isdigit() else 100
    ensure_user(m.from_user.id, m.chat.id)
    ex("UPDATE users SET coins=MAX(0, coins+?) WHERE user_id=?", (n, m.from_user.id))
    await m.answer(f"🧪 Монеты изменены на {n}.")


# ---------------------------------------------------------------- всё остальное
@router.message()
async def fallback(m: Message):
    await show_menu(m, m.from_user.id, m.chat.id)


@router.callback_query()
async def stale_button(cb: CallbackQuery):
    await cb.answer("Эта кнопка уже неактуальна. Нажми /start", show_alert=True)


# ---------------------------------------------------------------- ежедневные уведомления
def notification_text(u, g: sqlite3.Row) -> str:
    if u["intro_seen"]:
        # «монетный» вариант: для тех, кто уже получал монеты
        return (
            f"Ты можешь получить ещё {coins_word(COINS_PER_TOPUP)} за следующее пополнение копилки «{html.escape(g['name'])}» 🪙\n"
            f"У тебя сейчас {coins_word(u['coins'])}."
        )
    p = plan_for(g)
    again = " ещё раз" if has_topups(g["id"]) else ""
    text = f"Привет! Готов ли ты сегодня пополнить копилку «{html.escape(g['name'])}»{again}? Это поможет накопить тебе быстрее."
    if p["days_left"] > 0 and p["daily"]:
        left = p["remaining"] - p["daily"]
        tail = f"до цели останется {money(left)}" if left > 0 else "это будет последний шаг до цели"
        text += f"\n\n📈 Если отложить сегодня {money(p['daily'])}, {tail}."
    return text


def notify_problems(u, g, n: datetime) -> list[tuple[str, str]]:
    """Почему уведомление пользователю сейчас НЕ уйдёт. Пустой список — можно отправлять.
    Тип 'time' — не подошло время (рано / окно закрыто), 'block' — всё остальное.
    Одна и та же проверка используется и в рассылке, и в диагностике /notifystatus."""
    out: list[tuple[str, str]] = []
    start = datetime.combine(n.date(), REMIND_AT, tzinfo=TZ)
    end = start + timedelta(minutes=REMIND_WINDOW_MIN)
    if u["blocked"]:
        out.append(("block", "пользователь заблокировал бота"))
    if g is None:
        out.append(("block", "нет активной цели (её создаёт /new)"))
        return out
    if n < start:
        out.append(("time", f"ещё рано: сейчас {n:%H:%M}, уведомление в {start:%H:%M}"))
    elif n >= end:
        out.append(("time", f"окно отправки закрылось в {end:%H:%M} (REMIND_WINDOW_MIN={REMIND_WINDOW_MIN})"))
    if u["last_notified"] == n.date().isoformat():
        out.append(("block", "сегодня уведомление уже отправлялось (бот шлёт не чаще раза в день); /nextday сбросит это"))
    created = datetime.fromisoformat(g["created_at"])
    if created >= start:
        out.append(("block", f"цель создана в {created:%H:%M}, то есть позже {start:%H:%M}: первое уведомление придёт завтра"))
    if user_topped_up_today(u["user_id"]):
        out.append(("block", "сегодня уже было пополнение, повторно не беспокоим"))
    return out


async def tick(bot: Bot) -> None:
    """Раз в 30 секунд. Уведомление уходит только в окне [REMIND_TIME; REMIND_TIME + REMIND_WINDOW_MIN)
    и не чаще раза в день на пользователя."""
    n = now()
    start = datetime.combine(n.date(), REMIND_AT, tzinfo=TZ)
    if not (start <= n < start + timedelta(minutes=REMIND_WINDOW_MIN)):
        return
    day = n.date().isoformat()
    users = q(
        "SELECT * FROM users u WHERE u.blocked=0 AND (u.last_notified IS NULL OR u.last_notified<>?) "
        "AND EXISTS(SELECT 1 FROM goals g WHERE g.user_id=u.user_id AND g.status='active')",
        (day,),
    )
    for u in users:
        g = active_goal(u["user_id"])
        problems = notify_problems(u, g, n)
        if problems:
            log.info("Уведомление user=%s пропущено: %s", u["user_id"], "; ".join(t for _, t in problems))
            continue
        try:
            await bot.send_message(u["chat_id"], notification_text(u, g), reply_markup=kb_notify())
            ex("UPDATE users SET last_notified=? WHERE user_id=?", (day, u["user_id"]))
            log.info("Уведомление user=%s отправлено", u["user_id"])
        except TelegramForbiddenError:  # пользователь заблокировал бота
            ex("UPDATE users SET blocked=1 WHERE user_id=?", (u["user_id"],))
        except Exception:
            log.exception("Не удалось отправить уведомление user=%s", u["user_id"])


async def scheduler(bot: Bot) -> None:
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(30)


# ---------------------------------------------------------------- запуск
async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("Задай BOT_TOKEN (см. .env.example)")
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="new", description="Добавить цель"),
            BotCommand(command="progress", description="Прогресс и расписание"),
            BotCommand(command="shop", description="Магазин для маскота"),
            BotCommand(command="mascot", description="Мой маскот"),
        ]
    )
    log.info(
        "Время бота: %s (%s). Уведомления в %s, окно %s мин. Админы: %s",
        now().strftime("%d.%m %H:%M"), TZ.key, REMIND_TIME, REMIND_WINDOW_MIN, sorted(ADMIN_IDS) or "нет",
    )
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
