#!/usr/bin/env python3
"""
Telegram reseller bot for the doomsbot.store Developer API.

- Customers have their own balance inside THIS bot (SQLite).
- Admin tops up customer balances (/addbalance) after receiving payment.
- On purchase: customer balance is debited -> supplier API is called with
  your API key -> codes are delivered. Any failure refunds the customer.
"""
import asyncio
import html
import logging
import math
import os
import sqlite3
import time

import httpx
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_KEY = os.environ["DOOMS_API_KEY"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
MARKUP = float(os.getenv("MARKUP_PERCENT", "20"))
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "the admin")
DB_PATH = os.getenv("DB_PATH", "shop.db")
BASE = "https://doomsbot.store/api/public/v1"
PAGE_SIZE = 8

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reseller")

# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT,
    balance INTEGER NOT NULL DEFAULT 0,   -- cents
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    qty INTEGER NOT NULL,
    charged INTEGER NOT NULL,             -- cents charged to customer
    cost INTEGER NOT NULL,                -- cents supplier price
    status TEXT NOT NULL,                 -- pending|done|refunded|review|settled
    api_order_id TEXT,
    codes TEXT,
    created INTEGER NOT NULL
);
"""
)
db.commit()


def money(cents: int) -> str:
    return f"${cents / 100:.2f}"


def to_cents(x) -> int:
    return int(round(float(x) * 100))


def sell_cents(supplier_price) -> int:
    return math.ceil(to_cents(supplier_price) * (1 + MARKUP / 100))


def ensure_user(u) -> sqlite3.Row:
    db.execute(
        "INSERT OR IGNORE INTO users(id, username, created) VALUES (?,?,?)",
        (u.id, u.username, int(time.time())),
    )
    db.commit()
    return db.execute("SELECT * FROM users WHERE id=?", (u.id,)).fetchone()


def balance_of(uid: int) -> int:
    row = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    return row["balance"] if row else 0


def esc(s) -> str:
    return html.escape(str(s or ""))


# --------------------------------------------------------------- supplier API
_calls: list[float] = []
_rl = asyncio.Lock()


async def api(method: str, path: str, **kw):
    """Calls the supplier API while staying under 20 req/min."""
    async with _rl:
        now = time.time()
        while _calls and now - _calls[0] > 60:
            _calls.pop(0)
        if len(_calls) >= 18:
            await asyncio.sleep(max(0.0, 60 - (now - _calls[0])) + 0.2)
        _calls.append(time.time())
    headers = {"X-API-Key": API_KEY, **kw.pop("headers", {})}
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.request(method, BASE + path, headers=headers, **kw)
    try:
        body = r.json()
    except Exception:
        body = {}
    return r.status_code, body


_cache = {"t": 0.0, "data": []}


async def get_products(force: bool = False) -> list[dict]:
    if not force and _cache["data"] and time.time() - _cache["t"] < 45:
        return _cache["data"]
    try:
        status, body = await api("GET", "/products")
    except httpx.HTTPError:
        return _cache["data"]
    if status == 200:
        _cache["data"] = body.get("products", [])
        _cache["t"] = time.time()
    return _cache["data"]


async def notify_admins(bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception:
            log.exception("could not notify admin %s", aid)


# ------------------------------------------------------------------ purchase
def refund(order_id: int, status: str = "refunded"):
    o = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o or o["status"] in ("refunded", "done", "settled"):
        return False
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (o["charged"], o["user_id"]))
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    db.commit()
    return True


async def purchase(bot, uid: int, pid: str, qty: int):
    """Returns (result, payload). result in: ok, stock, funds, refunded, review"""
    products = await get_products(force=True)
    p = next((x for x in products if x["id"] == pid), None)
    if not p or not p.get("in_stock"):
        return "stock", None
    if isinstance(p.get("stock"), int) and p["stock"] < qty:
        return "stock", None

    charged = sell_cents(p["price"]) * qty
    cost = to_cents(p["price"]) * qty

    cur = db.execute(
        "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (charged, uid, charged)
    )
    if cur.rowcount == 0:
        db.rollback()
        return "funds", None
    cur = db.execute(
        "INSERT INTO orders(user_id,product_id,product_name,qty,charged,cost,status,created)"
        " VALUES (?,?,?,?,?,?,'pending',?)",
        (uid, pid, p["name"], qty, charged, cost, int(time.time())),
    )
    oid = cur.lastrowid
    db.commit()

    idem = f"tgbot-order-{oid}"  # stable key => safe retries, no double charge
    status, body = None, {}
    for _ in range(3):
        try:
            status, body = await api(
                "POST",
                "/purchase",
                headers={"Idempotency-Key": idem},
                json={"product_id": pid, "quantity": qty},
            )
        except httpx.HTTPError:
            status, body = None, {}
            await asyncio.sleep(3)
            continue
        if status == 429:
            await asyncio.sleep(10)
            continue
        break

    if status == 200 and body.get("success"):
        codes = body.get("codes", [])
        db.execute(
            "UPDATE orders SET status='done', api_order_id=?, codes=? WHERE id=?",
            (str(body.get("order_id", "")), "\n".join(codes), oid),
        )
        db.commit()
        return "ok", {
            "order_id": oid,
            "codes": codes,
            "instructions": body.get("instructions") or p.get("instructions"),
            "name": p["name"],
        }

    if status in (401, 402, 404, 409, 429, 500):
        refund(oid)
        if status in (401, 402):
            reason = "invalid API key" if status == 401 else "supplier balance too low"
            await notify_admins(bot, f"⚠️ Purchase failed: <b>{reason}</b> (order #{oid}). Customer refunded.")
        return "refunded", status

    # Unknown outcome (network failure / odd response): hold funds, ask admin
    db.execute("UPDATE orders SET status='review' WHERE id=?", (oid,))
    db.commit()
    await notify_admins(
        bot,
        f"🔎 Order #{oid} needs review (status={status}). Idempotency key: <code>{idem}</code>\n"
        f"Check supplier orders, then /refund {oid} or /settle {oid}.",
    )
    return "review", oid


# ------------------------------------------------------------------ keyboards
def main_menu() -> Markup:
    return Markup(
        [
            [Btn("🛒 Shop", callback_data="shop:0")],
            [Btn("💰 Balance", callback_data="bal"), Btn("📦 My orders", callback_data="orders")],
            [Btn("➕ Add funds", callback_data="dep")],
        ]
    )


def back_btn(to="menu", label="⬅️ Back") -> list:
    return [Btn(label, callback_data=to)]


async def show(update: Update, text: str, kb: Markup):
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ------------------------------------------------------------------- handlers
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await show(update, "👋 <b>Welcome!</b>\nChoose an option:", main_menu())


async def balance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await show(update, f"💰 Your balance: <b>{money(balance_of(update.effective_user.id))}</b>", main_menu())


async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    data = q.data
    parts = data.split(":")
    kind = parts[0]

    if kind == "menu":
        return await show(update, "🏠 <b>Main menu</b>", main_menu())

    if kind == "bal":
        return await show(
            update,
            f"💰 Your balance: <b>{money(balance_of(uid))}</b>",
            Markup([[Btn("➕ Add funds", callback_data="dep")], back_btn()]),
        )

    if kind == "dep":
        return await show(
            update,
            "➕ <b>Add funds</b>\n\n"
            f"Contact {esc(ADMIN_CONTACT)} to pay, and send your ID:\n<code>{uid}</code>\n"
            "Your balance is updated once payment is confirmed.",
            Markup([back_btn()]),
        )

    if kind == "orders":
        rows = db.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 8", (uid,)
        ).fetchall()
        if not rows:
            text = "📦 No orders yet."
        else:
            text = "📦 <b>Your last orders</b>\n\n"
            for o in rows:
                text += f"#{o['id']} • {esc(o['product_name'])} ×{o['qty']} • {money(o['charged'])} • {o['status']}\n"
                if o["codes"]:
                    text += f"<code>{esc(o['codes'])}</code>\n"
                text += "\n"
        return await show(update, text[:4000], Markup([back_btn()]))

    if kind == "shop":
        page = int(parts[1])
        products = await get_products()
        if not products:
            return await show(update, "😕 Could not load products. Try again shortly.", Markup([back_btn()]))
        chunk = products[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        rows = []
        for p in chunk:
            tag = money(sell_cents(p["price"])) if p.get("in_stock") else "out of stock"
            rows.append([Btn(f"{p['name']} — {tag}", callback_data=f"p:{p['id']}")])
        nav = []
        if page > 0:
            nav.append(Btn("◀️", callback_data=f"shop:{page - 1}"))
        if (page + 1) * PAGE_SIZE < len(products):
            nav.append(Btn("▶️", callback_data=f"shop:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append(back_btn())
        return await show(update, "🛒 <b>Shop</b>\nPick a product:", Markup(rows))

    if kind == "p":
        pid = parts[1]
        p = next((x for x in await get_products() if x["id"] == pid), None)
        if not p:
            return await show(update, "Product not found.", Markup([back_btn("shop:0")]))
        text = (
            f"<b>{esc(p['name'])}</b>\n{esc(p.get('description'))}\n\n"
            f"Price: <b>{money(sell_cents(p['price']))}</b> each\n"
            f"Stock: {esc(p.get('stock'))}"
        )
        rows = []
        if p.get("in_stock"):
            stock = p.get("stock") if isinstance(p.get("stock"), int) else 50
            opts = [n for n in (1, 2, 5, 10) if n <= min(stock, 50)]
            rows.append([Btn(f"Buy ×{n}", callback_data=f"c:{pid}:{n}") for n in opts])
        rows.append(back_btn("shop:0"))
        return await show(update, text, Markup(rows))

    if kind == "c":  # confirm
        pid, qty = parts[1], int(parts[2])
        p = next((x for x in await get_products() if x["id"] == pid), None)
        if not p:
            return await show(update, "Product not found.", Markup([back_btn("shop:0")]))
        total = sell_cents(p["price"]) * qty
        text = (
            f"🧾 <b>Confirm order</b>\n{esc(p['name'])} ×{qty}\n"
            f"Total: <b>{money(total)}</b>\nYour balance: {money(balance_of(uid))}"
        )
        return await show(
            update,
            text,
            Markup([[Btn("✅ Confirm", callback_data=f"ok:{pid}:{qty}"), Btn("❌ Cancel", callback_data=f"p:{pid}")]]),
        )

    if kind == "ok":  # execute
        pid, qty = parts[1], int(parts[2])
        await q.answer("Processing…")
        await q.edit_message_text("⏳ Processing your order…")
        result, payload = await purchase(ctx.bot, uid, pid, qty)
        kb = Markup([[Btn("🛒 Shop", callback_data="shop:0"), Btn("🏠 Menu", callback_data="menu")]])
        if result == "ok":
            text = f"✅ <b>Order #{payload['order_id']} complete</b>\n{esc(payload['name'])}\n\n"
            text += "\n".join(f"<code>{esc(c)}</code>" for c in payload["codes"])
            if payload.get("instructions"):
                text += f"\n\n📝 {esc(payload['instructions'])}"
            text += f"\n\nBalance: {money(balance_of(uid))}"
        elif result == "funds":
            text = f"❌ Not enough balance. You have {money(balance_of(uid))}."
        elif result == "stock":
            text = "❌ Out of stock or not enough stock."
        elif result == "refunded":
            text = "⚠️ Order could not be completed. You were refunded. Please try again later."
        else:
            text = (
                f"🔎 Order #{payload} is being verified. Your funds are held safely; "
                f"{esc(ADMIN_CONTACT)} will resolve it shortly."
            )
        return await q.edit_message_text(text[:4000], parse_mode="HTML", reply_markup=kb)


# --------------------------------------------------------------- admin commands
def admin_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return
        return await fn(update, ctx)

    return wrapper


@admin_only
async def addbalance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        uid, amount = int(ctx.args[0]), to_cents(ctx.args[1])
    except (IndexError, ValueError):
        return await update.message.reply_text("Usage: /addbalance <user_id> <amount>")
    cur = db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
    db.commit()
    if cur.rowcount == 0:
        return await update.message.reply_text("User not found (they must /start the bot first).")
    await update.message.reply_text(f"Done. New balance: {money(balance_of(uid))}")
    try:
        await ctx.bot.send_message(uid, f"✅ {money(amount)} added. Balance: {money(balance_of(uid))}")
    except Exception:
        pass


@admin_only
async def supplier(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    status, body = await api("GET", "/balance")
    owed = db.execute("SELECT COALESCE(SUM(balance),0) s FROM users").fetchone()["s"]
    await update.message.reply_text(
        f"Supplier wallet: {body.get('balance')} {body.get('currency', '')} (HTTP {status})\n"
        f"Customer balances held: {money(owed)}"
    )


@admin_only
async def review(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.execute("SELECT * FROM orders WHERE status='review'").fetchall()
    if not rows:
        return await update.message.reply_text("No orders awaiting review.")
    await update.message.reply_text(
        "\n".join(f"#{o['id']} user {o['user_id']} {o['product_name']} ×{o['qty']} {money(o['charged'])}" for o in rows)
    )


@admin_only
async def refund_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        oid = int(ctx.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Usage: /refund <order_id>")
    ok = refund(oid)
    await update.message.reply_text("Refunded." if ok else "Nothing to refund.")


@admin_only
async def settle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mark a 'review' order as fulfilled manually (no refund)."""
    try:
        oid = int(ctx.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Usage: /settle <order_id>")
    db.execute("UPDATE orders SET status='settled' WHERE id=? AND status='review'", (oid,))
    db.commit()
    await update.message.reply_text("Marked as settled.")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("handler error", exc_info=ctx.error)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("addbalance", addbalance))
    app.add_handler(CommandHandler("supplier", supplier))
    app.add_handler(CommandHandler("review", review))
    app.add_handler(CommandHandler("refund", refund_cmd))
    app.add_handler(CommandHandler("settle", settle))
    app.add_handler(CallbackQueryHandler(router))
    app.add_error_handler(on_error)
    log.info("Bot running…")
    app.run_polling()


if __name__ == "__main__":
    main()
