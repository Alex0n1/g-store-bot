import asyncio
import html
import logging
import os
import time

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = 8328202124  # Единственный администратор
ADMIN_IDS = {ADMIN_ID}
admin_states = {}
DB_PATH = os.getenv("DB_PATH", "shop.db")
RESERVE_MINUTES = int(os.getenv("RESERVE_MINUTES", "30"))

REGIONS = {"any": "🎲 Случайный", "RU": "🇷🇺 Россия", "US": "🇺🇸 США", "DE": "🇩🇪 Германия"}
DEFAULT_PRICES = {1: 100, 3: 249, 5: 459, 10: 750}
router = Router()
db: aiosqlite.Connection
db_lock = asyncio.Lock()


async def setting(key: str, default: str) -> str:
    cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cur.fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str):
    await db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
    )
    await db.commit()


async def price_for(qty: int) -> int:
    value = await setting(f"price_{qty}", str(DEFAULT_PRICES[qty]))
    return int(value)


async def prices_text() -> str:
    parts = []
    for qty in DEFAULT_PRICES:
        parts.append(f"x{qty} — {await price_for(qty)} ₽")
    return " · ".join(parts)


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript("""
    CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        region TEXT NOT NULL,
        data TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'free',
        order_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        region TEXT NOT NULL,
        qty INTEGER NOT NULL,
        price INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    await db.commit()


async def release_expired():
    cutoff = int(time.time()) - RESERVE_MINUTES * 60
    await db.execute(
        "UPDATE accounts SET status='free', order_id=NULL WHERE status='reserved' "
        "AND order_id IN (SELECT id FROM orders WHERE status IN ('pending','contacted') AND created_at<?)",
        (cutoff,)
    )
    await db.execute(
        "UPDATE orders SET status='expired' WHERE status IN ('pending','contacted') AND created_at<?",
        (cutoff,)
    )


async def stock_counts():
    async with db_lock:
        await release_expired()
        cur = await db.execute("SELECT region,COUNT(*) FROM accounts WHERE status='free' GROUP BY region")
        rows = dict(await cur.fetchall())
        await db.commit()
    result = {r: rows.get(r, 0) for r in ("RU", "US", "DE")}
    result["any"] = sum(result.values())
    return result


def main_menu(is_admin=False):
    b = ReplyKeyboardBuilder()
    b.button(text="👤 Аккаунт")
    b.button(text="🛍 Магазин")
    b.button(text="ℹ️ Помощь")
    if is_admin:
        b.button(text="⚙️ Админ-панель")
    b.adjust(2, 1, 1)
    return b.as_markup(resize_keyboard=True)


def inline_regions(stock):
    b = InlineKeyboardBuilder()
    for code, title in REGIONS.items():
        b.button(text=f"{title} · {stock[code]} шт.", callback_data=f"region:{code}")
    b.adjust(1)
    return b.as_markup()


async def inline_quantities(region, available):
    b = InlineKeyboardBuilder()
    for qty in DEFAULT_PRICES:
        if qty <= available:
            b.button(text=f"x{qty} — {await price_for(qty)} ₽", callback_data=f"qty:{region}:{qty}")
    b.button(text="⬅️ Назад", callback_data="shop")
    b.adjust(2, 2, 1)
    return b.as_markup()


def is_admin(user_id):
    return user_id in ADMIN_IDS


@router.message(CommandStart())
async def start(m: Message):
    welcome = await setting("welcome", "Добро пожаловать в магазин аккаунтов! Выберите нужный раздел ниже.")
    await m.answer(
        f"✨ <b>G STORE</b>\n\n{html.escape(welcome)}\n\n"
        f"<b>Прайс:</b> {await prices_text()}",
        reply_markup=main_menu(is_admin(m.from_user.id))
    )


@router.message(F.text == "🛍 Магазин")
@router.callback_query(F.data == "shop")
async def shop(event):
    stock = await stock_counts()
    text = "🛍 <b>Магазин</b>\n\nВыберите товар:"
    if isinstance(event, Message):
        await event.answer(text, reply_markup=service_keyboard(stock))
    else:
        await event.message.edit_text(text, reply_markup=service_keyboard(stock))
        await event.answer()


def service_keyboard(stock):
    b = InlineKeyboardBuilder()
    b.button(text=f"🌐 Сервис G · в наличии {stock['any']} шт.", callback_data="service:g")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "service:g")
async def service_g(cb: CallbackQuery):
    stock = await stock_counts()
    await cb.message.edit_text(
        "🌐 <b>Сервис G</b>\n\nВыберите регион аккаунтов:",
        reply_markup=inline_regions(stock)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("region:"))
async def choose_region(cb: CallbackQuery):
    region = cb.data.split(":")[1]
    if region not in REGIONS:
        return await cb.answer("Неизвестный регион", show_alert=True)
    stock = await stock_counts()
    if stock[region] <= 0:
        return await cb.answer("Нет аккаунтов в наличии для этого выбора", show_alert=True)
    await cb.message.edit_text(
        f"🌍 Регион: <b>{REGIONS[region]}</b>\n\nВыберите количество:",
        reply_markup=await inline_quantities(region, stock[region])
    )
    await cb.answer()


@router.callback_query(F.data.startswith("qty:"))
async def choose_quantity(cb: CallbackQuery):
    _, region, qty_raw = cb.data.split(":")
    if region not in REGIONS or not qty_raw.isdigit():
        return await cb.answer("Некорректный выбор", show_alert=True)
    qty = int(qty_raw)
    if qty not in DEFAULT_PRICES:
        return await cb.answer("Некорректное количество", show_alert=True)

    async with db_lock:
        await release_expired()
        # Один активный заказ на пользователя: предыдущую бронь освобождаем.
        cur = await db.execute(
            "SELECT id FROM orders WHERE user_id=? AND status IN ('pending','contacted')",
            (cb.from_user.id,)
        )
        old_ids = [r[0] for r in await cur.fetchall()]
        for old_id in old_ids:
            await db.execute("UPDATE accounts SET status='free',order_id=NULL WHERE order_id=? AND status='reserved'", (old_id,))
        await db.execute(
            "UPDATE orders SET status='cancelled' WHERE user_id=? AND status IN ('pending','contacted')",
            (cb.from_user.id,)
        )
        if region == "any":
            cur = await db.execute(
                "SELECT id FROM accounts WHERE status='free' ORDER BY RANDOM() LIMIT ?", (qty,)
            )
        else:
            cur = await db.execute(
                "SELECT id FROM accounts WHERE status='free' AND region=? ORDER BY RANDOM() LIMIT ?",
                (region, qty)
            )
        ids = [r[0] for r in await cur.fetchall()]
        if len(ids) != qty:
            await db.commit()
            return await cb.answer("Остаток изменился. Выберите другое количество.", show_alert=True)
        price = await price_for(qty)
        cur = await db.execute(
            "INSERT INTO orders(user_id,username,region,qty,price,status,created_at) "
            "VALUES(?,?,?,?,?,'contacted',?)",
            (cb.from_user.id, cb.from_user.username, region, qty, price, int(time.time()))
        )
        order_id = cur.lastrowid
        await db.executemany(
            "UPDATE accounts SET status='reserved',order_id=? WHERE id=?",
            [(order_id, account_id) for account_id in ids]
        )
        await db.commit()

    contact = await setting("contact", "@PierceCo")
    await cb.message.edit_text(
        f"🧾 <b>Заявка №{order_id} создана</b>\n\n"
        f"Товар: Сервис G\nРегион: {REGIONS[region]}\n"
        f"Количество: x{qty}\nСумма: <b>{price} ₽</b>\n\n"
        f"💬 Для оплаты напишите сюда: {html.escape(contact)}\n\n"
        f"Бронь действует {RESERVE_MINUTES} минут. После согласования администратор выдаст заказ.",
        reply_markup=order_keyboard(order_id)
    )
    await cb.answer("Заявка создана")


def order_keyboard(order_id):
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отменить заявку", callback_data=f"cancel:{order_id}")
    b.button(text="🏠 В меню", callback_data="home")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "home")
async def home(cb: CallbackQuery):
    await cb.message.delete()
    await cb.message.answer("Главное меню:", reply_markup=main_menu(is_admin(cb.from_user.id)))
    await cb.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_order(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    async with db_lock:
        cur = await db.execute(
            "UPDATE orders SET status='cancelled' WHERE id=? AND user_id=? AND status='contacted'",
            (order_id, cb.from_user.id)
        )
        if cur.rowcount:
            await db.execute(
                "UPDATE accounts SET status='free',order_id=NULL WHERE order_id=? AND status='reserved'",
                (order_id,)
            )
        await db.commit()
    if cur.rowcount:
        await cb.message.edit_text("Заявка отменена. Чтобы начать заново, нажмите /start.")
    else:
        await cb.answer("Эту заявку уже нельзя отменить", show_alert=True)
        return
    await cb.answer()


@router.message(F.text == "👤 Аккаунт")
async def account(m: Message):
    cur = await db.execute(
        "SELECT id,qty,price,status,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5",
        (m.from_user.id,)
    )
    rows = await cur.fetchall()
    if not rows:
        return await m.answer("👤 <b>Ваш аккаунт</b>\n\nУ вас пока нет заказов.")
    labels = {
        "contacted": "Ожидает связи с администратором", "paid": "Оплачен",
        "delivered": "Выдан", "cancelled": "Отменён", "expired": "Бронь истекла",
        "rejected": "Отклонён", "pending": "Ожидает оплаты"
    }
    lines = [f"№{i} · x{q} · {p} ₽ · {labels.get(s,s)}" for i,q,p,s,_ in rows]
    await m.answer("👤 <b>Последние заказы</b>\n\n" + "\n".join(lines))


@router.message(F.text == "ℹ️ Помощь")
async def help_message(m: Message):
    contact = await setting("contact", "@PierceCo")
    await m.answer(
        "ℹ️ <b>Помощь</b>\n\nВыберите «Магазин», затем товар, регион и количество. "
        f"Для оплаты напишите администратору: {html.escape(contact)}"
    )


def admin_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📦 Склад", callback_data="adm:stock")
    b.button(text="➕ Добавить аккаунты", callback_data="adm:add")
    b.button(text="💰 Изменить цены", callback_data="adm:prices")
    b.button(text="💬 Контакт оплаты", callback_data="adm:contact")
    b.button(text="📝 Приветствие", callback_data="adm:welcome")
    b.button(text="🧾 Заказы", callback_data="adm:orders")
    b.button(text="📊 Статистика", callback_data="adm:stats")
    b.button(text="🛠 Инструкция", callback_data="adm:guide")
    b.adjust(2, 2, 2, 2)
    return b.as_markup()


def admin_regions_keyboard():
    b = InlineKeyboardBuilder()
    for code, title in (("RU", "🇷🇺 Россия"), ("US", "🇺🇸 США"), ("DE", "🇩🇪 Германия")):
        b.button(text=title, callback_data=f"admregion:{code}")
    b.adjust(1)
    return b.as_markup()


def admin_prices_keyboard():
    b = InlineKeyboardBuilder()
    for qty in DEFAULT_PRICES:
        b.button(text=f"Набор x{qty}", callback_data=f"admprice:{qty}")
    b.adjust(2, 2)
    return b.as_markup()


@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("⚙️ <b>Панель администратора</b>\nВыберите раздел:", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("adm:"))
async def admin_actions(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    action = cb.data.split(":")[1]
    if action == "add":
        await cb.message.edit_text("➕ <b>Добавление аккаунтов</b>\\nВыберите регион:", reply_markup=admin_regions_keyboard())
    elif action == "prices":
        await cb.message.edit_text("💰 <b>Изменение цен</b>\\nВыберите набор:", reply_markup=admin_prices_keyboard())
    elif action == "contact":
        admin_states[cb.from_user.id] = ("contact", None)
        await cb.message.answer("Отправьте новый контакт для оплаты, например @PierceCo")
    elif action == "welcome":
        admin_states[cb.from_user.id] = ("welcome", None)
        await cb.message.answer("Отправьте новый текст приветствия.")
    elif action == "orders":
        cur = await db.execute("SELECT id,user_id,region,qty,price,status FROM orders ORDER BY id DESC LIMIT 10")
        rows = await cur.fetchall()
        if not rows:
            await cb.message.edit_text("🧾 Заказов пока нет.", reply_markup=admin_keyboard())
        else:
            await cb.message.edit_text("🧾 <b>Последние заявки</b>\\nНажмите на заявку для обработки:", reply_markup=orders_keyboard(rows))
    elif action == "stock":
        cur = await db.execute("SELECT region,status,COUNT(*) FROM accounts GROUP BY region,status")
        rows = await cur.fetchall()
        body = "\n".join(f"{REGIONS.get(r,r)} — {s}: {n}" for r,s,n in rows) or "Склад пуст"
        await cb.message.edit_text("📦 <b>Склад</b>\n\n" + body, reply_markup=admin_keyboard())
    elif action == "stats":
        cur = await db.execute("SELECT COUNT(*),COALESCE(SUM(price),0) FROM orders WHERE status IN ('paid','delivered')")
        count,total = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM orders")
        all_orders = (await cur.fetchone())[0]
        await cb.message.edit_text(
            f"📊 <b>Статистика</b>\n\nВсего заявок: {all_orders}\n"
            f"Завершённых заказов: {count}\nСумма завершённых заказов: {total} ₽",
            reply_markup=admin_keyboard()
        )
    else:
        guide = (
            "🛠 <b>Команды администратора</b>\n\n"
            "/add RU — добавить аккаунты (следующие строки: логин:пароль)\n"
            "/stock — остатки\n"
            "/setprice 1 120 — изменить цену x1\n"
            "/setcontact @PierceCo — изменить контакт для оплаты\n"
            "/setwelcome Текст — изменить приветствие\n"
            "/approve ID — выдать заказ\n"
            "/reject ID — отменить заказ и вернуть аккаунты на склад\n"
            "/orders — последние заявки"
        )
        await cb.message.edit_text(guide, reply_markup=admin_keyboard())
    await cb.answer()



def orders_keyboard(rows):
    b = InlineKeyboardBuilder()
    for order_id, user_id, region, qty, price, status in rows:
        b.button(text=f"№{order_id} · x{qty} · {price}₽ · {status}", callback_data=f"admorder:{order_id}")
    b.button(text="⬅️ В админ-панель", callback_data="adm:back")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "adm:back")
async def admin_back(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("⚙️ <b>Панель администратора</b>", reply_markup=admin_keyboard())
    await cb.answer()


@router.callback_query(F.data.startswith("admregion:"))
async def admin_add_region(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    region = cb.data.split(":")[1]
    if region not in ("RU", "US", "DE"):
        return await cb.answer("Неверный регион", show_alert=True)
    admin_states[cb.from_user.id] = ("add", region)
    await cb.message.answer(
        f"Отправьте аккаунты для региона {REGIONS[region]}, каждый с новой строки.\\n"
        "Формат: логин:пароль"
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admprice:"))
async def admin_pick_price(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    qty = int(cb.data.split(":")[1])
    if qty not in DEFAULT_PRICES:
        return await cb.answer("Неверный набор", show_alert=True)
    admin_states[cb.from_user.id] = ("price", qty)
    await cb.message.answer(f"Введите новую цену в рублях для набора x{qty}. Например: 120")
    await cb.answer()


@router.callback_query(F.data.startswith("admorder:"))
async def admin_order_detail(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    order_id = int(cb.data.split(":")[1])
    cur = await db.execute(
        "SELECT user_id,region,qty,price,status FROM orders WHERE id=?", (order_id,)
    )
    row = await cur.fetchone()
    if not row:
        return await cb.answer("Заявка не найдена", show_alert=True)
    user_id, region, qty, price, status = row
    b = InlineKeyboardBuilder()
    if status in ("contacted", "pending", "review"):
        b.button(text="✅ Подтвердить и выдать", callback_data=f"admapprove:{order_id}")
        b.button(text="❌ Отклонить", callback_data=f"admreject:{order_id}")
    b.button(text="⬅️ К заказам", callback_data="adm:orders")
    b.adjust(1)
    await cb.message.edit_text(
        f"🧾 <b>Заявка №{order_id}</b>\\nПокупатель ID: <code>{user_id}</code>\\n"
        f"Регион: {REGIONS.get(region, region)}\\nКоличество: x{qty}\\n"
        f"Сумма: {price} ₽\\nСтатус: {status}",
        reply_markup=b.as_markup()
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admapprove:"))
async def admin_approve_button(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await finish_order(cb.message, int(cb.data.split(":")[1]), True)
    await cb.answer()


@router.callback_query(F.data.startswith("admreject:"))
async def admin_reject_button(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await finish_order(cb.message, int(cb.data.split(":")[1]), False)
    await cb.answer()


@router.message(F.text)
async def admin_text_input(m: Message):
    if not is_admin(m.from_user.id) or m.from_user.id not in admin_states:
        return
    action, value = admin_states.pop(m.from_user.id)
    text = (m.text or "").strip()
    if action == "add":
        items = [line.strip() for line in text.splitlines() if line.strip()]
        if not items:
            return await m.answer("Список пуст. Откройте админ-панель и повторите.")
        async with db_lock:
            await db.executemany("INSERT INTO accounts(region,data) VALUES(?,?)", [(value, item) for item in items])
            await db.commit()
        await m.answer(f"✅ Добавлено {len(items)} аккаунтов в регион {REGIONS[value]}.", reply_markup=main_menu(True))
    elif action == "price":
        if not text.isdigit() or not 1 <= int(text) <= 1_000_000:
            return await m.answer("Цена должна быть целым числом от 1 до 1000000. Повторите через админ-панель.")
        await set_setting(f"price_{value}", text)
        await m.answer(f"✅ Цена набора x{value} изменена на {text} ₽.", reply_markup=main_menu(True))
    elif action == "contact":
        if len(text) > 100:
            return await m.answer("Слишком длинный контакт. Повторите через админ-панель.")
        await set_setting("contact", text)
        await m.answer(f"✅ Контакт оплаты изменён: {html.escape(text)}", reply_markup=main_menu(True))
    elif action == "welcome":
        if len(text) > 700:
            return await m.answer("Текст слишком длинный (максимум 700 символов). Повторите через админ-панель.")
        await set_setting("welcome", text)
        await m.answer("✅ Приветствие обновлено.", reply_markup=main_menu(True))


@router.message(Command("add"))
async def add_accounts(m: Message):
    if not is_admin(m.from_user.id):
        return
    lines = (m.text or "").splitlines()
    parts = lines[0].split()
    region = parts[1].upper() if len(parts) > 1 else ""
    if region not in ("RU", "US", "DE"):
        return await m.answer("Формат:\n/add RU\nлогин:пароль\nлогин:пароль")
    items = [line.strip() for line in lines[1:] if line.strip()]
    if not items:
        return await m.answer("Добавьте аккаунты со следующих строк, по одному на строку.")
    async with db_lock:
        await db.executemany("INSERT INTO accounts(region,data) VALUES(?,?)", [(region,x) for x in items])
        await db.commit()
    await m.answer(f"✅ Добавлено аккаунтов: {len(items)} ({REGIONS[region]}).")


@router.message(Command("setprice"))
async def set_price(m: Message):
    if not is_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        return await m.answer("Формат: /setprice 1 120")
    qty, price = int(args[1]), int(args[2])
    if qty not in DEFAULT_PRICES or price < 1 or price > 1_000_000:
        return await m.answer("Количество должно быть 1, 3, 5 или 10; цена — положительное число.")
    await set_setting(f"price_{qty}", str(price))
    await m.answer(f"✅ Новая цена x{qty}: {price} ₽")


@router.message(Command("setcontact"))
async def set_contact(m: Message):
    if not is_admin(m.from_user.id):
        return
    value = (m.text or "").partition(" ")[2].strip()
    if not value:
        return await m.answer("Формат: /setcontact @PierceCo")
    await set_setting("contact", value)
    await m.answer(f"✅ Контакт для оплаты изменён: {html.escape(value)}")


@router.message(Command("setwelcome"))
async def set_welcome(m: Message):
    if not is_admin(m.from_user.id):
        return
    value = (m.text or "").partition(" ")[2].strip()
    if not value:
        return await m.answer("Напишите текст после команды: /setwelcome Добро пожаловать!")
    await set_setting("welcome", value)
    await m.answer("✅ Приветствие обновлено.")


@router.message(Command("stock"))
async def stock_command(m: Message):
    if not is_admin(m.from_user.id):
        return
    cur = await db.execute("SELECT region,status,COUNT(*) FROM accounts GROUP BY region,status")
    rows = await cur.fetchall()
    await m.answer("📦 <b>Склад</b>\n" + ("\n".join(
        f"{REGIONS.get(r,r)} — {s}: {n}" for r,s,n in rows) or "Пусто"))


@router.message(Command("orders"))
async def orders_command(m: Message):
    if not is_admin(m.from_user.id):
        return
    cur = await db.execute(
        "SELECT id,user_id,region,qty,price,status FROM orders ORDER BY id DESC LIMIT 15"
    )
    rows = await cur.fetchall()
    if not rows:
        return await m.answer("Заказов пока нет.")
    lines = [f"№{i} · user {u} · {REGIONS.get(r,r)} · x{q} · {p} ₽ · {s}" for i,u,r,q,p,s in rows]
    await m.answer("🧾 <b>Последние заявки</b>\n\n" + "\n".join(lines))


async def finish_order(m: Message, order_id: int, approved: bool):
    async with db_lock:
        cur = await db.execute("SELECT user_id,status FROM orders WHERE id=?", (order_id,))
        row = await cur.fetchone()
        if not row or row[1] not in ("contacted", "pending", "review"):
            await db.commit()
            return await m.answer("Заявка не найдена или уже обработана.")
        user_id = row[0]
        if approved:
            cur = await db.execute("SELECT data FROM accounts WHERE order_id=? AND status='reserved'", (order_id,))
            accounts = [x[0] for x in await cur.fetchall()]
            await db.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
            await db.execute("UPDATE accounts SET status='sold' WHERE order_id=?", (order_id,))
        else:
            accounts = []
            await db.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
            await db.execute("UPDATE accounts SET status='free',order_id=NULL WHERE order_id=? AND status='reserved'", (order_id,))
        await db.commit()
    try:
        if approved:
            await m.bot.send_message(
                user_id, f"✅ <b>Заказ №{order_id} выдан</b>\n\n" +
                "\n".join(f"<code>{html.escape(x)}</code>" for x in accounts)
            )
        else:
            await m.bot.send_message(user_id, f"Заявка №{order_id} отменена. Если вопрос не решён, напишите администратору.")
    except Exception:
        await m.answer("Статус обновлён, но написать покупателю не удалось.")
        return
    await m.answer("✅ Заказ выдан." if approved else "Заявка отклонена, аккаунты возвращены на склад.")


@router.message(Command("approve"))
async def approve(m: Message):
    if not is_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        return await m.answer("Формат: /approve ID")
    await finish_order(m, int(args[1]), True)


@router.message(Command("reject"))
async def reject(m: Message):
    if not is_admin(m.from_user.id):
        return
    args = (m.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        return await m.answer("Формат: /reject ID")
    await finish_order(m, int(args[1]), False)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("Заполните BOT_TOKEN в .env")
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
