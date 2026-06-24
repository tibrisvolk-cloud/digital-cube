import logging
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import sqlite3

# ---------- НАСТРОЙКИ ----------
TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_NAME = "bot_data.db"
# --------------------------------

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MENU_IMAGE = os.path.join(SCRIPT_DIR, "menu.jpg")
RIDDLES_IMAGE = os.path.join(SCRIPT_DIR, "riddles.jpg")
SHOP_IMAGE = os.path.join(SCRIPT_DIR, "shop.jpg")

# ---------- Безопасное подключение к SQLite ----------
def get_connection():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

# ---------- РАБОТА С БД ----------
def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS riddles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            answer TEXT NOT NULL,
            points_reward INTEGER DEFAULT 10,
            total_limit INTEGER,
            image TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1)""")
    try:
        c.execute("ALTER TABLE riddles ADD COLUMN total_limit INTEGER")
        c.execute("ALTER TABLE riddles ADD COLUMN image TEXT DEFAULT ''")
    except:
        pass

    c.execute("""CREATE TABLE IF NOT EXISTS user_riddle_attempts (
            user_id INTEGER,
            riddle_id INTEGER,
            first_attempt_time TIMESTAMP,
            attempts_count INTEGER DEFAULT 0,
            solved INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, riddle_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT NOT NULL,
            description TEXT,
            points_reward INTEGER DEFAULT 5,
            task_type TEXT DEFAULT 'subscription',
            delay_seconds INTEGER DEFAULT 0,
            secret_code TEXT DEFAULT '',
            total_limit INTEGER,
            is_active INTEGER DEFAULT 1)""")
    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN total_limit INTEGER")
    except:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS user_subscriptions (
            user_id INTEGER,
            subscription_id INTEGER,
            completed INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, subscription_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS shop_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price INTEGER NOT NULL,
            total_limit INTEGER,
            user_limit INTEGER,
            content TEXT DEFAULT '',
            manual_delivery INTEGER DEFAULT 0,
            image TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1)""")
    try:
        c.execute("ALTER TABLE shop_items ADD COLUMN content TEXT DEFAULT ''")
        c.execute("ALTER TABLE shop_items ADD COLUMN manual_delivery INTEGER DEFAULT 0")
        c.execute("ALTER TABLE shop_items ADD COLUMN image TEXT DEFAULT ''")
    except:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            content TEXT DEFAULT '',
            delivered INTEGER DEFAULT 0,
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    try:
        c.execute("ALTER TABLE purchases ADD COLUMN content TEXT DEFAULT ''")
        c.execute("ALTER TABLE purchases ADD COLUMN delivered INTEGER DEFAULT 0")
    except:
        pass
    conn.commit()
    conn.close()

def ensure_user(user_id, username=None):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()

def get_user_points(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

# ---------- ЗАГАДКИ (видны даже с исчерпанными попытками) ----------
def get_active_riddles_for_user(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT r.id, r.text, r.image FROM riddles r
        WHERE r.is_active = 1
          AND NOT EXISTS (
              SELECT 1 FROM user_riddle_attempts u
              WHERE u.user_id = ? AND u.riddle_id = r.id AND u.solved = 1
          )
          AND (r.total_limit IS NULL OR (
              SELECT COUNT(*) FROM user_riddle_attempts WHERE riddle_id = r.id AND solved = 1
          ) < r.total_limit)
    """, (user_id,))
    riddles = c.fetchall()
    conn.close()
    return riddles

def add_riddle(text, answer, points, total_limit=None, image=''):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO riddles (text, answer, points_reward, total_limit, image) VALUES (?, ?, ?, ?, ?)",
              (text, answer.lower(), points, total_limit, image))
    conn.commit()
    riddle_id = c.lastrowid
    conn.close()
    return riddle_id

def remove_riddle(riddle_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE riddles SET is_active = 0 WHERE id = ?", (riddle_id,))
    conn.commit()
    conn.close()

def check_riddle_answer(user_id, riddle_id, user_answer):
    now = datetime.now()
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT answer, points_reward FROM riddles WHERE id = ? AND is_active = 1", (riddle_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "riddle_not_found"
    correct_answer, reward = row[0].strip().lower(), row[1]

    # Проверка общего лимита
    c.execute("SELECT total_limit FROM riddles WHERE id = ?", (riddle_id,))
    total_limit = c.fetchone()[0]
    if total_limit is not None:
        c.execute("SELECT COUNT(*) FROM user_riddle_attempts WHERE riddle_id = ? AND solved = 1", (riddle_id,))
        if c.fetchone()[0] >= total_limit:
            conn.close()
            return "riddle_not_found"

    # Проверяем текущее состояние попыток (ДО ИНКРЕМЕНТА)
    c.execute("SELECT attempts_count, first_attempt_time, solved FROM user_riddle_attempts WHERE user_id = ? AND riddle_id = ?",
              (user_id, riddle_id))
    current = c.fetchone()
    if current:
        attempts_before, first_time_str, solved = current
        if solved:
            conn.close()
            return "already_solved"
        if attempts_before >= 3:
            first_time = datetime.fromisoformat(first_time_str) if first_time_str else None
            if first_time and (now - first_time) > timedelta(hours=24):
                # Сброс
                c.execute("UPDATE user_riddle_attempts SET attempts_count = 1, first_attempt_time = ? WHERE user_id = ? AND riddle_id = ?",
                          (now, user_id, riddle_id))
                conn.commit()
                attempts_before = 1
            else:
                conn.close()
                return "no_attempts"
    else:
        attempts_before = 0

    # Теперь инкрементируем попытки
    if attempts_before == 0:
        c.execute("INSERT INTO user_riddle_attempts (user_id, riddle_id, first_attempt_time, attempts_count, solved) VALUES (?, ?, ?, 1, 0)",
                  (user_id, riddle_id, now))
    else:
        c.execute("UPDATE user_riddle_attempts SET attempts_count = attempts_count + 1 WHERE user_id = ? AND riddle_id = ?",
                  (user_id, riddle_id))
    conn.commit()

    # Снова читаем актуальное количество попыток
    c.execute("SELECT attempts_count FROM user_riddle_attempts WHERE user_id = ? AND riddle_id = ?", (user_id, riddle_id))
    new_attempts = c.fetchone()[0]

    if user_answer.strip().lower() != correct_answer:
        if new_attempts >= 3:
            conn.close()
            return "no_attempts"
        conn.close()
        return "wrong"

    # Правильный ответ
    c.execute("UPDATE user_riddle_attempts SET solved = 1 WHERE user_id = ? AND riddle_id = ?",
              (user_id, riddle_id))
    c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (reward, user_id))
    conn.commit()
    conn.close()
    return "correct"

# ---------- ЗАДАНИЯ (с упрощённым парсингом) ----------
def get_active_subscriptions_for_user(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT s.id, s.channel_username, s.description, s.points_reward,
                        s.task_type, s.delay_seconds, s.secret_code
                 FROM subscriptions s
                 WHERE s.is_active = 1
                   AND NOT EXISTS (SELECT 1 FROM user_subscriptions us WHERE us.user_id = ? AND us.subscription_id = s.id AND us.completed = 1)
                   AND (s.total_limit IS NULL OR (SELECT COUNT(*) FROM user_subscriptions WHERE subscription_id = s.id AND completed = 1) < s.total_limit)
              """, (user_id,))
    subs = c.fetchall()
    conn.close()
    return subs

def add_subscription_generic(channel_username, description, points, task_type='subscription', delay=0, secret_code='', total_limit=None):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO subscriptions (channel_username, description, points_reward, task_type, delay_seconds, secret_code, total_limit) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (channel_username, description, points, task_type, delay, secret_code, total_limit))
    conn.commit()
    conn.close()

def remove_subscription(sub_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE subscriptions SET is_active = 0 WHERE id = ?", (sub_id,))
    conn.commit()
    conn.close()

async def verify_single_subscription(bot, user_id, sub_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT channel_username, points_reward, task_type, delay_seconds, secret_code FROM subscriptions WHERE id = ? AND is_active = 1", (sub_id,))
    sub = c.fetchone()
    if not sub:
        conn.close()
        return False, "Задание не найдено.", 0
    channel, reward, task_type, delay, secret_code = sub

    c.execute("SELECT completed FROM user_subscriptions WHERE user_id = ? AND subscription_id = ?", (user_id, sub_id))
    if c.fetchone() is not None:
        conn.close()
        return False, "Уже выполнено.", 0

    if task_type == 'subscription':
        try:
            chat_member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if chat_member.status in ['member', 'administrator', 'creator']:
                c.execute("INSERT OR IGNORE INTO user_subscriptions (user_id, subscription_id, completed) VALUES (?, ?, 1)",
                          (user_id, sub_id))
                c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (reward, user_id))
                conn.commit()
                conn.close()
                return True, f"Подписка подтверждена! Начислено {reward} баллов.", reward
            else:
                conn.close()
                return False, "Вы не подписаны на этот канал.", 0
        except Exception as e:
            conn.close()
            logger.warning(f"Ошибка проверки подписки {channel}: {e}")
            return False, "Ошибка проверки. Попробуйте позже.", 0
    elif task_type == 'external':
        c.execute("INSERT OR IGNORE INTO user_subscriptions (user_id, subscription_id, completed) VALUES (?, ?, 1)",
                  (user_id, sub_id))
        c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (reward, user_id))
        conn.commit()
        conn.close()
        return True, f"Задание выполнено!\nНачислено {reward} баллов.", reward
    else:
        conn.close()
        return False, "Введите код после перехода по ссылке.", 0

# ---------- МАГАЗИН ----------
def get_active_shop_items_for_user(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, description, price, total_limit, user_limit, content, manual_delivery, image FROM shop_items WHERE is_active = 1")
    items = c.fetchall()
    conn.close()
    return items

def purchase_item(user_id, item_id):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        c = conn.cursor()
        c.execute("SELECT name, price, total_limit, user_limit, content, manual_delivery FROM shop_items WHERE id = ? AND is_active = 1", (item_id,))
        row = c.fetchone()
        if not row:
            return False, "Товар не найден.", None, None, None, None, None
        name, price, total_limit, user_limit, content, manual = row
        c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
        user_points = c.fetchone()
        if not user_points or user_points[0] < price:
            return False, f"Недостаточно баллов. Ваш баланс: {user_points[0] if user_points else 0}.", None, None, None, None, None
        if total_limit is not None:
            c.execute("SELECT COUNT(*) FROM purchases WHERE item_id = ?", (item_id,))
            if c.fetchone()[0] >= total_limit:
                return False, "Товар закончился.", None, None, None, None, None
        if user_limit is not None:
            c.execute("SELECT COUNT(*) FROM purchases WHERE user_id = ? AND item_id = ?", (user_id, item_id))
            if c.fetchone()[0] >= user_limit:
                return False, f"Вы исчерпали свой лимит ({user_limit} шт.).", None, None, None, None, None
        c.execute("UPDATE users SET points = points - ? WHERE user_id = ?", (price, user_id))
        delivered = 0 if manual else 1
        c.execute("INSERT INTO purchases (user_id, item_id, content, delivered) VALUES (?, ?, ?, ?)",
                  (user_id, item_id, content, delivered))
        purchase_id = c.lastrowid
        conn.commit()
        return True, f"Вы купили товар! Списано {price} баллов.", purchase_id, manual, content, price, name
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка покупки: {e}")
        return False, "Ошибка при покупке.", None, None, None, None, None
    finally:
        conn.close()

def get_user_purchases(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT p.id, i.name, p.content, p.delivered, p.purchased_at
        FROM purchases p JOIN shop_items i ON p.item_id = i.id
        WHERE p.user_id = ? ORDER BY p.purchased_at DESC""", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# ---------- ОБРАБОТЧИКИ МАГАЗИНА ----------
async def shop_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    items = get_active_shop_items_for_user(user_id)
    await query.message.delete()
    if not items:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
        await context.bot.send_message(chat_id=user_id, text="📦 В магазине пока нет товаров.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    keyboard = []
    for item in items:
        item_id, name, desc, price, total_limit, user_limit, content, manual, image = item
        keyboard.append([InlineKeyboardButton(f"🎁 {name}", callback_data=f"shop_item_{item_id}")])
    keyboard.append([InlineKeyboardButton("🎒 Склад", callback_data="inventory")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
    text = "🏪 Магазин\nВыберите товар для просмотра:"
    if os.path.isfile(SHOP_IMAGE):
        with open(SHOP_IMAGE, "rb") as photo:
            await context.bot.send_photo(chat_id=user_id, photo=photo, caption=text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

async def shop_item_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    item_id = int(query.data.split("_")[-1])
    await query.message.delete()
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT name, description, price, total_limit, user_limit, content, manual_delivery, image FROM shop_items WHERE id = ? AND is_active = 1", (item_id,))
    item = c.fetchone()
    if not item:
        keyboard = [[InlineKeyboardButton("🔙 В магазин", callback_data="shop_menu")]]
        await context.bot.send_message(chat_id=user_id, text="Товар не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        conn.close()
        return
    name, desc, price, total_limit, user_limit, content, manual, image = item
    c.execute("SELECT COUNT(*) FROM purchases WHERE item_id = ?", (item_id,))
    total_sold = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM purchases WHERE user_id = ? AND item_id = ?", (user_id, item_id))
    user_sold = c.fetchone()[0]
    conn.close()
    text = f"🎁 {name}\n"
    if desc: text += f"📝 {desc}\n"
    text += f"💎 Цена: {price} бал.\n"
    if total_limit is not None: text += f"📦 Общий лимит: {total_sold}/{total_limit}\n"
    if user_limit is not None: text += f"👤 Ваш лимит: {user_sold}/{user_limit}\n"
    if manual: text += "🚚 Выдаётся администратором вручную\n"
    can_buy = True
    if total_limit is not None and total_sold >= total_limit: can_buy = False
    if user_limit is not None and user_sold >= user_limit: can_buy = False
    keyboard = []
    if can_buy:
        keyboard.append([InlineKeyboardButton("✅ Купить", callback_data=f"buy_{item_id}")])
    else:
        keyboard.append([InlineKeyboardButton("❌ Товар недоступен", callback_data="none")])
    keyboard.append([InlineKeyboardButton("🔙 К списку товаров", callback_data="shop_menu")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    if image:
        try:
            await context.bot.send_photo(chat_id=user_id, photo=image, caption=text, reply_markup=InlineKeyboardMarkup(keyboard))
        except:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    item_id = int(query.data.split("_")[-1])
    success, message, purchase_id, manual, content, price, name = purchase_item(user_id, item_id)
    if success:
        balance = get_user_points(user_id)
        buyer = update.effective_user
        buyer_name = f"@{buyer.username}" if buyer.username else f"id{buyer.id}"
        if manual:
            admin_msg = f"🔔 {buyer_name} купил «{name}» (ручная выдача). ID покупки: {purchase_id}"
        else:
            admin_msg = f"🔔 {buyer_name} купил «{name}» (авто). Сумма: {price} бал."
        try:
            await context.bot.send_message(ADMIN_ID, admin_msg)
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа: {e}")
        if manual:
            text = f"✅ {message}\n💰 Ваш новый баланс: {balance} бал.\n\nТовар будет выдан администратором в ближайшее время."
        else:
            text = f"✅ {message}\n💰 Ваш новый баланс: {balance} бал.\n\n📦 Ваш товар:\n{content}"
        keyboard = [
            [InlineKeyboardButton("🏪 В магазин", callback_data="shop_menu")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]
    else:
        text = f"❌ {message}"
        keyboard = [
            [InlineKeyboardButton("🔙 Назад к товару", callback_data=f"shop_item_{item_id}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]
    await query.message.delete()
    await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

async def inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    purchases = get_user_purchases(user_id)
    await query.message.delete()
    if not purchases:
        keyboard = [[InlineKeyboardButton("🔙 В магазин", callback_data="shop_menu")]]
        await context.bot.send_message(chat_id=user_id, text="🎒 Ваш склад пуст.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    lines = ["🎒 Ваши покупки:\n"]
    for pur in purchases[:10]:
        pid, name, content, delivered, date = pur
        status = "✅" if delivered else "⏳"
        lines.append(f"{status} {name} ({date})")
        if delivered and content:
            display_content = content if len(content) <= 200 else content[:200] + "..."
            lines.append(f"   {display_content}")
    if len(purchases) > 10:
        lines.append(f"... и ещё {len(purchases)-10}")
    keyboard = [[InlineKeyboardButton("🔙 В магазин", callback_data="shop_menu")]]
    await context.bot.send_message(chat_id=user_id, text="\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))

# ---------- ГЛАВНОЕ МЕНЮ ----------
async def send_main_menu(message, context):
    caption = "🎮 *Добро пожаловать в Digital Cube!*\n \nРешайте загадки, выполняйте задания, чтобы получить очки, за которые можно купить реальные товары в магазине!\n \nПодпишитесь на @Cube_Quest, чтобы не упустить новые активности!"
    await message.reply_text(text=caption, parse_mode="MarkdownV2", reply_markup=reply_markup)
    keyboard = [
        [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton("🧩 Загадки", callback_data="riddles_menu")],
        [InlineKeyboardButton("🔍 Задания", callback_data="check_subscriptions")],
        [InlineKeyboardButton("🏪 Магазин", callback_data="shop_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if os.path.isfile(MENU_IMAGE):
        with open(MENU_IMAGE, "rb") as photo:
            await message.reply_photo(photo=photo, caption=caption, reply_markup=reply_markup)
    else:
        await message.reply_text(text=caption, reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    await send_main_menu(update.message, context)

# ---------- ОСНОВНОЙ ОБРАБОТЧИК КНОПОК ----------
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "balance":
        points = get_user_points(user_id)
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
        await query.message.reply_text(f"💎 Ваши баллы доверия: {points}", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "riddles_menu":
        riddles = get_active_riddles_for_user(user_id)
        if not riddles:
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
            await query.message.reply_text("😕 Пока нет активных загадок.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        keyboard = []
        for rid, text_preview, image in riddles:
            short_text = (text_preview[:30] + "…") if len(text_preview) > 30 else text_preview
            keyboard.append([InlineKeyboardButton(short_text, callback_data=f"riddle_{rid}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        if os.path.isfile(RIDDLES_IMAGE):
            with open(RIDDLES_IMAGE, "rb") as photo:
                await query.message.reply_photo(photo=photo, caption="Выберите загадку:", reply_markup=reply_markup)
        else:
            await query.message.reply_text("Выберите загадку:", reply_markup=reply_markup)

    elif data.startswith("riddle_"):
        riddle_id = int(data.split("_")[1])
        # Проверка попыток перед открытием
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT solved, attempts_count, first_attempt_time FROM user_riddle_attempts WHERE user_id = ? AND riddle_id = ?", (user_id, riddle_id))
        att = c.fetchone()
        conn.close()
        if att:
            solved, attempts, first_time_str = att
            if not solved and attempts >= 3:
                first_time = datetime.fromisoformat(first_time_str) if first_time_str else None
                if first_time and (datetime.now() - first_time) < timedelta(hours=24):
                    keyboard = [[InlineKeyboardButton("🔙 К списку загадок", callback_data="riddles_menu")]]
                    await query.message.reply_text("⏳ Попытки исчерпаны. Возвращайтесь через 24 часа.", reply_markup=InlineKeyboardMarkup(keyboard))
                    return

        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT text, points_reward, image FROM riddles WHERE id = ? AND is_active = 1", (riddle_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
            await query.message.reply_text("❌ Загадка не найдена.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        context.user_data['active_riddle'] = riddle_id
        context.user_data['active_riddle_reward'] = row[1]
        keyboard = [[InlineKeyboardButton("🔙 К списку загадок", callback_data="riddles_menu")]]
        text = f"🧩 Загадка:\n\n{row[0]}\n\n✏️ Введите ответ:"
        if row[2]:
            try:
                await query.message.reply_photo(photo=row[2], caption=text, reply_markup=InlineKeyboardMarkup(keyboard))
            except:
                await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "check_subscriptions":
        subs = get_active_subscriptions_for_user(user_id)
        if not subs:
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
            await query.message.reply_text("📋 Нет активных заданий.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        keyboard = []
        for sub in subs:
            sub_id, channel, desc, reward, task_type, delay, secret_code = sub
            if task_type == 'subscription':
                url = f"https://t.me/{channel.lstrip('@')}"   # <-- Вернули кнопку-ссылку
                keyboard.append([
                    InlineKeyboardButton(f"🔗 Перейти: {channel}", url=url),
                    InlineKeyboardButton("✅ Проверить", callback_data=f"verify_sub_{sub_id}")
                ])
            elif task_type == 'external':
                keyboard.append([
                    InlineKeyboardButton("🔗 Перейти", url=channel),
                    InlineKeyboardButton("✅ Проверить", callback_data=f"verify_ext_{sub_id}")
                ])
            elif task_type == 'code':
                keyboard.append([
                    InlineKeyboardButton("🔗 Перейти", url=channel),
                    InlineKeyboardButton("🔑 Ввести код", callback_data=f"code_{sub_id}")
                ])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
        await query.message.reply_text("📌 Доступные задания:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("verify_sub_"):
        sub_id = int(data.split("_")[-1])
        success, message, reward = await verify_single_subscription(context.bot, user_id, sub_id)
        await query.message.reply_text(f"{'✅' if success else '❌'} {message}")

    elif data.startswith("ext_start_"):
        sub_id = int(data.split("_")[-1])
        context.user_data[f"ext_start_{sub_id}"] = datetime.now()
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT channel_username, delay_seconds, points_reward FROM subscriptions WHERE id = ? AND is_active = 1", (sub_id,))
        sub = c.fetchone()
        conn.close()
        if sub:
            url, delay, reward = sub
            await query.message.reply_text(f"⏱ Таймер запущен ({delay} сек). Перейдите по ссылке:\n{url}")
        else:
            await query.message.reply_text("Задание не найдено.")

    elif data.startswith("verify_ext_"):
        sub_id = int(data.split("_")[-1])
        start_time = context.user_data.get(f"ext_start_{sub_id}")
        if not start_time:
            await query.message.reply_text("Сначала нажмите кнопку с описанием задания.")
            return
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT delay_seconds, points_reward FROM subscriptions WHERE id = ? AND is_active = 1", (sub_id,))
        sub = c.fetchone()
        conn.close()
        if not sub:
            await query.message.reply_text("Задание не найдено.")
            return
        delay, reward = sub
        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed < delay:
            await query.message.reply_text(f"⏳ Осталось подождать {int(delay - elapsed)} сек.")
            return
        success, message, _ = await verify_single_subscription(context.bot, user_id, sub_id)
        if success:
            context.user_data.pop(f"ext_start_{sub_id}", None)
        await query.message.reply_text(f"{'✅' if success else '❌'} {message}")

    elif data.startswith("code_"):
        sub_id = int(data.split("_")[-1])
        context.user_data['active_code_sub'] = sub_id
        await query.message.reply_text("Введите секретный код из поста:")

    elif data == "main_menu":
        await send_main_menu(query.message, context)

# ---------- ОБРАБОТЧИК ТЕКСТА ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    if context.user_data.get('expecting_broadcast') and user.id == ADMIN_ID:
        await handle_broadcast_message(update, context)
        return

    if await try_handle_riddle_step(update, context):
        return

    if 'active_code_sub' in context.user_data:
        sub_id = context.user_data.pop('active_code_sub')
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT secret_code, points_reward FROM subscriptions WHERE id = ? AND is_active = 1", (sub_id,))
        sub = c.fetchone()
        if not sub:
            conn.close()
            await update.message.reply_text("Задание не найдено.")
            return
        secret_code, reward = sub
        if text.strip().upper() == secret_code.upper():
            c.execute("INSERT OR IGNORE INTO user_subscriptions (user_id, subscription_id, completed) VALUES (?, ?, 1)", (user.id, sub_id))
            c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (reward, user.id))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"✅ Код верный!\nНачислено {reward} баллов.")
        else:
            conn.close()
            await update.message.reply_text("❌ Неверный код.")
        return

    if 'active_riddle' in context.user_data:
        riddle_id = context.user_data['active_riddle']
        reward = context.user_data.get('active_riddle_reward', 10)

        # Дополнительная проверка попыток перед ответом
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT solved, attempts_count, first_attempt_time FROM user_riddle_attempts WHERE user_id = ? AND riddle_id = ?", (user.id, riddle_id))
        att = c.fetchone()
        conn.close()
        if att:
            solved, attempts, first_time_str = att
            if not solved and attempts >= 3:
                first_time = datetime.fromisoformat(first_time_str) if first_time_str else None
                if first_time and (datetime.now() - first_time) < timedelta(hours=24):
                    context.user_data.pop('active_riddle', None)
                    context.user_data.pop('active_riddle_reward', None)
                    await update.message.reply_text("⏳ Попытки исчерпаны. Возвращайтесь через 24 часа.")
                    return

        result = check_riddle_answer(user.id, riddle_id, text)

        if result == "riddle_not_found":
            context.user_data.pop('active_riddle', None)
            context.user_data.pop('active_riddle_reward', None)
            await update.message.reply_text("Загадка не активна или лимит исчерпан.")
        elif result == "already_solved":
            context.user_data.pop('active_riddle', None)
            context.user_data.pop('active_riddle_reward', None)
            await update.message.reply_text("🎉 Вы уже решили эту загадку!")
        elif result == "no_attempts":
            context.user_data.pop('active_riddle', None)
            context.user_data.pop('active_riddle_reward', None)
            await update.message.reply_text("⏳ Попытки исчерпаны. Возвращайтесь через 24 часа.")
        elif result == "wrong":
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT attempts_count FROM user_riddle_attempts WHERE user_id = ? AND riddle_id = ?", (user.id, riddle_id))
            attempts = c.fetchone()[0]
            conn.close()
            msg = f"❌ Неверно. Использовано попыток: {attempts}/3."
            if attempts >= 3:
                context.user_data.pop('active_riddle', None)
                context.user_data.pop('active_riddle_reward', None)
                msg += "\nВы израсходовали все попытки."
            await update.message.reply_text(msg)
        elif result == "correct":
            context.user_data.pop('active_riddle', None)
            context.user_data.pop('active_riddle_reward', None)
            await update.message.reply_text(f"🎉 Правильно!\nНачислено {reward} баллов.")
        else:
            context.user_data.pop('active_riddle', None)
            context.user_data.pop('active_riddle_reward', None)
            await update.message.reply_text("Произошла ошибка.")
        return

# ---------- ОБРАБОТЧИК ФОТО (для загадки) ----------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await try_handle_riddle_step(update, context):
        return

async def try_handle_riddle_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return False

    ud = context.user_data
    if ud.get('adding_riddle'):
        if update.message and update.message.text:
            ud['riddle_text'] = update.message.text
            ud['adding_riddle'] = False
            ud['awaiting_answer'] = True
            await update.message.reply_text("Теперь введите правильный ответ (одно слово/фраза):")
            return True
        else:
            await update.message.reply_text("Пожалуйста, введите текст загадки (не фото).")
            return True

    if ud.get('awaiting_answer'):
        if update.message and update.message.text:
            ud['riddle_answer'] = update.message.text.lower()
            ud['awaiting_answer'] = False
            ud['awaiting_points'] = True
            await update.message.reply_text("Сколько баллов начислить за правильный ответ? (целое число, по умолчанию 10)")
            return True
        else:
            await update.message.reply_text("Пожалуйста, введите ответ текстом.")
            return True

    if ud.get('awaiting_points'):
        if update.message and update.message.text:
            try:
                points = int(update.message.text)
            except ValueError:
                points = 10
            if points < 1:
                points = 10
            ud['riddle_points'] = points
            ud['awaiting_points'] = False
            # Переходим сразу к фото
            ud['awaiting_image'] = True
            await update.message.reply_text("Отправьте фото для загадки (или напишите 'нет' / 'skip' для пропуска):")
            return True
        else:
            await update.message.reply_text("Пожалуйста, введите число.")
            return True

    if ud.get('awaiting_image'):
        if update.message.photo:
            ud['riddle_image'] = update.message.photo[-1].file_id
        elif update.message.text and update.message.text.lower() in ['нет', 'skip', '-']:
            ud['riddle_image'] = ''
        else:
            await update.message.reply_text("Отправьте фото или напишите 'нет' для пропуска.")
            return True

        # Завершаем создание загадки
        text = ud.pop('riddle_text')
        answer = ud.pop('riddle_answer')
        points = ud.pop('riddle_points')
        image = ud.pop('riddle_image', '')
        ud.pop('awaiting_image', None)
        riddle_id = add_riddle(text, answer, points, image=image)
        await update.message.reply_text(f"✅ Загадка #{riddle_id} добавлена (награда {points} бал.).")
        return True

    return False

# ---------- АДМИНСКИЕ КОМАНДЫ (упрощённый парсинг) ----------
async def addriddle_start(update, context):
    if update.effective_user.id != ADMIN_ID: return
    context.user_data['adding_riddle'] = True
    await update.message.reply_text("Введите текст загадки:")

async def removeriddle(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        rid = int(context.args[0])
        remove_riddle(rid)
        await update.message.reply_text(f"Загадка #{rid} деактивирована.")
    except:
        await update.message.reply_text("Используйте: /removeriddle <id>")

async def listriddles(update, context):
    if update.effective_user.id != ADMIN_ID: return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, text, points_reward, total_limit FROM riddles WHERE is_active = 1")
    riddles = c.fetchall()
    conn.close()
    if not riddles:
        await update.message.reply_text("Нет активных загадок.")
        return
    lines = ["📃 Активные загадки:"]
    for rid, text, reward, total_l in riddles:
        limits = ""
        if total_l is not None: limits += f" общ.лимит:{total_l}"
        lines.append(f"#{rid} ({reward} бал.{limits}) – {text[:50]}...")
    await update.message.reply_text("\n".join(lines))

async def addsub(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        args = context.args
        channel = args[0]
        points = int(args[1])
        # Попытка прочитать третий аргумент как total_limit, если он число
        total_limit = None
        desc_start = 2
        if len(args) > 2 and args[2].isdigit():
            total_limit = int(args[2])
            desc_start = 3
        desc = " ".join(args[desc_start:])
        add_subscription_generic(channel, desc, points, task_type='subscription', total_limit=total_limit)
        await update.message.reply_text("Задание на подписку добавлено.")
    except:
        await update.message.reply_text("Формат: /addsub @channel баллы [лимит] описание")

async def addext(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        args = context.args
        url = args[0]
        points = int(args[1])
        total_limit = None
        delay = 0
        desc_start = 2
        # Попробуем прочитать следующие числа: общий лимит и задержку
        if len(args) > 2 and args[2].isdigit():
            total_limit = int(args[2])
            desc_start = 3
        if len(args) > 3 and args[3].isdigit():
            delay = int(args[3])
            desc_start = 4
        desc = " ".join(args[desc_start:])
        add_subscription_generic(url, desc, points, task_type='external', delay=delay, total_limit=total_limit)
        await update.message.reply_text("Внешнее задание добавлено.")
    except:
        await update.message.reply_text("Формат: /addext URL баллы [общ.лимит] [задержка_сек] описание")

async def addcode(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        args = context.args
        url = args[0]
        points = int(args[1])
        code = args[2]
        total_limit = None
        desc_start = 3
        if len(args) > 3 and args[3].isdigit():
            total_limit = int(args[3])
            desc_start = 4
        desc = " ".join(args[desc_start:])
        add_subscription_generic(url, desc, points, task_type='code', secret_code=code, total_limit=total_limit)
        await update.message.reply_text("Задание с кодом добавлено.")
    except:
        await update.message.reply_text("Формат: /addcode URL баллы код [общ.лимит] описание")

async def removesub(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        sub_id = int(context.args[0])
        remove_subscription(sub_id)
        await update.message.reply_text("Задание удалено.")
    except:
        await update.message.reply_text("Используйте: /removesub <id>")

async def listsubs(update, context):
    if update.effective_user.id != ADMIN_ID: return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, channel_username, description, points_reward, task_type, delay_seconds, secret_code, total_limit FROM subscriptions WHERE is_active = 1")
    subs = c.fetchall()
    conn.close()
    if not subs:
        await update.message.reply_text("Нет активных заданий.")
        return
    lines = ["📌 Активные задания:"]
    for sub in subs:
        sid, chan, desc, pts, ttype, delay, code, total_l = sub
        limits = ""
        if total_l is not None: limits += f" общ.лимит:{total_l}"
        extra = f" тип:{ttype}"
        if ttype == 'external': extra += f" задержка:{delay}с"
        if ttype == 'code': extra += f" код:{code}"
        lines.append(f"#{sid} {chan} — {desc} ({pts} бал.{limits}{extra})")
    await update.message.reply_text("\n".join(lines))

async def earnings(update, context):
    if update.effective_user.id != ADMIN_ID: return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT SUM(riddles.points_reward) FROM user_riddle_attempts JOIN riddles ON user_riddle_attempts.riddle_id = riddles.id WHERE solved = 1")
    riddles_sum = c.fetchone()[0] or 0
    c.execute("SELECT SUM(subscriptions.points_reward) FROM user_subscriptions JOIN subscriptions ON user_subscriptions.subscription_id = subscriptions.id WHERE completed = 1")
    tasks_sum = c.fetchone()[0] or 0
    conn.close()
    await update.message.reply_text(f"📊 Заработано на загадках: {riddles_sum} бал.\n📊 Заработано на заданиях: {tasks_sum} бал.")

async def add_item_command(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        args = context.args
        name = args[0]
        price = int(args[1])
        total_limit = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
        user_limit = int(args[3]) if len(args) > 3 and args[3].isdigit() else None
        manual = 0
        content = ""
        image = ""
        idx = 2
        if total_limit is not None: idx += 1
        if user_limit is not None: idx += 1
        while idx < len(args):
            if args[idx].startswith("manual="):
                manual = 1 if args[idx].split("=")[1] == "1" else 0
                idx += 1
            elif args[idx].startswith("content="):
                content = " ".join(args[idx:]).replace("content=", "", 1)
                break
            elif args[idx].startswith("image="):
                image = args[idx][6:]
                idx += 1
            else:
                idx += 1
    except:
        await update.message.reply_text("Формат: /additem <название> <цена> [общий лимит] [лимит на пользователя] [manual=1] [image=URL] [content=текст]")
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO shop_items (name, price, total_limit, user_limit, manual_delivery, content, image) VALUES (?,?,?,?,?,?,?)",
              (name, price, total_limit, user_limit, manual, content, image))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Товар «{name}» добавлен (manual={manual}).")

async def remove_item_command(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        item_id = int(context.args[0])
        conn = get_connection()
        c = conn.cursor()
        c.execute("UPDATE shop_items SET is_active = 0 WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"Товар #{item_id} деактивирован.")
    except:
        await update.message.reply_text("Используйте: /removeitem <id>")

async def list_items_command(update, context):
    if update.effective_user.id != ADMIN_ID: return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, price, total_limit, user_limit, manual_delivery, image FROM shop_items WHERE is_active = 1")
    items = c.fetchall()
    conn.close()
    if not items:
        await update.message.reply_text("Товаров нет.")
        return
    lines = ["📦 Товары:"]
    for i in items:
        lines.append(f"#{i[0]} {i[1]} — 💎{i[2]} | общий: {i[3] or '∞'} | на польз: {i[4] or '∞'} | manual: {i[5]}")
    await update.message.reply_text("\n".join(lines))

async def deliver_command(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        parts = context.args
        purchase_id = int(parts[0])
        extra_msg = " ".join(parts[1:]) if len(parts) > 1 else ""
    except:
        await update.message.reply_text("Используйте: /deliver <purchase_id> [сообщение]")
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE purchases SET delivered = 1 WHERE id = ?", (purchase_id,))
    if c.rowcount == 0:
        await update.message.reply_text("Покупка не найдена.")
        conn.close()
        return
    conn.commit()
    c.execute("SELECT user_id, item_id, content FROM purchases WHERE id = ?", (purchase_id,))
    user_id, item_id, content = c.fetchone()
    conn.close()
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT name, content FROM shop_items WHERE id = ?", (item_id,))
    item = c.fetchone()
    conn.close()
    if not item: return
    name, item_content = item
    notification = f"✅ Ваш заказ «{name}» выдан!\n📦 {item_content}"
    if extra_msg:
        notification += f"\n💌 Сообщение от поддержки: {extra_msg}"
    try:
        await context.bot.send_message(user_id, notification)
    except Exception as e:
        logger.warning(f"Не удалось уведомить {user_id}: {e}")
        await update.message.reply_text("Покупка отмечена, но уведомление не отправлено.")
        return
    await update.message.reply_text(f"Покупка #{purchase_id} отмечена как выданная, пользователь уведомлён.")

async def pending_orders(update, context):
    if update.effective_user.id != ADMIN_ID: return
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT p.id, p.user_id, u.username, i.name, p.purchased_at
        FROM purchases p JOIN shop_items i ON p.item_id = i.id LEFT JOIN users u ON p.user_id = u.user_id
        WHERE p.delivered = 0 ORDER BY p.purchased_at DESC""")
    orders = c.fetchall()
    conn.close()
    if not orders:
        await update.message.reply_text("Нет невыданных заказов.")
        return
    lines = ["⏳ Невыданные заказы:"]
    for pid, uid, username, name, date in orders:
        user_str = f"@{username}" if username else f"id{uid}"
        lines.append(f"#{pid} {user_str} — «{name}» ({date})")
    await update.message.reply_text("\n".join(lines))

async def broadcast(update, context):
    if update.effective_user.id != ADMIN_ID: return
    context.user_data['expecting_broadcast'] = True
    await update.message.reply_text("Пришлите сообщение для рассылки (текст, фото, что угодно). /cancel для отмены.")

async def cancel(update, context):
    context.user_data.pop('expecting_broadcast', None)
    await update.message.reply_text("Рассылка отменена.")

async def handle_broadcast_message(update, context):
    if not context.user_data or not context.user_data.get('expecting_broadcast'):
        return
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data.pop('expecting_broadcast')
    users = get_all_users()
    msg = update.message
    success = 0
    for uid in users:
        try:
            await msg.copy(chat_id=uid)
            success += 1
        except:
            pass
    await update.message.reply_text(f"Рассылка завершена: доставлено {success}/{len(users)} пользователям.")

def get_all_users():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

async def stats(update, context):
    if update.effective_user.id != ADMIN_ID: return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*), SUM(points) FROM users")
    count, total = c.fetchone()
    conn.close()
    await update.message.reply_text(f"👥 Пользователей: {count}\n💎 Всего баллов: {total or 0}")

async def users_list(update, context):
    if update.effective_user.id != ADMIN_ID: return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, points FROM users ORDER BY points DESC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("В базе ещё нет участников.")
        return
    text = "👥 Список участников:\n\n"
    for uid, username, pts in rows:
        name = f"@{username}" if username else f"id{uid}"
        text += f"{name} → {pts} бал.\n"
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000])
    else:
        await update.message.reply_text(text)

def resolve_user_id(identifier: str):
    identifier = identifier.strip()
    try:
        uid = int(identifier)
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (uid,))
        exists = c.fetchone()
        conn.close()
        if exists:
            return uid, None
        else:
            return None, f"Пользователь с ID {uid} не найден в базе."
    except ValueError:
        pass
    username = identifier.lstrip('@').lower()
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], None
    else:
        return None, f"Пользователь с юзернеймом @{username} не найден в базе."

async def add_points_command(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        identifier = context.args[0]
        amount = int(context.args[1])
        reason = " ".join(context.args[2:]) if len(context.args) > 2 else ""
    except:
        await update.message.reply_text("Используйте: /addpoints <ID или @username> <количество> [текст]")
        return
    target_id, error = resolve_user_id(identifier)
    if error:
        await update.message.reply_text(error)
        return
    ensure_user(target_id)
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (amount, target_id))
    conn.commit()
    conn.close()
    msg = f"🔔 Вам начислено +{amount} баллов."
    if reason: msg += f"\n{reason}"
    try:
        await context.bot.send_message(chat_id=target_id, text=msg)
    except Exception as e:
        logger.warning(f"Не удалось уведомить {target_id}: {e}")
    await update.message.reply_text(f"✅ Пользователю {identifier} начислено {amount} баллов и отправлено уведомление.")

async def remove_points_command(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        identifier = context.args[0]
        amount = int(context.args[1])
        reason = " ".join(context.args[2:]) if len(context.args) > 2 else ""
    except:
        await update.message.reply_text("Используйте: /removepoints <ID или @username> <количество> [текст]")
        return
    target_id, error = resolve_user_id(identifier)
    if error:
        await update.message.reply_text(error)
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET points = points - ? WHERE user_id = ?", (amount, target_id))
    conn.commit()
    conn.close()
    msg = f"🔔 С вашего счёта снято {amount} баллов."
    if reason: msg += f"\n{reason}"
    try:
        await context.bot.send_message(chat_id=target_id, text=msg)
    except Exception as e:
        logger.warning(f"Не удалось уведомить {target_id}: {e}")
    await update.message.reply_text(f"✅ У пользователя {identifier} снято {amount} баллов и отправлено уведомление.")

async def set_points_command(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        identifier = context.args[0]
        amount = int(context.args[1])
        reason = " ".join(context.args[2:]) if len(context.args) > 2 else ""
    except:
        await update.message.reply_text("Используйте: /setpoints <ID или @username> <количество> [текст]")
        return
    target_id, error = resolve_user_id(identifier)
    if error:
        await update.message.reply_text(error)
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET points = ? WHERE user_id = ?", (amount, target_id))
    conn.commit()
    conn.close()
    msg = f"🔔 Ваш баланс изменён: теперь {amount} баллов."
    if reason: msg += f"\nПричина: {reason}"
    try:
        await context.bot.send_message(chat_id=target_id, text=msg)
    except Exception as e:
        logger.warning(f"Не удалось уведомить {target_id}: {e}")
    await update.message.reply_text(f"✅ Для пользователя {identifier} установлено {amount} баллов и отправлено уведомление.")

# ---------- ЗАПУСК ----------
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_click, pattern="^(?!shop_item_|buy_|shop_menu$|inventory$|verify_sub_|verify_ext_|ext_start_|code_).*$"))
    app.add_handler(CallbackQueryHandler(shop_menu, pattern="^shop_menu$"))
    app.add_handler(CallbackQueryHandler(shop_item_card, pattern="^shop_item_"))
    app.add_handler(CallbackQueryHandler(buy_item, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(inventory, pattern="^inventory$"))
    app.add_handler(CallbackQueryHandler(button_click, pattern="^verify_sub_"))
    app.add_handler(CallbackQueryHandler(button_click, pattern="^verify_ext_"))
    app.add_handler(CallbackQueryHandler(button_click, pattern="^ext_start_"))
    app.add_handler(CallbackQueryHandler(button_click, pattern="^code_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_handler(CommandHandler("addriddle", addriddle_start))
    app.add_handler(CommandHandler("removeriddle", removeriddle))
    app.add_handler(CommandHandler("listriddles", listriddles))
    app.add_handler(CommandHandler("addsub", addsub))
    app.add_handler(CommandHandler("addext", addext))
    app.add_handler(CommandHandler("addcode", addcode))
    app.add_handler(CommandHandler("removesub", removesub))
    app.add_handler(CommandHandler("listsubs", listsubs))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.TEXT, handle_broadcast_message))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("additem", add_item_command))
    app.add_handler(CommandHandler("removeitem", remove_item_command))
    app.add_handler(CommandHandler("listitems", list_items_command))
    app.add_handler(CommandHandler("deliver", deliver_command))
    app.add_handler(CommandHandler("pending", pending_orders))
    app.add_handler(CommandHandler("addpoints", add_points_command))
    app.add_handler(CommandHandler("removepoints", remove_points_command))
    app.add_handler(CommandHandler("setpoints", set_points_command))
    app.add_handler(CommandHandler("earnings", earnings))

    print("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
