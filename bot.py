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
import re
import sqlite3
import time

import httpx
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

# Crypto (NOWPayments) - automatic deposits
NOW_KEY = os.getenv("NOWPAYMENTS_API_KEY", "")
CRYPTO_CURRENCY = os.getenv("CRYPTO_CURRENCY", "usdttrc20")
CRYPTO_LABEL = os.getenv("CRYPTO_LABEL", "USDT (TRC20)")
MIN_DEPOSIT_USD = float(os.getenv("MIN_DEPOSIT_USD", "5"))

# UPI - manual verification
UPI_ID = os.getenv("UPI_ID", "")
UPI_NAME = os.getenv("UPI_NAME", "")
UPI_RATE = float(os.getenv("UPI_RATE", "90"))  # INR per 1 USD credited
UPI_MIN_INR = int(os.getenv("UPI_MIN_INR", "300"))

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
CREATE TABLE IF NOT EXISTS deposits(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    method TEXT NOT NULL,                 -- crypto|upi
    usd INTEGER NOT NULL,                 -- cents to credit
    inr INTEGER,
    status TEXT NOT NULL,                 -- awaiting|pending|waiting|partial|credited|rejected|expired|failed|refunded
    ref TEXT,                             -- UTR (upi) or provider payment_id (crypto)
    pay_address TEXT,
    pay_amount TEXT,
    created INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS dep_ref ON deposits(method, ref) WHERE ref IS NOT NULL;
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


# ------------------------------------------------------------------ deposits
NOW_BASE = "https://api.nowpayments.io/v1"


async def nowp(method: str, path: str, **kw):
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.request(method, NOW_BASE + path, headers={"x-api-key": NOW_KEY}, **kw)
    try:
        body = r.json()
    except Exception:
        body = {}
    return r.status_code, body


def credit_deposit(dep_id: int):
    """Atomically credit a deposit exactly once. Returns the row, or None if already processed."""
    d = db.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    if not d:
        return None
    cur = db.execute(
        "UPDATE deposits SET status='credited' WHERE id=? AND status IN ('waiting','partial','pending')",
        (dep_id,),
    )
    if cur.rowcount == 0:
        db.rollback()
        return None
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (d["usd"], d["user_id"]))
    db.commit()
    return d


async def sync_crypto(bot, d) -> str:
    """Ask the provider for the payment status and act on it. Returns our resulting status."""
    status, body = await nowp("GET", f"/payment/{d['ref']}")
    if status != 200:
        return d["status"]
    ps = body.get("payment_status", "")
    if ps == "finished":
        dep = credit_deposit(d["id"])
        if dep:
            await bot.send_message(
                dep["user_id"],
                f"✅ Deposit received: <b>{money(dep['usd'])}</b> added.\n"
                f"Balance: {money(balance_of(dep['user_id']))}",
                parse_mode="HTML",
            )
        return "credited"
    if ps == "partially_paid" and d["status"] == "waiting":
        db.execute("UPDATE deposits SET status='partial' WHERE id=?", (d["id"],))
        db.commit()
        await bot.send_message(
            d["user_id"],
            f"⚠️ Deposit #{d['id']} was underpaid. Send the remaining amount to the same address "
            f"or contact {esc(ADMIN_CONTACT)}.",
            parse_mode="HTML",
        )
        await notify_admins(bot, f"⚠️ Crypto deposit #{d['id']} (user {d['user_id']}) is <b>partially paid</b>.")
        return "partial"
    if ps in ("failed", "expired", "refunded") and d["status"] in ("waiting", "partial"):
        db.execute("UPDATE deposits SET status=? WHERE id=?", (ps, d["id"]))
        db.commit()
        await bot.send_message(d["user_id"], f"❌ Deposit #{d['id']} {ps}. If you paid, contact {esc(ADMIN_CONTACT)}.", parse_mode="HTML")
        return ps
    return d["status"]


async def poll_crypto(ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.execute(
        "SELECT * FROM deposits WHERE method='crypto' AND status IN ('waiting','partial') AND created>?",
        (int(time.time()) - 3 * 86400,),
    ).fetchall()
    for d in rows:
        try:
            await sync_crypto(ctx.bot, d)
        except Exception:
            log.exception("poll error for deposit %s", d["id"])
        await asyncio.sleep(0.5)


async def start_crypto(uid: int, usd: float):
    """Create a crypto payment. Returns (text, keyboard)."""
    back = Markup([back_btn("dep")])
    if usd < MIN_DEPOSIT_USD:
        return f"❌ Minimum deposit is ${MIN_DEPOSIT_USD:g}.", back
    if usd > 5000:
        return "❌ Amount too large.", back
    open_cnt = db.execute(
        "SELECT COUNT(*) c FROM deposits WHERE user_id=? AND method='crypto' AND status='waiting' AND created>?",
        (uid, int(time.time()) - 3600),
    ).fetchone()["c"]
    if open_cnt >= 3:
        return "⏳ You already have open payments. Complete or wait for them to expire.", back
    cur = db.execute(
        "INSERT INTO deposits(user_id,method,usd,status,created) VALUES (?,?,?,?,?)",
        (uid, "crypto", to_cents(usd), "awaiting", int(time.time())),
    )
    did = cur.lastrowid
    db.commit()
    try:
        status, body = await nowp(
            "POST",
            "/payment",
            json={
                "price_amount": usd,
                "price_currency": "usd",
                "pay_currency": CRYPTO_CURRENCY,
                "order_id": f"dep-{did}",
            },
        )
    except httpx.HTTPError:
        status, body = None, {}
    if status not in (200, 201) or not body.get("pay_address"):
        log.error("NOWPayments create failed: %s %s", status, body)
        db.execute("UPDATE deposits SET status='failed' WHERE id=?", (did,))
        db.commit()
        return "❌ Could not create the payment right now. Please try again later.", back
    db.execute(
        "UPDATE deposits SET status='waiting', ref=?, pay_address=?, pay_amount=? WHERE id=?",
        (str(body["payment_id"]), body["pay_address"], str(body["pay_amount"]), did),
    )
    db.commit()
    text = (
        f"🪙 <b>Deposit #{did}</b> — {money(to_cents(usd))}\n\n"
        f"Send exactly:\n<code>{esc(body['pay_amount'])}</code> {esc(CRYPTO_LABEL)}\n\n"
        f"To address:\n<code>{esc(body['pay_address'])}</code>\n"
    )
    if body.get("payin_extra_id"):
        text += f"\nMemo/Tag: <code>{esc(body['payin_extra_id'])}</code>\n"
    text += (
        f"\n⚠️ Use the <b>{esc(CRYPTO_LABEL)}</b> network only and send the exact amount. "
        "Your balance is credited automatically after confirmation."
    )
    kb = Markup([[Btn("🔄 Check status", callback_data=f"dck:{did}")], back_btn("menu", "🏠 Menu")])
    return text, kb


def upi_admin_text(d) -> str:
    return (
        f"🇮🇳 <b>UPI deposit #{d['id']}</b>\n"
        f"User: <code>{d['user_id']}</code>\n"
        f"Amount: ₹{d['inr']} → credit {money(d['usd'])}\n"
        f"UTR: <code>{esc(d['ref'])}</code>"
    )


def upi_admin_kb(did: int) -> Markup:
    return Markup([[Btn("✅ Approve", callback_data=f"da:{did}"), Btn("❌ Reject", callback_data=f"dr:{did}")]])


def start_upi(uid: int, inr: int, ctx: ContextTypes.DEFAULT_TYPE):
    """Create a UPI deposit awaiting the customer's UTR. Returns (text, keyboard)."""
    back = Markup([back_btn("dep")])
    if inr < UPI_MIN_INR:
        return f"❌ Minimum UPI deposit is ₹{UPI_MIN_INR}.", back
    if inr > 200000:
        return "❌ Amount too large.", back
    usd_cents = int(inr / UPI_RATE * 100)
    cur = db.execute(
        "INSERT INTO deposits(user_id,method,usd,inr,status,created) VALUES (?,?,?,?,?,?)",
        (uid, "upi", usd_cents, inr, "awaiting", int(time.time())),
    )
    did = cur.lastrowid
    db.commit()
    ctx.user_data["await"] = ("utr", did)
    text = (
        f"🇮🇳 <b>UPI deposit #{did}</b>\n\n"
        f"Pay exactly <b>₹{inr}</b> to:\nUPI ID: <code>{esc(UPI_ID)}</code>\n"
        + (f"Name: {esc(UPI_NAME)}\n" if UPI_NAME else "")
        + f"\nYou will receive: <b>{money(usd_cents)}</b> (₹{UPI_RATE:g} = $1)\n\n"
        "After paying, <b>send the UTR / transaction reference number</b> here as a message "
        "(you can also send a screenshot). An admin will verify and credit your balance."
    )
    return text, Markup([back_btn("dep", "❌ Cancel")])


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
        ctx.user_data.pop("await", None)
        rows = []
        if NOW_KEY:
            rows.append([Btn("🪙 Crypto — automatic", callback_data="dc")])
        if UPI_ID:
            rows.append([Btn("🇮🇳 UPI — manual verification", callback_data="du")])
        rows.append(back_btn())
        text = "➕ <b>Add funds</b>\nChoose a payment method:"
        if len(rows) == 1:
            text = f"➕ <b>Add funds</b>\n\nContact {esc(ADMIN_CONTACT)} to pay. Your ID: <code>{uid}</code>"
        return await show(update, text, Markup(rows))

    if kind == "dc":  # crypto amount menu
        ctx.user_data.pop("await", None)
        rows = [
            [Btn(f"${n}", callback_data=f"dca:{n}") for n in (5, 10, 20, 50) if n >= MIN_DEPOSIT_USD],
            [Btn("✏️ Custom amount", callback_data="dcc")],
            back_btn("dep"),
        ]
        return await show(
            update, f"🪙 <b>Crypto deposit</b>\nPaid in {esc(CRYPTO_LABEL)}. Choose an amount:", Markup(rows)
        )

    if kind == "dcc":
        ctx.user_data["await"] = ("crypto_amt",)
        return await show(
            update, f"✏️ Send the amount in USD you want to add (min ${MIN_DEPOSIT_USD:g}).", Markup([back_btn("dc")])
        )

    if kind == "dca":
        await q.answer()
        await q.edit_message_text("⏳ Creating payment…")
        text, kb = await start_crypto(uid, float(parts[1]))
        return await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    if kind == "dck":  # check crypto status
        d = db.execute("SELECT * FROM deposits WHERE id=? AND user_id=?", (int(parts[1]), uid)).fetchone()
        if not d:
            return await q.answer("Not found", show_alert=True)
        if d["status"] in ("waiting", "partial"):
            try:
                await sync_crypto(ctx.bot, d)
            except Exception:
                log.exception("manual sync failed")
            d = db.execute("SELECT * FROM deposits WHERE id=?", (d["id"],)).fetchone()
        labels = {
            "waiting": "⏳ Waiting for payment / confirmations",
            "partial": "⚠️ Underpaid — send the remainder or contact admin",
            "credited": "✅ Credited",
            "expired": "⌛ Expired",
            "failed": "❌ Failed",
            "refunded": "↩️ Refunded",
        }
        await q.answer(labels.get(d["status"], d["status"]), show_alert=True)
        return

    if kind == "du":  # UPI amount menu
        ctx.user_data["await"] = ("upi_amt",)
        amounts = [a for a in (300, 500, 1000, 2000) if a >= UPI_MIN_INR]
        rows = [[Btn(f"₹{a}", callback_data=f"dua:{a}") for a in amounts], back_btn("dep")]
        return await show(
            update,
            f"🇮🇳 <b>UPI deposit</b>\nRate: ₹{UPI_RATE:g} = $1 • Min ₹{UPI_MIN_INR}\n"
            "Pick an amount or type your own amount in ₹:",
            Markup(rows),
        )

    if kind == "dua":
        text, kb = start_upi(uid, int(parts[1]), ctx)
        return await show(update, text, kb)

    if kind in ("da", "dr"):  # admin approve / reject UPI
        if uid not in ADMIN_IDS:
            return await q.answer("Not allowed", show_alert=True)
        did = int(parts[1])
        d = db.execute("SELECT * FROM deposits WHERE id=? AND method='upi'", (did,)).fetchone()
        if not d:
            return await q.answer("Not found", show_alert=True)
        if kind == "da":
            if not credit_deposit(did):
                return await q.answer("Already processed", show_alert=True)
            note = f"✅ Approved by {uid}"
            user_msg = (
                f"✅ UPI payment verified. <b>{money(d['usd'])}</b> added.\n"
                f"Balance: {money(balance_of(d['user_id']))}"
            )
        else:
            cur = db.execute("UPDATE deposits SET status='rejected' WHERE id=? AND status='pending'", (did,))
            db.commit()
            if cur.rowcount == 0:
                return await q.answer("Already processed", show_alert=True)
            note = f"❌ Rejected by {uid}"
            user_msg = f"❌ Your UPI deposit #{did} could not be verified. Contact {esc(ADMIN_CONTACT)}."
        try:
            await ctx.bot.send_message(d["user_id"], user_msg, parse_mode="HTML")
        except Exception:
            pass
        await q.answer("Done")
        return await q.edit_message_text(upi_admin_text(d) + f"\n\n{note}", parse_mode="HTML")

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
async def catalog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    products = await get_products(force=True)
    if not products:
        return await update.message.reply_text("Could not load products (check API key / try again).")
    lines = []
    for i, p in enumerate(products, 1):
        stock = p.get("stock")
        flag = "✅" if p.get("in_stock") else "❌"
        lines.append(
            f"{i}. {flag} <b>{esc(p['name'])}</b>\n"
            f"   cost ${float(p['price']):.2f} → sell {money(sell_cents(p['price']))} | stock {esc(stock)}\n"
            f"   id: <code>{esc(p['id'])}</code>"
        )
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n\n"
    if chunk:
        await update.message.reply_text(chunk, parse_mode="HTML")


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


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = ctx.user_data.get("await")
    if not st:
        return
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    txt = (update.message.text or "").strip()

    if st[0] == "crypto_amt":
        try:
            usd = float(txt.replace("$", "").replace(",", ""))
        except ValueError:
            return await update.message.reply_text("Please send a number, e.g. 15")
        ctx.user_data.pop("await", None)
        msg = await update.message.reply_text("⏳ Creating payment…")
        text, kb = await start_crypto(uid, usd)
        return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)

    if st[0] == "upi_amt":
        try:
            inr = int(float(txt.replace("₹", "").replace(",", "")))
        except ValueError:
            return await update.message.reply_text("Please send a number, e.g. 500")
        text, kb = start_upi(uid, inr, ctx)
        return await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

    if st[0] == "utr":
        did = st[1]
        utr = re.sub(r"\s+", "", txt).upper()
        if not re.fullmatch(r"[A-Z0-9]{10,25}", utr):
            return await update.message.reply_text("That doesn't look like a valid UTR. Send the 12-digit reference number.")
        try:
            cur = db.execute(
                "UPDATE deposits SET ref=?, status='pending' WHERE id=? AND user_id=? AND status='awaiting'",
                (utr, did, uid),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return await update.message.reply_text("⚠️ This UTR was already submitted.")
        if cur.rowcount == 0:
            ctx.user_data.pop("await", None)
            return await update.message.reply_text("This deposit is no longer open. Start again from Add funds.")
        ctx.user_data.pop("await", None)
        d = db.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        await update.message.reply_text(
            f"✅ Deposit #{did} submitted for verification. You'll be notified once it's approved."
        )
        for aid in ADMIN_IDS:
            try:
                await ctx.bot.send_message(aid, upi_admin_text(d), parse_mode="HTML", reply_markup=upi_admin_kb(did))
            except Exception:
                log.exception("could not notify admin %s", aid)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    d = db.execute(
        "SELECT * FROM deposits WHERE user_id=? AND method='upi' AND status IN ('awaiting','pending') "
        "ORDER BY id DESC LIMIT 1",
        (uid,),
    ).fetchone()
    if not d:
        return
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(aid, f"📎 Screenshot for UPI deposit #{d['id']} from user <code>{uid}</code>", parse_mode="HTML")
            await update.message.copy(aid)
        except Exception:
            log.exception("could not send screenshot to %s", aid)
    extra = "" if d["status"] == "pending" else " Please also send the UTR number as text."
    await update.message.reply_text("📎 Screenshot sent to the admin." + extra)


@admin_only
async def deposits_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.execute("SELECT * FROM deposits WHERE method='upi' AND status='pending'").fetchall()
    if not rows:
        return await update.message.reply_text("No pending UPI deposits.")
    for d in rows:
        await update.message.reply_text(upi_admin_text(d), parse_mode="HTML", reply_markup=upi_admin_kb(d["id"]))


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("handler error", exc_info=ctx.error)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("addbalance", addbalance))
    app.add_handler(CommandHandler("supplier", supplier))
    app.add_handler(CommandHandler("catalog", catalog))
    app.add_handler(CommandHandler("review", review))
    app.add_handler(CommandHandler("refund", refund_cmd))
    app.add_handler(CommandHandler("settle", settle))
    app.add_handler(CommandHandler("deposits", deposits_cmd))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    if NOW_KEY:
        app.job_queue.run_repeating(poll_crypto, interval=45, first=20)
    app.add_error_handler(on_error)
    log.info("Bot running…")
    app.run_polling()


if __name__ == "__main__":
    main()
