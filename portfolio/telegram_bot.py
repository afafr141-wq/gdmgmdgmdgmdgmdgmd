"""
Telegram bot — Arabic inline-keyboard interface for the MEXC Rebalancer.

Commands
--------
/start  /menu  — main menu
/done         — finalise bot creation wizard (manual allocation mode)
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

log = logging.getLogger("telegram_bot")

# ── Auth ───────────────────────────────────────────────────────────────────────
def _allowed(update: Update) -> bool:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        return True
    uid = str(update.effective_user.id) if update.effective_user else ""
    return uid == chat_id

async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text("⛔ غير مصرح.")
    elif update.callback_query:
        await update.callback_query.answer("⛔ غير مصرح.", show_alert=True)

# ── Injected functions ─────────────────────────────────────────────────────────
_start_fn:            Callable = lambda pid: None
_stop_fn:             Callable = lambda pid: None
_rebalance_fn:        Callable = lambda pid: []
_list_portfolios:     Callable = lambda: []
_is_running_fn:       Callable = lambda pid: False
_get_portfolio_fn:    Callable = lambda pid: None
_save_portfolio_fn:   Callable = lambda name, cfg: None
_update_portfolio_fn: Callable  = lambda pid, cfg: None
_delete_portfolio_fn: Callable  = lambda pid: None
_buy_fn:              Callable = lambda symbol, usdt: {}
_sell_fn:             Callable = lambda symbol, amount: {}
_get_balances_fn:     Callable = lambda: {}

# SuperTrend injected functions
_st_start_fn:    Callable = lambda bid: None
_st_stop_fn:     Callable = lambda bid: None
_st_is_running:  Callable = lambda bid: False
_st_create_fn:   Callable = lambda name, cfg: 0
_st_get_fn:      Callable = lambda bid: None
_st_list_fn:     Callable = lambda: []
_st_update_fn:   Callable = lambda bid, cfg: None
_st_delete_fn:   Callable = lambda bid: None
_st_signals_fn:  Callable = lambda bid, limit=10: []
_st_loop_info_fn: Callable = lambda bid: None

# ── Keyboards ──────────────────────────────────────────────────────────────────
def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ إنشاء بوت",    callback_data="action:create_bot"),
            InlineKeyboardButton("📋 المحافظ",       callback_data="action:portfolios"),
        ],
        [
            InlineKeyboardButton("💰 الرصيد العام",  callback_data="action:balance_all"),
        ],
    ])

def _kb_back(target: str = "action:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=target)]])

def _kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="action:menu")]])

def _kb_portfolios(portfolios: list) -> InlineKeyboardMarkup:
    rows = []
    for p in portfolios:
        pid  = p["id"]
        name = p.get("name", f"محفظة {pid}")
        icon = "🟢" if p.get("running") else "⚫"
        rows.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"portfolio:{pid}")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="action:menu")])
    return InlineKeyboardMarkup(rows)

def _kb_portfolio_detail(pid: int, running: bool) -> InlineKeyboardMarkup:
    if running:
        service_btn = InlineKeyboardButton("🔴 بيع + وقف الخدمة", callback_data=f"paction:sell_stop:{pid}")
    else:
        service_btn = InlineKeyboardButton("🟢 شراء + بدء الخدمة", callback_data=f"paction:buy_start:{pid}")
    return InlineKeyboardMarkup([
        [
            service_btn,
            InlineKeyboardButton("🔄 إعادة توازن", callback_data=f"paction:rebalance:{pid}"),
        ],
        [
            InlineKeyboardButton("🟢 شراء",        callback_data=f"paction:buy:{pid}"),
            InlineKeyboardButton("🔴 بيع",          callback_data=f"paction:sell:{pid}"),
        ],
        [
            InlineKeyboardButton("🗑️ حذف عملة",    callback_data=f"paction:remove:{pid}"),
            InlineKeyboardButton("🔁 استبدال عملة", callback_data=f"paction:replace:{pid}"),
        ],
        [
            InlineKeyboardButton("💼 رصيد المحفظة", callback_data=f"paction:balance:{pid}"),
            InlineKeyboardButton("⚙️ الإعدادات",    callback_data=f"psettings:menu:{pid}"),
        ],
        [InlineKeyboardButton("🔙 رجوع",            callback_data="portfolio:home")],
    ])


def _kb_portfolio_settings(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ تغيير الاسم",    callback_data=f"psettings:rename:{pid}"),
            InlineKeyboardButton("💵 تغيير الميزانية", callback_data=f"psettings:budget:{pid}"),
        ],
        [
            InlineKeyboardButton("📊 تغيير الانحراف",  callback_data=f"psettings:deviation:{pid}"),
            InlineKeyboardButton("🔄 وضع التوازن",     callback_data=f"psettings:mode:{pid}"),
        ],
        [
            InlineKeyboardButton("📋 عرض الإعدادات",   callback_data=f"psettings:view:{pid}"),
            InlineKeyboardButton("📤 تصدير الإعدادات", callback_data=f"psettings:export:{pid}"),
        ],
        [InlineKeyboardButton("🗑️ حذف المحفظة",       callback_data=f"psettings:delete:{pid}")],
        [InlineKeyboardButton("🔙 رجوع",               callback_data=f"portfolio:{pid}")],
    ])

# ── Wizard keyboards ───────────────────────────────────────────────────────────
def _kb_alloc_mode() -> InlineKeyboardMarkup:
    """Step: choose allocation mode after entering symbols."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚖️ متساوي تلقائي", callback_data="wizard:alloc:equal"),
            InlineKeyboardButton("✏️ يدوي",           callback_data="wizard:alloc:manual"),
        ],
        [InlineKeyboardButton("❌ إلغاء", callback_data="action:menu")],
    ])

def _kb_deviation() -> InlineKeyboardMarkup:
    """Step: choose rebalance deviation threshold."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1%",  callback_data="wizard:dev:1"),
            InlineKeyboardButton("3%",  callback_data="wizard:dev:3"),
            InlineKeyboardButton("5%",  callback_data="wizard:dev:5"),
            InlineKeyboardButton("10%", callback_data="wizard:dev:10"),
        ],
        [InlineKeyboardButton("🔢 مخصص", callback_data="wizard:dev:custom")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="action:menu")],
    ])

def _kb_balance_mode() -> InlineKeyboardMarkup:
    """Step: choose how much capital to use."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💯 كل الرصيد",   callback_data="wizard:bal:all"),
            InlineKeyboardButton("💵 مبلغ محدد",   callback_data="wizard:bal:custom"),
        ],
        [InlineKeyboardButton("❌ إلغاء", callback_data="action:menu")],
    ])

# ── SuperTrend keyboards ───────────────────────────────────────────────────────
def _kb_st_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إنشاء بوت ST+UT", callback_data="staction:create")],
        [InlineKeyboardButton("📋 بوتات ST+UT",      callback_data="staction:list")],
        [InlineKeyboardButton("🔙 رجوع",              callback_data="action:menu")],
    ])

def _kb_st_interval() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1m",  callback_data="stwizard:interval:1m"),
            InlineKeyboardButton("5m",  callback_data="stwizard:interval:5m"),
            InlineKeyboardButton("15m", callback_data="stwizard:interval:15m"),
            InlineKeyboardButton("30m", callback_data="stwizard:interval:30m"),
        ],
        [
            InlineKeyboardButton("1h",  callback_data="stwizard:interval:1h"),
            InlineKeyboardButton("4h",  callback_data="stwizard:interval:4h"),
            InlineKeyboardButton("8h",  callback_data="stwizard:interval:8h"),
            InlineKeyboardButton("1d",  callback_data="stwizard:interval:1d"),
        ],
        [InlineKeyboardButton("❌ إلغاء", callback_data="staction:list")],
    ])

def _kb_st_capital() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("25 USDT",  callback_data="stwizard:capital:25"),
            InlineKeyboardButton("50 USDT",  callback_data="stwizard:capital:50"),
            InlineKeyboardButton("100 USDT", callback_data="stwizard:capital:100"),
        ],
        [
            InlineKeyboardButton("200 USDT", callback_data="stwizard:capital:200"),
            InlineKeyboardButton("500 USDT", callback_data="stwizard:capital:500"),
            InlineKeyboardButton("✏️ مخصص",  callback_data="stwizard:capital:custom"),
        ],
        [InlineKeyboardButton("❌ إلغاء", callback_data="staction:list")],
    ])

def _kb_st_paper() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 ورقي (بدون أوامر حقيقية)", callback_data="stwizard:paper:true"),
            InlineKeyboardButton("💰 حقيقي",                    callback_data="stwizard:paper:false"),
        ],
        [InlineKeyboardButton("❌ إلغاء", callback_data="staction:list")],
    ])

def _kb_st_bot_detail(bid: int, running: bool) -> InlineKeyboardMarkup:
    if running:
        toggle_btn = InlineKeyboardButton("🔴 إيقاف", callback_data=f"stbot:stop:{bid}")
    else:
        toggle_btn = InlineKeyboardButton("🟢 تشغيل", callback_data=f"stbot:start:{bid}")
    return InlineKeyboardMarkup([
        [
            toggle_btn,
            InlineKeyboardButton("📊 الإشارات", callback_data=f"stbot:signals:{bid}"),
        ],
        [
            InlineKeyboardButton("📈 الحالة",   callback_data=f"stbot:status:{bid}"),
            InlineKeyboardButton("🗑️ حذف",      callback_data=f"stbot:delete:{bid}"),
        ],
        [InlineKeyboardButton("🔙 رجوع", callback_data="staction:list")],
    ])

def _kb_st_list(bots: list) -> InlineKeyboardMarkup:
    rows = []
    for b in bots:
        bid  = b["id"]
        name = b.get("name", f"بوت {bid}")
        icon = "🟢" if b.get("running") else "⚫"
        rows.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"stbot:detail:{bid}")])
    rows.append([
        InlineKeyboardButton("➕ إنشاء بوت ST+UT", callback_data="staction:create"),
        InlineKeyboardButton("🔙 رجوع",             callback_data="action:menu"),
    ])
    return InlineKeyboardMarkup(rows)

def _fmt_st_wizard_summary(ctx) -> str:
    ud = ctx.user_data
    return (
        f"📝 *ملخص بوت ST+UT:*\n\n"
        f"الاسم: *{ud.get('st_name', '—')}*\n"
        f"العملة: `{ud.get('st_symbol', '—')}`\n"
        f"الإطار الزمني: `{ud.get('st_interval', '—')}`\n"
        f"رأس المال: `{ud.get('st_capital', 0)} USDT`\n"
        f"SuperTrend: فترة `{ud.get('st_period', 10)}` × `{ud.get('st_multiplier', 3.0)}`\n"
        f"UT Bot: حساسية `{ud.get('st_kv', 1.0)}` / ATR `{ud.get('st_atr', 1)}`\n"
        f"الوضع: {'📄 ورقي' if ud.get('st_paper') else '💰 حقيقي'}"
    )

def _kb_asset_pick(assets: list, action: str, pid: int) -> InlineKeyboardMarkup:
    """Inline keyboard listing each asset as a button. action: sell|remove|replace."""
    rows = []
    for i in range(0, len(assets), 3):
        row = []
        for a in assets[i:i+3]:
            sym = a["symbol"]
            row.append(InlineKeyboardButton(
                f"{sym}",
                callback_data=f"asset:{action}:{pid}:{sym}",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data=f"portfolio:{pid}")])
    return InlineKeyboardMarkup(rows)

# ── Formatters ─────────────────────────────────────────────────────────────────
def _fmt_portfolio_balance(pid: int) -> str:
    cfg = _get_portfolio_fn(pid)
    if not cfg:
        return "❌ المحفظة غير موجودة."
    assets = cfg.get("portfolio", {}).get("assets", [])
    if not assets:
        return "⚠️ لا توجد عملات في هذه المحفظة."
    try:
        balances = _get_balances_fn()
    except Exception as e:
        return f"❌ خطأ في جلب الأرصدة: `{e}`"
    name = cfg.get("bot", {}).get("name", f"محفظة {pid}")
    from portfolio.mexc_client import MEXCClient
    client = MEXCClient()
    lines = [f"💼 *{name}*\n─────────────────────────\n"]
    total = 0.0
    for a in assets:
        sym  = a["symbol"].upper()
        tgt  = a.get("allocation_pct", 0)
        bal  = balances.get(sym, 0.0)
        try:
            price = 1.0 if sym == "USDT" else client.get_price(f"{sym}USDT")
        except Exception:
            price = 0.0
        val = bal * price
        total += val
        if val > 0:
            lines.append(f"▸ `{sym:<6}` `{val:>10.2f} USDT`  _{tgt:.0f}%_")
    if len(lines) == 1:
        lines.append("_لا توجد أرصدة._")
    lines.append(f"\n─────────────────────────\n💎 *الإجمالي:* `{total:.2f} USDT`")
    return "\n".join(lines)

def _fmt_all_balances() -> str:
    try:
        balances = _get_balances_fn()
    except Exception as e:
        return f"❌ خطأ في جلب الأرصدة: `{e}`"
    non_zero = {s: b for s, b in balances.items() if b > 0}
    if not non_zero:
        return "💼 لا توجد أرصدة في الحساب."
    from portfolio.mexc_client import MEXCClient
    client = MEXCClient()
    lines = ["💰 *الرصيد العام*\n─────────────────────────\n"]
    total = 0.0
    for sym, bal in sorted(non_zero.items()):
        try:
            price = 1.0 if sym == "USDT" else client.get_price(f"{sym}USDT")
        except Exception:
            price = 0.0
        val = bal * price
        total += val
        lines.append(f"▸ `{sym:<6}` `{val:>10.2f} USDT`")
    lines.append(f"\n─────────────────────────\n💎 *الإجمالي:* `{total:.2f} USDT`")
    return "\n".join(lines)

def _fmt_wizard_summary(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Build a readable summary of wizard state so far."""
    ud = ctx.user_data
    name   = ud.get("new_bot_name", "—")
    syms   = ud.get("new_bot_symbols", [])
    mode   = ud.get("alloc_mode", "—")
    dev    = ud.get("deviation_pct")
    bal    = ud.get("balance_mode", "—")
    amount = ud.get("balance_usdt")

    sym_str  = "  ".join(f"`{s}`" for s in syms) if syms else "—"
    mode_str = "⚖️ متساوي تلقائي" if mode == "equal" else "✏️ يدوي"
    dev_str  = f"`{dev}%`" if dev is not None else "—"
    bal_str  = "💯 كل الرصيد" if bal == "all" else (f"💵 `{amount} USDT`" if amount else "—")

    return (
        f"📝 *ملخص البوت:*\n\n"
        f"الاسم: *{name}*\n"
        f"العملات: {sym_str}\n"
        f"التوزيع: {mode_str}\n"
        f"الانحراف: {dev_str}\n"
        f"الرصيد: {bal_str}"
    )

# ── Helpers ────────────────────────────────────────────────────────────────────
async def _edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def _reply(update: Update, text: str, kb=None) -> None:
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ── Home screen builder ────────────────────────────────────────────────────────
def _build_home() -> tuple[str, InlineKeyboardMarkup]:
    """
    Returns (text, keyboard) for the home screen.
    - No portfolios  → simple menu with create button
    - One portfolio  → show its controls directly
    - Many portfolios → show portfolio list buttons + create
    """
    portfolios = _list_portfolios()
    for p in portfolios:
        p["running"] = _is_running_fn(p["id"])

    if not portfolios:
        text = (
            "┌─────────────────────────┐\n"
            "│  💼  *Portfolio Bot*    │\n"
            "│  📍  *MEXC Spot*        │\n"
            "└─────────────────────────┘\n\n"
            "⚪ لا توجد محافظ بعد.\n\n"
            "ابدأ بإنشاء محفظة جديدة أو شغّل بوت ST+UT."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إنشاء محفظة جديدة", callback_data="action:create_bot")],
            [InlineKeyboardButton("💰 الرصيد العام",       callback_data="action:balance_all")],
        ])
        return text, kb

    if len(portfolios) == 1:
        p       = portfolios[0]
        pid     = p["id"]
        running = p["running"]
        name    = p.get("name", f"محفظة {pid}")
        status  = "🟢 تعمل" if running else "⚫ موقوفة"

        service_btn = (
            InlineKeyboardButton("🔴 بيع + إيقاف", callback_data=f"paction:sell_stop:{pid}")
            if running else
            InlineKeyboardButton("🟢 شراء + تشغيل", callback_data=f"paction:buy_start:{pid}")
        )

        text = (
            "┌─────────────────────────┐\n"
            "│  💼  *Portfolio Bot*    │\n"
            "└─────────────────────────┘\n\n"
            f"📁 *{name}*\n"
            f"📡 الحالة: {status}"
        )
        kb = InlineKeyboardMarkup([
            [service_btn,
             InlineKeyboardButton("🔄 إعادة توازن",   callback_data=f"paction:rebalance:{pid}")],
            [InlineKeyboardButton("🟢 شراء",           callback_data=f"paction:buy:{pid}"),
             InlineKeyboardButton("🔴 بيع",             callback_data=f"paction:sell:{pid}")],
            [InlineKeyboardButton("🗑️ حذف عملة",       callback_data=f"paction:remove:{pid}"),
             InlineKeyboardButton("🔁 استبدال عملة",    callback_data=f"paction:replace:{pid}")],
            [InlineKeyboardButton("💼 رصيد المحفظة",   callback_data=f"paction:balance:{pid}"),
             InlineKeyboardButton("⚙️ الإعدادات",       callback_data=f"psettings:menu:{pid}")],
            [InlineKeyboardButton("➕ محفظة جديدة",    callback_data="action:create_bot"),
             InlineKeyboardButton("💰 الرصيد العام",   callback_data="action:balance_all")],
        ])
        return text, kb

    # Many portfolios
    running_count = sum(1 for p in portfolios if p["running"])
    rows = []
    for p in portfolios:
        pid  = p["id"]
        name = p.get("name", f"محفظة {pid}")
        icon = "🟢" if p["running"] else "⚫"
        rows.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"portfolio:{pid}")])
    rows.append([
        InlineKeyboardButton("➕ محفظة جديدة",  callback_data="action:create_bot"),
        InlineKeyboardButton("💰 الرصيد العام", callback_data="action:balance_all"),
    ])
    text = (
        "┌─────────────────────────┐\n"
        "│  💼  *Portfolio Bot*    │\n"
        "└─────────────────────────┘\n\n"
        f"📊 المحافظ: `{len(portfolios)}`  |  🟢 تعمل: `{running_count}`\n\n"
        "اختر محفظة للإدارة:"
    )
    return text, InlineKeyboardMarkup(rows)


# ── /start ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return await _deny(update)
    ctx.user_data.clear()
    text, kb = _build_home()
    await _reply(update, text, kb)

# ── Callback handler ───────────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return await _deny(update)

    data = query.data

    # ── Main menu ──────────────────────────────────────────────────────────────
    if data == "action:menu":
        ctx.user_data.clear()
        text, kb = _build_home()
        await _edit(query, text, kb)

    elif data == "action:balance_all":
        await _edit(query, "⏳ جاري جلب الأرصدة...", _kb_back())
        text = _fmt_all_balances()
        await _edit(query, text, _kb_back())

    elif data == "action:portfolios":
        portfolios = _list_portfolios()
        if not portfolios:
            await _edit(query, "📋 لا توجد محافظ. أنشئ بوتاً أولاً.", _kb_back())
            return
        for p in portfolios:
            p["running"] = _is_running_fn(p["id"])
        await _edit(query, "📋 *المحافظ:*\n\nاختر محفظة:", _kb_portfolios(portfolios))

    # ── Wizard: start ──────────────────────────────────────────────────────────
    elif data == "action:create_bot":
        ctx.user_data.clear()
        ctx.user_data["state"] = "wizard_name"
        await _edit(
            query,
            "➕ *إنشاء بوت جديد*\n\n"
            "*الخطوة 1/5* — أرسل *اسم البوت:*",
            _kb_cancel(),
        )

    # ── Wizard: allocation mode ────────────────────────────────────────────────
    elif data == "wizard:alloc:equal":
        ctx.user_data["alloc_mode"] = "equal"
        ctx.user_data["state"]      = "wizard_deviation"
        syms = ctx.user_data.get("new_bot_symbols", [])
        n    = len(syms)
        pct  = round(100 / n, 2) if n else 0
        sym_lines = "\n".join(f"  • `{s}` — `{pct}%`" for s in syms)
        await _edit(
            query,
            f"✅ التوزيع المتساوي: كل عملة `{pct}%`\n\n"
            f"{sym_lines}\n\n"
            "*الخطوة 3/5* — اختر نسبة الانحراف لإعادة التوازن:",
            _kb_deviation(),
        )

    elif data == "wizard:alloc:manual":
        ctx.user_data["alloc_mode"] = "manual"
        ctx.user_data["state"]      = "wizard_manual_alloc"
        syms = ctx.user_data.get("new_bot_symbols", [])
        sym_str = "  ".join(f"`{s}`" for s in syms)
        await _edit(
            query,
            f"✏️ *التوزيع اليدوي*\n\n"
            f"العملات: {sym_str}\n\n"
            "*الخطوة 3/5* — أرسل النسب بالترتيب:\n"
            f"`{' '.join(syms)}`\n"
            "مثال: `40 30 20 10`\n\n"
            "_المجموع يجب أن يساوي 100%_",
            _kb_cancel(),
        )

    # ── Wizard: deviation preset ───────────────────────────────────────────────
    elif data.startswith("wizard:dev:"):
        val = data.split(":")[2]
        if val == "custom":
            ctx.user_data["state"] = "wizard_deviation_custom"
            await _edit(
                query,
                "*الخطوة 3/5* — أرسل نسبة الانحراف المخصصة (مثال: `2.5`):",
                _kb_cancel(),
            )
        else:
            ctx.user_data["deviation_pct"] = float(val)
            ctx.user_data["state"]         = "wizard_balance"
            await _edit(
                query,
                f"✅ الانحراف: `{val}%`\n\n"
                "*الخطوة 4/5* — كيف تريد تحديد رأس المال؟",
                _kb_balance_mode(),
            )

    # ── Wizard: balance mode ───────────────────────────────────────────────────
    elif data == "wizard:bal:all":
        ctx.user_data["balance_mode"] = "all"
        ctx.user_data["balance_usdt"] = 0
        ctx.user_data["state"]        = "wizard_confirm"
        summary = _fmt_wizard_summary(ctx)
        await _edit(
            query,
            f"{summary}\n\n"
            "*الخطوة 5/5* — هل تريد حفظ البوت؟",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ حفظ",   callback_data="wizard:confirm:yes"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="action:menu"),
                ]
            ]),
        )

    elif data == "wizard:bal:custom":
        ctx.user_data["balance_mode"] = "custom"
        ctx.user_data["state"]        = "wizard_balance_amount"
        await _edit(
            query,
            "*الخطوة 4/5* — أرسل المبلغ بـ USDT (مثال: `500`):",
            _kb_cancel(),
        )

    # ── Wizard: confirm & save ─────────────────────────────────────────────────
    elif data == "wizard:confirm:yes":
        await _wizard_save(query, ctx)

    # ── Portfolio home (from grid bot main menu) ───────────────────────────────
    elif data == "portfolio:home":
        text, kb = _build_home()
        await _edit(query, text, kb)

    # ── Portfolio detail ───────────────────────────────────────────────────────
    elif data.startswith("portfolio:"):
        pid = int(data.split(":")[1])
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await query.answer("المحفظة غير موجودة.", show_alert=True)
            return
        running = _is_running_fn(pid)
        name    = cfg.get("bot", {}).get("name", f"محفظة {pid}")
        mode_map = {"proportional": "نسبي", "timed": "مجدول", "unbalanced": "يدوي"}
        mode     = mode_map.get(cfg.get("rebalance", {}).get("mode", ""), "—")
        assets   = cfg.get("portfolio", {}).get("assets", [])
        asset_lines = "\n".join(
            f"  • `{a['symbol']}` — `{a['allocation_pct']}%`" for a in assets
        )
        dev = (
            cfg.get("rebalance", {})
               .get("proportional", {})
               .get("min_deviation_to_execute_pct", "—")
        )
        status_icon = "🟢 شغالة" if running else "⚫ موقوفة"
        text = (
            f"📋 *{name}*\n\n"
            f"الحالة: {status_icon}\n"
            f"الوضع: `{mode}`\n"
            f"الانحراف: `{dev}%`\n\n"
            f"*الأصول:*\n{asset_lines}"
        )
        await _edit(query, text, _kb_portfolio_detail(pid, running))

    # ── Portfolio actions ──────────────────────────────────────────────────────
    elif data.startswith("paction:"):
        parts  = data.split(":")
        action = parts[1]
        pid    = int(parts[2])

        if action == "start":
            if _is_running_fn(pid):
                await query.answer("المحفظة شغالة بالفعل.", show_alert=True)
                return
            _start_fn(pid)
            await _edit(query, "✅ *البوت بدأ*", _kb_portfolio_detail(pid, True))

        elif action == "stop":
            _stop_fn(pid)
            await _edit(query, "⏹️ *البوت أُوقف*", _kb_portfolio_detail(pid, False))

        elif action == "buy_start":
            # شراء المحفظة كلها ثم بدء الخدمة
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            sym_list = "\n".join(f"  • `{a['symbol']}` — `{a['allocation_pct']}%`" for a in assets)
            await _edit(
                query,
                f"🟢 *تأكيد شراء المحفظة وبدء الخدمة*\n\n"
                f"سيتم شراء جميع العملات حسب النسب:\n{sym_list}\n\n"
                "ثم تبدأ الخدمة تلقائياً. هل أنت متأكد؟",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ تأكيد", callback_data=f"confirm:buy_start:{pid}"),
                        InlineKeyboardButton("❌ إلغاء", callback_data=f"portfolio:{pid}"),
                    ]
                ]),
            )

        elif action == "sell_stop":
            # بيع المحفظة كلها ثم وقف الخدمة
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            sym_list = "\n".join(f"  • `{a['symbol']}`" for a in assets)
            await _edit(
                query,
                f"🔴 *تأكيد بيع المحفظة ووقف الخدمة*\n\n"
                f"سيتم بيع جميع العملات:\n{sym_list}\n\n"
                "ثم تتوقف الخدمة. هل أنت متأكد؟",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ تأكيد", callback_data=f"confirm:sell_stop:{pid}"),
                        InlineKeyboardButton("❌ إلغاء", callback_data=f"portfolio:{pid}"),
                    ]
                ]),
            )

        elif action == "rebalance":
            await _edit(
                query,
                "🔄 *تأكيد إعادة التوازن*\n\nسيتم بيع العملات الزائدة وشراء الناقصة حسب النسب المحددة.",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ تأكيد", callback_data=f"confirm:rebalance:{pid}"),
                        InlineKeyboardButton("❌ إلغاء", callback_data=f"portfolio:{pid}"),
                    ]
                ]),
            )

        elif action == "buy":
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            if not assets:
                await query.answer("لا توجد عملات في المحفظة.", show_alert=True)
                return
            await _edit(
                query,
                "🟢 *شراء — اختر نوع الشراء:*",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("💰 شراء عملة محددة",   callback_data=f"paction:buy_pick:{pid}"),
                        InlineKeyboardButton("🟢 شراء المحفظة كلها", callback_data=f"paction:buy_all:{pid}"),
                    ],
                    [InlineKeyboardButton("❌ إلغاء", callback_data=f"portfolio:{pid}")],
                ]),
            )

        elif action == "buy_pick":
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            if not assets:
                await query.answer("لا توجد عملات في المحفظة.", show_alert=True)
                return
            await _edit(
                query,
                "🟢 *شراء عملة — اختر العملة:*",
                _kb_asset_pick(assets, "buy", pid),
            )

        elif action == "buy_all":
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            sym_list = "\n".join(f"  • `{a['symbol']}` — `{a['allocation_pct']}%`" for a in assets)
            await _edit(
                query,
                f"🟢 *تأكيد شراء المحفظة كلها*\n\n"
                f"سيتم شراء جميع العملات حسب النسب:\n{sym_list}\n\n"
                "هل أنت متأكد؟",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ تأكيد الشراء الكامل", callback_data=f"confirm:buy_all:{pid}"),
                        InlineKeyboardButton("❌ إلغاء",                callback_data=f"portfolio:{pid}"),
                    ]
                ]),
            )

        elif action == "sell":
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            if not assets:
                await query.answer("لا توجد عملات في المحفظة.", show_alert=True)
                return
            await _edit(
                query,
                "🔴 *بيع — اختر نوع البيع:*",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("💰 بيع عملة محددة", callback_data=f"paction:sell_pick:{pid}"),
                        InlineKeyboardButton("🔴 بيع المحفظة كلها", callback_data=f"paction:sell_all:{pid}"),
                    ],
                    [InlineKeyboardButton("❌ إلغاء", callback_data=f"portfolio:{pid}")],
                ]),
            )

        elif action == "sell_pick":
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            if not assets:
                await query.answer("لا توجد عملات في المحفظة.", show_alert=True)
                return
            await _edit(
                query,
                "🔴 *بيع عملة — اختر العملة:*",
                _kb_asset_pick(assets, "sell", pid),
            )

        elif action == "sell_all":
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            if not assets:
                await query.answer("لا توجد عملات في المحفظة.", show_alert=True)
                return
            sym_list = "\n".join(f"  • `{a['symbol']}`" for a in assets)
            await _edit(
                query,
                f"⚠️ *تأكيد بيع المحفظة كلها*\n\n"
                f"سيتم بيع جميع العملات:\n{sym_list}\n\n"
                "هل أنت متأكد؟",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ تأكيد البيع الكامل", callback_data=f"confirm:sell_all:{pid}"),
                        InlineKeyboardButton("❌ إلغاء",               callback_data=f"portfolio:{pid}"),
                    ]
                ]),
            )

        elif action == "remove":
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            if not assets:
                await query.answer("لا توجد عملات في المحفظة.", show_alert=True)
                return
            await _edit(
                query,
                "🗑️ *حذف عملة — اختر العملة التي تريد حذفها:*",
                _kb_asset_pick(assets, "remove", pid),
            )

        elif action == "replace":
            cfg    = _get_portfolio_fn(pid)
            assets = cfg.get("portfolio", {}).get("assets", []) if cfg else []
            if not assets:
                await query.answer("لا توجد عملات في المحفظة.", show_alert=True)
                return
            await _edit(
                query,
                "🔁 *استبدال عملة — اختر العملة التي تريد استبدالها:*",
                _kb_asset_pick(assets, "replace", pid),
            )

        elif action == "balance":
            await _edit(query, "⏳ جاري جلب الرصيد...", _kb_back("action:menu"))
            text = _fmt_portfolio_balance(pid)
            await _edit(query, text, _kb_portfolio_detail(pid, _is_running_fn(pid)))

    # ── Asset picker result ────────────────────────────────────────────────────
    # callback_data = "asset:{action}:{pid}:{sym}"
    elif data.startswith("asset:"):
        _, act, pid_str, sym = data.split(":", 3)
        pid = int(pid_str)

        if act == "buy":
            ctx.user_data["state"]      = "await_buy_amount"
            ctx.user_data["trade_pid"]  = pid
            ctx.user_data["trade_sym"]  = sym
            await _edit(
                query,
                f"🟢 *شراء `{sym}`*\n\nأرسل المبلغ بـ USDT:\nمثال: `50`",
                _kb_back(f"paction:buy:{pid}"),
            )

        elif act == "sell":
            ctx.user_data["state"]      = "await_sell_amount"
            ctx.user_data["trade_pid"]  = pid
            ctx.user_data["trade_sym"]  = sym
            await _edit(
                query,
                f"🔴 *بيع `{sym}`*\n\nأرسل الكمية:\nمثال: `0.001`",
                _kb_back(f"paction:sell:{pid}"),
            )

        elif act == "remove":
            ctx.user_data["state"]         = "confirm_remove"
            ctx.user_data["trade_pid"]     = pid
            ctx.user_data["trade_sym"]     = sym
            await _edit(
                query,
                f"🗑️ هل تريد حذف `{sym}` من المحفظة؟\n\n"
                "⚠️ سيتم إعادة توزيع النسب تلقائياً على باقي العملات.",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"confirm:remove:{pid}:{sym}"),
                        InlineKeyboardButton("❌ إلغاء",        callback_data=f"portfolio:{pid}"),
                    ]
                ]),
            )

        elif act == "replace":
            ctx.user_data["state"]         = "await_replace_new"
            ctx.user_data["trade_pid"]     = pid
            ctx.user_data["trade_sym"]     = sym
            await _edit(
                query,
                f"🔁 *استبدال `{sym}`*\n\nأرسل رمز العملة الجديدة:\nمثال: `ADA`",
                _kb_back(f"paction:replace:{pid}"),
            )

    # ── Confirm rebalance ──────────────────────────────────────────────────────
    elif data.startswith("confirm:rebalance:"):
        pid = int(data.split(":")[2])
        await _edit(query, "⏳ *جاري إعادة التوازن...*", _kb_back("action:menu"))
        try:
            result = _rebalance_fn(pid)
            trades = [r for r in result if r.get("action") in ("BUY", "SELL")]
            if trades:
                lines = ["✅ *تمت إعادة التوازن:*\n"]
                for r in trades:
                    icon = "🟢" if r["action"] == "BUY" else "🔴"
                    lines.append(f"{icon} `{r['symbol']}` `{r.get('diff_usdt', 0):+.2f}$`")
                await _edit(query, "\n".join(lines), _kb_portfolio_detail(pid, _is_running_fn(pid)))
            else:
                await _edit(query, "✅ *المحفظة متوازنة — لا توجد تعديلات.*",
                            _kb_portfolio_detail(pid, _is_running_fn(pid)))
        except Exception as e:
            await _edit(query, f"❌ فشلت إعادة التوازن: `{e}`", _kb_back("action:menu"))

    # ── Confirm sell all ───────────────────────────────────────────────────────
    elif data.startswith("confirm:sell_all:"):
        pid = int(data.split(":")[2])
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await query.answer("المحفظة غير موجودة.", show_alert=True)
            return
        assets = cfg.get("portfolio", {}).get("assets", [])
        await _edit(query, "⏳ *جاري بيع المحفظة كلها...*", _kb_back(f"portfolio:{pid}"))
        results, errors = [], []
        try:
            balances = _get_balances_fn()
        except Exception as e:
            await _edit(query, f"❌ فشل جلب الأرصدة: `{e}`", _kb_portfolio_detail(pid, _is_running_fn(pid)))
            return
        for a in assets:
            sym = a["symbol"].upper()
            if sym == "USDT":
                continue
            bal = balances.get(sym, 0.0)
            if bal <= 0:
                continue
            try:
                res = _sell_fn(f"{sym}USDT", bal)
                results.append(f"🔴 `{sym}` — Order ID: `{res.get('orderId', '—')}`")
            except Exception as e:
                errors.append(f"❌ `{sym}`: `{e}`")
        lines = ["✅ *تم بيع المحفظة:*\n"] + results
        if errors:
            lines += ["\n⚠️ *أخطاء:*"] + errors
        if not results and not errors:
            lines = ["ℹ️ لا توجد أرصدة للبيع."]
        await _edit(query, "\n".join(lines), _kb_portfolio_detail(pid, _is_running_fn(pid)))

    # ── Confirm buy_start (شراء + بدء الخدمة) ────────────────────────────────
    elif data.startswith("confirm:buy_start:"):
        pid = int(data.split(":")[2])
        await _edit(query, "⏳ *جاري شراء المحفظة وبدء الخدمة...*", _kb_back(f"portfolio:{pid}"))
        try:
            result = _rebalance_fn(pid)
            trades = [r for r in result if r.get("action") == "BUY"]
            _start_fn(pid)
            lines = ["✅ *تم الشراء وبدأت الخدمة:*\n"]
            for r in trades:
                lines.append(f"🟢 `{r['symbol']}` — `{r.get('diff_usdt', 0):.2f} USDT`")
            if not trades:
                lines = ["✅ *بدأت الخدمة*\nℹ️ لا توجد عملات تحتاج شراء حالياً."]
        except Exception as e:
            lines = [f"❌ فشل: `{e}`"]
        await _edit(query, "\n".join(lines), _kb_portfolio_detail(pid, _is_running_fn(pid)))

    # ── Confirm sell_stop (بيع + وقف الخدمة) ─────────────────────────────────
    elif data.startswith("confirm:sell_stop:"):
        pid = int(data.split(":")[2])
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await query.answer("المحفظة غير موجودة.", show_alert=True)
            return
        assets = cfg.get("portfolio", {}).get("assets", [])
        await _edit(query, "⏳ *جاري بيع المحفظة ووقف الخدمة...*", _kb_back(f"portfolio:{pid}"))
        _stop_fn(pid)
        results, errors = [], []
        try:
            balances = _get_balances_fn()
        except Exception as e:
            await _edit(query, f"❌ فشل جلب الأرصدة: `{e}`", _kb_portfolio_detail(pid, False))
            return
        for a in assets:
            sym = a["symbol"].upper()
            if sym == "USDT":
                continue
            bal = balances.get(sym, 0.0)
            if bal <= 0:
                continue
            try:
                res = _sell_fn(f"{sym}USDT", bal)
                results.append(f"🔴 `{sym}` — Order ID: `{res.get('orderId', '—')}`")
            except Exception as e:
                errors.append(f"❌ `{sym}`: `{e}`")
        lines = ["⏹️ *الخدمة أُوقفت*\n✅ *تم البيع:*\n"] + results
        if errors:
            lines += ["\n⚠️ *أخطاء:*"] + errors
        if not results and not errors:
            lines = ["⏹️ *الخدمة أُوقفت*\nℹ️ لا توجد أرصدة للبيع."]
        await _edit(query, "\n".join(lines), _kb_portfolio_detail(pid, False))

    # ── Confirm buy all ────────────────────────────────────────────────────────
    elif data.startswith("confirm:buy_all:"):
        pid = int(data.split(":")[2])
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await query.answer("المحفظة غير موجودة.", show_alert=True)
            return
        await _edit(query, "⏳ *جاري شراء المحفظة كلها...*", _kb_back(f"portfolio:{pid}"))
        try:
            cfg["buy_enabled"] = True
            result = _rebalance_fn(pid)
            trades = [r for r in result if r.get("action") == "BUY"]
            if trades:
                lines = ["✅ *تم الشراء:*\n"]
                for r in trades:
                    lines.append(f"🟢 `{r['symbol']}` — `{r.get('diff_usdt', 0):.2f} USDT`")
            else:
                lines = ["ℹ️ لا توجد عملات تحتاج شراء حالياً."]
        except Exception as e:
            lines = [f"❌ فشل الشراء: `{e}`"]
        await _edit(query, "\n".join(lines), _kb_portfolio_detail(pid, _is_running_fn(pid)))

    # ── SuperTrend bot list ────────────────────────────────────────────────────
    elif data == "staction:list":
        bots = _st_list_fn()
        for b in bots:
            b["running"] = _st_is_running(b["id"])
        if not bots:
            await _edit(
                query,
                "📡 *بوتات SuperTrend + UT Bot*\n\nلا توجد بوتات بعد.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ إنشاء بوت ST+UT", callback_data="staction:create")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data="action:menu")],
                ]),
            )
        else:
            await _edit(query, "📡 *بوتات SuperTrend + UT Bot:*\n\nاختر بوتاً:", _kb_st_list(bots))

    # ── SuperTrend wizard: start ───────────────────────────────────────────────
    elif data == "staction:create":
        ctx.user_data.clear()
        ctx.user_data["state"] = "st_wizard_name"
        await _edit(
            query,
            "📡 *إنشاء بوت SuperTrend + UT Bot*\n\n"
            "*الخطوة 1/4* — أرسل *اسم البوت:*\n"
            "مثال: `BTC 1h`",
            _kb_cancel(),
        )

    # ── SuperTrend wizard: capital preset ─────────────────────────────────────
    # Step 3: symbol → capital
    elif data.startswith("stwizard:capital:"):
        val = data.split(":")[2]
        if val == "custom":
            ctx.user_data["state"] = "st_wizard_capital_custom"
            await _edit(
                query,
                "*الخطوة 3/4* — أرسل مبلغ الشراء بـ USDT:\nمثال: `150`",
                _kb_cancel(),
            )
        else:
            ctx.user_data["st_capital"] = float(val)
            ctx.user_data["state"]      = "st_wizard_interval"
            await _edit(
                query,
                f"✅ مبلغ الشراء: `{val} USDT`\n\n"
                "*الخطوة 4/4* — اختر الإطار الزمني:",
                _kb_st_interval(),
            )

    # ── SuperTrend wizard: interval ────────────────────────────────────────────
    # Step 4: capital → interval → paper
    elif data.startswith("stwizard:interval:"):
        interval = data.split(":")[2]
        ctx.user_data["st_interval"] = interval
        ctx.user_data["state"]       = "st_wizard_paper"
        summary = _fmt_st_wizard_summary(ctx)
        await _edit(
            query,
            f"✅ الإطار الزمني: `{interval}`\n\n"
            f"{summary}\n\n"
            "اختر وضع التداول:",
            _kb_st_paper(),
        )

    # ── SuperTrend wizard: paper mode ─────────────────────────────────────────
    elif data.startswith("stwizard:paper:"):
        paper = data.split(":")[2] == "true"
        ctx.user_data["st_paper"] = paper
        ctx.user_data["state"]    = "st_wizard_confirm"
        summary = _fmt_st_wizard_summary(ctx)
        await _edit(
            query,
            f"{summary}\n\n"
            "هل تريد حفظ البوت وتشغيله؟",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ حفظ وتشغيل", callback_data="stwizard:confirm:yes"),
                    InlineKeyboardButton("❌ إلغاء",       callback_data="staction:list"),
                ]
            ]),
        )

    # ── SuperTrend wizard: confirm & save ──────────────────────────────────────
    elif data == "stwizard:confirm:yes":
        ud = ctx.user_data
        name     = ud.get("st_name", "بوت ST+UT")
        symbol   = ud.get("st_symbol", "BTCUSDT").upper()
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        interval = ud.get("st_interval", "1h")
        capital  = float(ud.get("st_capital", 50))
        paper    = bool(ud.get("st_paper", False))
        period   = int(ud.get("st_period", 10))
        mult     = float(ud.get("st_multiplier", 3.0))
        kv       = float(ud.get("st_kv", 1.0))
        atr_p    = int(ud.get("st_atr", 1))
        cfg = {
            "symbol":       symbol,
            "interval":     interval,
            "capital_usdt": capital,
            "paper_trading": paper,
            "supertrend":   {"period": period, "multiplier": mult},
            "ut_bot":       {"key_value": kv, "atr_period": atr_p},
        }
        try:
            bid = _st_create_fn(name, cfg)
            paper_tag = " | 📄 ورقي" if paper else ""
            await _edit(
                query,
                f"✅ *تم إنشاء البوت وبدأ التشغيل!*\n\n"
                f"الاسم: *{name}*\n"
                f"العملة: `{symbol}` | الإطار: `{interval}`\n"
                f"مبلغ الشراء: `{capital} USDT`{paper_tag}\n\n"
                "سيرسل إشعاراً عند أول إشارة شراء أو بيع.",
                _kb_st_bot_detail(bid, True),
            )
        except Exception as e:
            await _edit(query, f"❌ فشل الحفظ: `{e}`", _kb_st_main())
        ctx.user_data.clear()

    # ── SuperTrend bot detail ──────────────────────────────────────────────────
    elif data.startswith("stbot:detail:"):
        bid = int(data.split(":")[2])
        row = _st_get_fn(bid)
        if not row:
            await query.answer("البوت غير موجود.", show_alert=True)
            return
        running  = _st_is_running(bid)
        cfg      = row["config"]
        symbol   = cfg.get("symbol", "—")
        interval = cfg.get("interval", "—")
        capital  = cfg.get("capital_usdt", 0)
        paper    = cfg.get("paper_trading", False)
        status_icon = "🟢 شغال" if running else "⚫ موقوف"
        paper_tag   = " | 📄 ورقي" if paper else ""
        text = (
            f"📡 *{row['name']}*\n\n"
            f"الحالة: {status_icon}{paper_tag}\n"
            f"العملة: `{symbol}` | الإطار: `{interval}`\n"
            f"مبلغ الشراء: `{capital} USDT`"
        )
        await _edit(query, text, _kb_st_bot_detail(bid, running))

    # ── SuperTrend bot: start ──────────────────────────────────────────────────
    elif data.startswith("stbot:start:"):
        bid = int(data.split(":")[2])
        if _st_is_running(bid):
            await query.answer("البوت شغال بالفعل.", show_alert=True)
            return
        _st_start_fn(bid)
        await query.answer("✅ البوت بدأ.")
        row = _st_get_fn(bid)
        if row:
            cfg      = row["config"]
            symbol   = cfg.get("symbol", "—")
            interval = cfg.get("interval", "—")
            capital  = cfg.get("capital_usdt", 0)
            paper    = cfg.get("paper_trading", False)
            paper_tag = " | 📄 ورقي" if paper else ""
            text = (
                f"📡 *{row['name']}*\n\n"
                f"الحالة: 🟢 شغال{paper_tag}\n"
                f"العملة: `{symbol}` | الإطار: `{interval}`\n"
                f"مبلغ الشراء: `{capital} USDT`"
            )
            await _edit(query, text, _kb_st_bot_detail(bid, True))

    # ── SuperTrend bot: stop ───────────────────────────────────────────────────
    elif data.startswith("stbot:stop:"):
        bid = int(data.split(":")[2])
        _st_stop_fn(bid)
        await query.answer("⏹️ البوت أُوقف.")
        row = _st_get_fn(bid)
        if row:
            cfg      = row["config"]
            symbol   = cfg.get("symbol", "—")
            interval = cfg.get("interval", "—")
            capital  = cfg.get("capital_usdt", 0)
            paper    = cfg.get("paper_trading", False)
            paper_tag = " | 📄 ورقي" if paper else ""
            text = (
                f"📡 *{row['name']}*\n\n"
                f"الحالة: ⚫ موقوف{paper_tag}\n"
                f"العملة: `{symbol}` | الإطار: `{interval}`\n"
                f"مبلغ الشراء: `{capital} USDT`"
            )
            await _edit(query, text, _kb_st_bot_detail(bid, False))

    # ── SuperTrend bot: signals history ───────────────────────────────────────
    elif data.startswith("stbot:signals:"):
        bid  = int(data.split(":")[2])
        sigs = _st_signals_fn(bid, 10)
        if not sigs:
            await _edit(query, "📊 لا توجد إشارات بعد.", _kb_st_bot_detail(bid, _st_is_running(bid)))
            return
        lines = ["📊 *آخر الإشارات:*\n"]
        for s in sigs:
            icon = "🟢" if s["signal"] == "BUY" else "🔴"
            paper_tag = " 📄" if s.get("paper") else ""
            err_tag   = f"\n  ❌ `{s['error']}`" if s.get("error") else ""
            lines.append(
                f"{icon} `{s['signal']}` — `{s['ts']}`{paper_tag}\n"
                f"  السعر: `{s['price']:.6f}` | القيمة: `{s['usdt']:.2f} USDT`{err_tag}"
            )
        await _edit(query, "\n".join(lines), _kb_st_bot_detail(bid, _st_is_running(bid)))

    # ── SuperTrend bot: live status ────────────────────────────────────────────
    elif data.startswith("stbot:status:"):
        bid = int(data.split(":")[2])
        row = _st_get_fn(bid)
        if not row:
            await query.answer("البوت غير موجود.", show_alert=True)
            return
        await _edit(query, "⏳ جاري تحليل السوق...", _kb_back(f"stbot:detail:{bid}"))
        try:
            from portfolio.mexc_client import MEXCClient
            from portfolio.supertrend_bot import analyze_current_state
            cfg      = row["config"]
            symbol   = cfg.get("symbol", "BTCUSDT")
            _iv_fix  = {"1h": "60m", "2h": "60m", "3m": "5m", "6h": "4h", "12h": "8h"}
            interval = _iv_fix.get(cfg.get("interval", "60m"), cfg.get("interval", "60m"))
            st_cfg   = cfg.get("supertrend", {})
            ut_cfg   = cfg.get("ut_bot", {})
            period   = int(st_cfg.get("period", 10))
            mult     = float(st_cfg.get("multiplier", 3.0))
            kv       = float(ut_cfg.get("key_value", 1.0))
            atr_p    = int(ut_cfg.get("atr_period", 1))
            candles  = MEXCClient().get_klines(symbol, interval, limit=max(period, atr_p) * 3 + 10)
            state    = analyze_current_state(candles, period, mult, kv, atr_p)
            if state.get("error"):
                text = f"⚠️ {state['error']}"
            else:
                sig = state["signal"]
                sig_str = f"🟢 *BUY*" if sig == "BUY" else (f"🔴 *SELL*" if sig == "SELL" else "⏸️ لا إشارة")
                text = (
                    f"📈 *حالة {symbol} ({interval})*\n\n"
                    f"السعر: `{state['close']:.6f}`\n"
                    f"SuperTrend: {state['st_direction_str']} (`{state['st_value']:.6f}`)\n"
                    f"UT Bot: {state['ut_direction_str']} (trailing: `{state['ut_trailing']:.6f}`)\n\n"
                    f"الإشارة الحالية: {sig_str}"
                )
        except Exception as e:
            text = f"❌ خطأ: `{e}`"
        await _edit(query, text, _kb_st_bot_detail(bid, _st_is_running(bid)))

    # ── SuperTrend bot: delete ─────────────────────────────────────────────────
    elif data.startswith("stbot:delete:"):
        bid = int(data.split(":")[2])
        row = _st_get_fn(bid)
        name = row["name"] if row else f"بوت {bid}"
        await _edit(
            query,
            f"🗑️ هل تريد حذف بوت *{name}*؟\n\n⚠️ سيتم حذف جميع الإشارات أيضاً.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"stbot:confirm_delete:{bid}"),
                    InlineKeyboardButton("❌ إلغاء",        callback_data=f"stbot:detail:{bid}"),
                ]
            ]),
        )

    elif data.startswith("psettings:"):
        await _handle_psettings(query, ctx, data)

    elif data.startswith("stbot:confirm_delete:"):
        bid = int(data.split(":")[2])
        if _st_is_running(bid):
            _st_stop_fn(bid)
        _st_delete_fn(bid)
        await _edit(query, "✅ *تم حذف البوت.*", _kb_st_main())

    # ── Confirm remove ─────────────────────────────────────────────────────────
    elif data.startswith("confirm:remove:"):
        _, _, pid_str, sym = data.split(":", 3)
        pid = int(pid_str)
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await query.answer("المحفظة غير موجودة.", show_alert=True)
            return
        assets = cfg.get("portfolio", {}).get("assets", [])
        assets = [a for a in assets if a["symbol"].upper() != sym.upper()]
        if not assets:
            await _edit(query, "⚠️ لا يمكن حذف العملة الوحيدة في المحفظة.", _kb_portfolio_detail(pid, _is_running_fn(pid)))
            return
        # Redistribute allocations equally among remaining assets
        pct = round(100 / len(assets), 4)
        for a in assets:
            a["allocation_pct"] = pct
        diff = round(100 - sum(a["allocation_pct"] for a in assets), 4)
        assets[-1]["allocation_pct"] = round(assets[-1]["allocation_pct"] + diff, 4)
        cfg["portfolio"]["assets"] = assets
        try:
            _update_portfolio_fn(pid, cfg)
            asset_lines = "\n".join(f"  • `{a['symbol']}` — `{a['allocation_pct']}%`" for a in assets)
            await _edit(
                query,
                f"✅ *تم حذف `{sym}` وإعادة توزيع النسب:*\n\n{asset_lines}",
                _kb_portfolio_detail(pid, _is_running_fn(pid)),
            )
        except Exception as e:
            await _edit(query, f"❌ فشل الحذف: `{e}`", _kb_portfolio_detail(pid, _is_running_fn(pid)))

# ── Portfolio settings ─────────────────────────────────────────────────────────

def _fmt_portfolio_settings(pid: int) -> str:
    cfg = _get_portfolio_fn(pid)
    if not cfg:
        return "❌ المحفظة غير موجودة."
    name   = cfg.get("bot", {}).get("name", f"محفظة {pid}")
    budget = cfg.get("portfolio", {}).get("total_usdt", 0)
    assets = cfg.get("portfolio", {}).get("assets", [])
    reb    = cfg.get("rebalance", {})
    mode   = {"proportional": "نسبي 📊", "timed": "مجدول 🕐", "unbalanced": "يدوي ✋"}.get(reb.get("mode", ""), "—")
    dev    = reb.get("proportional", {}).get("min_deviation_to_execute_pct", "—")
    paper  = "📄 ورقي" if cfg.get("paper_trading") else "💰 حقيقي"
    sl     = cfg.get("risk", {}).get("stop_loss_pct")
    tp     = cfg.get("risk", {}).get("take_profit_pct")
    sl_str = f"`{sl}%`" if sl else "غير مفعّل"
    tp_str = f"`{tp}%`" if tp else "غير مفعّل"
    asset_lines = "\n".join(f"  • `{a['symbol']}` — `{a['allocation_pct']}%`" for a in assets)
    return (
        f"⚙️ *إعدادات {name}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"الاسم:        `{name}`\n"
        f"الميزانية:    `{budget} USDT`\n"
        f"وضع التوازن:  {mode}\n"
        f"الانحراف:     `{dev}%`\n"
        f"وضع التداول:  {paper}\n"
        f"وقف الخسارة:  {sl_str}\n"
        f"جني الأرباح:  {tp_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*الأصول:*\n{asset_lines}"
    )


async def _handle_psettings(query, ctx, data: str) -> None:
    parts  = data.split(":")
    action = parts[1]
    pid    = int(parts[2])

    if action in ("menu", "view"):
        await _edit(query, _fmt_portfolio_settings(pid), _kb_portfolio_settings(pid))

    elif action == "export":
        import json as _json
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await query.answer("المحفظة غير موجودة.", show_alert=True)
            return
        text = f"```json\n{_json.dumps(cfg, ensure_ascii=False, indent=2)}\n```"
        await _edit(query, text, _kb_portfolio_settings(pid))

    elif action == "rename":
        ctx.user_data["state"]        = "settings_rename"
        ctx.user_data["settings_pid"] = pid
        await _edit(query, "✏️ *تغيير اسم المحفظة*\n\nأرسل الاسم الجديد:", _kb_back(f"psettings:menu:{pid}"))

    elif action == "budget":
        cfg = _get_portfolio_fn(pid)
        cur = cfg.get("portfolio", {}).get("total_usdt", 0) if cfg else 0
        ctx.user_data["state"]        = "settings_budget"
        ctx.user_data["settings_pid"] = pid
        await _edit(query, f"💵 *تغيير الميزانية*\n\nالحالية: `{cur} USDT`\n\nأرسل الميزانية الجديدة:", _kb_back(f"psettings:menu:{pid}"))

    elif action == "deviation":
        cfg = _get_portfolio_fn(pid)
        cur = cfg.get("rebalance", {}).get("proportional", {}).get("min_deviation_to_execute_pct", 3) if cfg else 3
        await _edit(
            query,
            f"📊 *تغيير نسبة الانحراف*\n\nالحالية: `{cur}%`\n\nاختر نسبة:",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1%",  callback_data=f"psettings:dev_set:{pid}:1"),
                    InlineKeyboardButton("3%",  callback_data=f"psettings:dev_set:{pid}:3"),
                    InlineKeyboardButton("5%",  callback_data=f"psettings:dev_set:{pid}:5"),
                    InlineKeyboardButton("10%", callback_data=f"psettings:dev_set:{pid}:10"),
                ],
                [InlineKeyboardButton("✏️ مخصص", callback_data=f"psettings:dev_custom:{pid}")],
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"psettings:menu:{pid}")],
            ]),
        )

    elif action == "dev_set":
        val = float(parts[3])
        cfg = _get_portfolio_fn(pid)
        if cfg:
            cfg.setdefault("rebalance", {}).setdefault("proportional", {})
            cfg["rebalance"]["proportional"]["threshold_pct"]               = val
            cfg["rebalance"]["proportional"]["min_deviation_to_execute_pct"] = val
            _update_portfolio_fn(pid, cfg)
        await _edit(query, f"✅ تم تغيير الانحراف إلى `{val}%`", _kb_portfolio_settings(pid))

    elif action == "dev_custom":
        ctx.user_data["state"]        = "settings_deviation"
        ctx.user_data["settings_pid"] = pid
        await _edit(query, "📊 أرسل نسبة الانحراف المخصصة (مثال: `2.5`):", _kb_back(f"psettings:menu:{pid}"))

    elif action == "mode":
        await _edit(
            query,
            "🔄 *وضع إعادة التوازن*\n\nاختر الوضع:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 نسبي (عند الانحراف)",  callback_data=f"psettings:mode_set:{pid}:proportional")],
                [InlineKeyboardButton("🕐 مجدول (يومي/أسبوعي)", callback_data=f"psettings:mode_set:{pid}:timed")],
                [InlineKeyboardButton("✋ يدوي (بدون تلقائي)",   callback_data=f"psettings:mode_set:{pid}:unbalanced")],
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"psettings:menu:{pid}")],
            ]),
        )

    elif action == "mode_set":
        new_mode = parts[3]
        cfg = _get_portfolio_fn(pid)
        if cfg:
            cfg.setdefault("rebalance", {})["mode"] = new_mode
            _update_portfolio_fn(pid, cfg)
        names = {"proportional": "نسبي 📊", "timed": "مجدول 🕐", "unbalanced": "يدوي ✋"}
        await _edit(query, f"✅ تم تغيير وضع التوازن إلى *{names.get(new_mode, new_mode)}*", _kb_portfolio_settings(pid))

    elif action == "delete":
        cfg  = _get_portfolio_fn(pid)
        name = cfg.get("bot", {}).get("name", f"محفظة {pid}") if cfg else f"محفظة {pid}"
        await _edit(
            query,
            f"🗑️ *حذف المحفظة*\n\nهل تريد حذف *{name}*؟\n\n"
            "⚠️ سيتم حذف المحفظة وكل سجلاتها نهائياً.\n_لن يتم بيع العملات تلقائياً._",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"psettings:confirm_delete:{pid}"),
                InlineKeyboardButton("❌ إلغاء",        callback_data=f"psettings:menu:{pid}"),
            ]]),
        )

    elif action == "confirm_delete":
        cfg  = _get_portfolio_fn(pid)
        name = cfg.get("bot", {}).get("name", f"محفظة {pid}") if cfg else f"محفظة {pid}"
        if _is_running_fn(pid):
            _stop_fn(pid)
        try:
            _delete_portfolio_fn(pid)
            text, kb = _build_home()
            await _edit(query, f"✅ *تم حذف المحفظة `{name}` بنجاح.*\n\n" + text, kb)
        except Exception as e:
            await _edit(query, f"❌ فشل الحذف: `{e}`", _kb_portfolio_settings(pid))


# ── Wizard save helper ─────────────────────────────────────────────────────────
async def _wizard_save(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ud       = ctx.user_data
    bot_name = ud.get("new_bot_name", "بوت جديد")
    syms     = ud.get("new_bot_symbols", [])
    mode     = ud.get("alloc_mode", "equal")
    dev      = ud.get("deviation_pct", 3.0)
    bal_mode = ud.get("balance_mode", "all")
    amount   = ud.get("balance_usdt", 0)

    if not syms:
        await _edit(query, "⚠️ لا توجد عملات.", _kb_cancel())
        return

    # Build assets list
    if mode == "equal":
        pct = round(100 / len(syms), 4)
        assets = [{"symbol": s, "allocation_pct": pct} for s in syms]
        # Adjust last to ensure sum == 100
        diff = 100 - sum(a["allocation_pct"] for a in assets)
        assets[-1]["allocation_pct"] = round(assets[-1]["allocation_pct"] + diff, 4)
    else:
        assets = ud.get("new_bot_assets", [])

    cfg = {
        "bot": {"name": bot_name},
        "portfolio": {
            "assets": assets,
            "total_usdt": amount,
            "initial_value_usdt": 0,
            "allocation_mode": "equal" if mode == "equal" else "manual",
        },
        "rebalance": {
            "mode": "proportional",
            "proportional": {
                "threshold_pct": dev,
                "check_interval_minutes": 5,
                "min_deviation_to_execute_pct": dev,
            },
            "timed": {"frequency": "daily", "hour": 10},
            "unbalanced": {},
        },
        "risk": {"stop_loss_pct": None, "take_profit_pct": None},
        "termination": {"sell_at_termination": False},
        "asset_transfer": {"enable_asset_transfer": False},
        "paper_trading": False,
        "last_rebalance": None,
    }

    try:
        pid = _save_portfolio_fn(bot_name, cfg)
        asset_lines = "\n".join(
            f"  • `{a['symbol']}` — `{a['allocation_pct']}%`" for a in assets
        )
        bal_str = "💯 كل الرصيد" if bal_mode == "all" else f"💵 `{amount} USDT`"
        await _edit(
            query,
            f"✅ *تم إنشاء البوت بنجاح!*\n\n"
            f"الاسم: *{bot_name}*\n"
            f"ID: `{pid}`\n"
            f"الانحراف: `{dev}%`\n"
            f"الرصيد: {bal_str}\n\n"
            f"*الأصول:*\n{asset_lines}\n\n"
            "اذهب للمحافظ لتشغيله.",
            _kb_main(),
        )
    except Exception as e:
        await _edit(query, f"❌ فشل الحفظ: `{e}`", _kb_main())

    ctx.user_data.clear()


# ── Message handler (wizard + trade steps) ────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return await _deny(update)

    state = ctx.user_data.get("state", "")
    text  = (update.message.text or "").strip()

    # ── Wizard step 1: bot name ────────────────────────────────────────────────
    if state == "wizard_name":
        if not text:
            await _reply(update, "⚠️ أرسل اسماً صحيحاً.", _kb_cancel())
            return
        ctx.user_data["new_bot_name"] = text
        ctx.user_data["state"]        = "wizard_symbols"
        await _reply(
            update,
            f"✅ الاسم: *{text}*\n\n"
            "*الخطوة 2/5* — أرسل العملات بدون نسب:\n"
            "`BTC ETH BNB SOL`\n\n"
            "_يمكنك إرسالها في رسالة واحدة أو عدة رسائل — أرسل /done عند الانتهاء_",
            _kb_cancel(),
        )

    # ── Wizard step 2: symbols ─────────────────────────────────────────────────
    elif state == "wizard_symbols":
        syms: list = ctx.user_data.setdefault("new_bot_symbols", [])
        existing   = {s.upper() for s in syms}
        added, skipped = [], []

        for token in text.replace(",", " ").upper().split():
            sym = token.strip()
            if not sym:
                continue
            if sym in existing:
                skipped.append(sym)
            elif len(syms) >= 20:
                skipped.append(f"{sym} (الحد 20)")
            else:
                syms.append(sym)
                existing.add(sym)
                added.append(sym)

        ctx.user_data["new_bot_symbols"] = syms
        sym_str = "  ".join(f"`{s}`" for s in syms)
        lines   = [f"📋 *العملات ({len(syms)}/20):*\n{sym_str}"]
        if skipped:
            lines.append(f"\n⚠️ تجاهلت: {' '.join(skipped)}")
        lines.append("\nأضف المزيد أو أرسل /done للمتابعة.")
        await _reply(update, "\n".join(lines), _kb_cancel())

    # ── Wizard step 3a: manual allocation ─────────────────────────────────────
    elif state == "wizard_manual_alloc":
        syms  = ctx.user_data.get("new_bot_symbols", [])
        parts = text.replace(",", " ").split()
        if len(parts) != len(syms):
            await _reply(
                update,
                f"⚠️ أرسل {len(syms)} أرقام بالترتيب.\n"
                f"العملات: {' '.join(syms)}\n"
                "مثال: `40 30 20 10`",
                _kb_cancel(),
            )
            return
        try:
            pcts = [float(p) for p in parts]
        except ValueError:
            await _reply(update, "⚠️ أرقام غير صحيحة.", _kb_cancel())
            return
        if any(p <= 0 for p in pcts):
            await _reply(update, "⚠️ كل نسبة يجب أن تكون أكبر من 0.", _kb_cancel())
            return
        total = sum(pcts)
        if abs(total - 100) > 0.01:
            await _reply(update, f"⚠️ المجموع `{total:.1f}%` — يجب أن يساوي 100%.", _kb_cancel())
            return
        assets = [{"symbol": s, "allocation_pct": p} for s, p in zip(syms, pcts)]
        ctx.user_data["new_bot_assets"] = assets
        ctx.user_data["state"]          = "wizard_deviation"
        lines = ["✅ *التوزيع:*\n"]
        for a in assets:
            lines.append(f"  • `{a['symbol']}` — `{a['allocation_pct']}%`")
        lines.append("\n*الخطوة 3/5* — اختر نسبة الانحراف:")
        await _reply(update, "\n".join(lines), _kb_deviation())

    # ── Wizard step 3b: custom deviation ──────────────────────────────────────
    elif state == "wizard_deviation_custom":
        try:
            dev = float(text)
            assert 0 < dev <= 50
        except Exception:
            await _reply(update, "⚠️ أرسل رقماً بين 0.1 و 50.", _kb_cancel())
            return
        ctx.user_data["deviation_pct"] = dev
        ctx.user_data["state"]         = "wizard_balance"
        await _reply(
            update,
            f"✅ الانحراف: `{dev}%`\n\n"
            "*الخطوة 4/5* — كيف تريد تحديد رأس المال؟",
            _kb_balance_mode(),
        )

    # ── Wizard step 4: custom balance amount ──────────────────────────────────
    elif state == "wizard_balance_amount":
        try:
            amount = float(text)
            assert amount > 0
        except Exception:
            await _reply(update, "⚠️ أرسل مبلغاً صحيحاً (مثال: `500`).", _kb_cancel())
            return
        ctx.user_data["balance_usdt"] = amount
        ctx.user_data["state"]        = "wizard_confirm"
        summary = _fmt_wizard_summary(ctx)
        await _reply(
            update,
            f"{summary}\n\n"
            "*الخطوة 5/5* — هل تريد حفظ البوت؟",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ حفظ",   callback_data="wizard:confirm:yes"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="action:menu"),
                ]
            ]),
        )

    # ── Trade: buy amount (after symbol chosen via button) ────────────────────
    elif state == "await_buy_amount":
        sym = ctx.user_data.get("trade_sym", "")
        try:
            amt = float(text)
            assert amt > 0
        except Exception:
            await _reply(update, "⚠️ أرسل مبلغاً صحيحاً بـ USDT (مثال: `50`).", _kb_cancel())
            return
        await _reply(update, f"⏳ جاري شراء `{sym}` بـ `{amt} USDT`...")
        try:
            result = _buy_fn(f"{sym}USDT", amt)
            await _reply(
                update,
                f"✅ *تم الشراء*\n`{sym}` بـ `{amt} USDT`\nOrder ID: `{result.get('orderId', '—')}`",
                _kb_main(),
            )
        except Exception as e:
            await _reply(update, f"❌ فشل الشراء: `{e}`", _kb_main())
        ctx.user_data.clear()

    # ── Trade: sell amount (after symbol chosen via button) ───────────────────
    elif state == "await_sell_amount":
        sym = ctx.user_data.get("trade_sym", "")
        try:
            amt = float(text)
            assert amt > 0
        except Exception:
            await _reply(update, "⚠️ أرسل كمية صحيحة (مثال: `0.001`).", _kb_cancel())
            return
        await _reply(update, f"⏳ جاري بيع `{amt}` من `{sym}`...")
        try:
            result = _sell_fn(f"{sym}USDT", amt)
            await _reply(
                update,
                f"✅ *تم البيع*\n`{amt}` من `{sym}`\nOrder ID: `{result.get('orderId', '—')}`",
                _kb_main(),
            )
        except Exception as e:
            await _reply(update, f"❌ فشل البيع: `{e}`", _kb_main())
        ctx.user_data.clear()

    # ── Replace: new symbol ────────────────────────────────────────────────────
    elif state == "await_replace_new":
        new_sym = text.upper().strip()
        pid     = ctx.user_data.get("trade_pid")
        old_sym = ctx.user_data.get("trade_sym", "")
        if not new_sym.isalpha():
            await _reply(update, "⚠️ أرسل رمز عملة صحيح (مثال: `ADA`).", _kb_cancel())
            return
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await _reply(update, "❌ المحفظة غير موجودة.", _kb_main())
            ctx.user_data.clear()
            return
        assets = cfg.get("portfolio", {}).get("assets", [])
        existing = [a["symbol"].upper() for a in assets]
        if new_sym in existing:
            await _reply(update, f"⚠️ `{new_sym}` موجودة بالفعل في المحفظة.", _kb_cancel())
            return
        for a in assets:
            if a["symbol"].upper() == old_sym.upper():
                a["symbol"] = new_sym
                break
        cfg["portfolio"]["assets"] = assets
        try:
            _update_portfolio_fn(pid, cfg)
            asset_lines = "\n".join(f"  • `{a['symbol']}` — `{a['allocation_pct']}%`" for a in assets)
            await _reply(
                update,
                f"✅ *تم استبدال `{old_sym}` بـ `{new_sym}`:*\n\n{asset_lines}",
                _kb_main(),
            )
        except Exception as e:
            await _reply(update, f"❌ فشل الاستبدال: `{e}`", _kb_main())
        ctx.user_data.clear()

    # ── SuperTrend wizard: step 1 — name ──────────────────────────────────────
    # ── Settings: rename ──────────────────────────────────────────────────────
    elif state == "settings_rename":
        pid = ctx.user_data.get("settings_pid")
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await _reply(update, "❌ المحفظة غير موجودة.", _kb_main())
            ctx.user_data.clear()
            return
        cfg.setdefault("bot", {})["name"] = text
        _update_portfolio_fn(pid, cfg)
        ctx.user_data.clear()
        await _reply(update, f"✅ تم تغيير الاسم إلى *{text}*", _kb_portfolio_settings(pid))

    # ── Settings: budget ──────────────────────────────────────────────────────
    elif state == "settings_budget":
        pid = ctx.user_data.get("settings_pid")
        try:
            amount = float(text)
            assert amount > 0
        except Exception:
            await _reply(update, "⚠️ أرسل مبلغاً صحيحاً (مثال: `500`).", _kb_cancel())
            return
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await _reply(update, "❌ المحفظة غير موجودة.", _kb_main())
            ctx.user_data.clear()
            return
        cfg.setdefault("portfolio", {})["total_usdt"] = amount
        _update_portfolio_fn(pid, cfg)
        ctx.user_data.clear()
        await _reply(update, f"✅ تم تغيير الميزانية إلى `{amount} USDT`", _kb_portfolio_settings(pid))

    # ── Settings: deviation custom ────────────────────────────────────────────
    elif state == "settings_deviation":
        pid = ctx.user_data.get("settings_pid")
        try:
            val = float(text)
            assert 0 < val <= 50
        except Exception:
            await _reply(update, "⚠️ أرسل رقماً بين 0.1 و 50.", _kb_cancel())
            return
        cfg = _get_portfolio_fn(pid)
        if not cfg:
            await _reply(update, "❌ المحفظة غير موجودة.", _kb_main())
            ctx.user_data.clear()
            return
        cfg.setdefault("rebalance", {}).setdefault("proportional", {})
        cfg["rebalance"]["proportional"]["threshold_pct"]               = val
        cfg["rebalance"]["proportional"]["min_deviation_to_execute_pct"] = val
        _update_portfolio_fn(pid, cfg)
        ctx.user_data.clear()
        await _reply(update, f"✅ تم تغيير الانحراف إلى `{val}%`", _kb_portfolio_settings(pid))

    elif state == "st_wizard_name":
        if not text:
            await _reply(update, "⚠️ أرسل اسماً صحيحاً.", _kb_cancel())
            return
        ctx.user_data["st_name"] = text
        ctx.user_data["state"]   = "st_wizard_symbol"
        await _reply(
            update,
            f"✅ الاسم: *{text}*\n\n"
            "*الخطوة 2/4* — أرسل رمز العملة:\n"
            "مثال: `BTC` أو `ETH` أو `BTCUSDT`",
            _kb_cancel(),
        )

    # ── SuperTrend wizard: step 2 — symbol ────────────────────────────────────
    elif state == "st_wizard_symbol":
        sym = text.upper().strip().replace("/", "")
        if not sym.endswith("USDT"):
            sym = sym + "USDT"
        ctx.user_data["st_symbol"] = sym
        ctx.user_data["state"]     = "st_wizard_capital"
        await _reply(
            update,
            f"✅ العملة: `{sym}`\n\n"
            "*الخطوة 3/4* — اختر مبلغ الشراء (USDT):",
            _kb_st_capital(),
        )

    # ── SuperTrend wizard: custom capital ─────────────────────────────────────
    elif state == "st_wizard_capital_custom":
        try:
            capital = float(text)
            assert capital >= 1
        except Exception:
            await _reply(update, "⚠️ أرسل مبلغاً صحيحاً (مثال: `150`).", _kb_cancel())
            return
        ctx.user_data["st_capital"] = capital
        ctx.user_data["state"]      = "st_wizard_paper"
        await _reply(
            update,
            f"✅ رأس المال: `{capital} USDT`\n\n"
            "*الخطوة 5/6* — اختر وضع التداول:",
            _kb_st_paper(),
        )

    else:
        await _reply(update, "اختر من القائمة:", _kb_main())


# ── /done — finalise wizard ────────────────────────────────────────────────────
async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return await _deny(update)

    state = ctx.user_data.get("state", "")

    # From symbols step → move to allocation mode choice
    if state == "wizard_symbols":
        syms = ctx.user_data.get("new_bot_symbols", [])
        if not syms:
            await _reply(update, "⚠️ أضف عملة واحدة على الأقل.", _kb_cancel())
            return
        if len(syms) < 2:
            await _reply(update, "⚠️ أضف عملتين على الأقل.", _kb_cancel())
            return
        sym_str = "  ".join(f"`{s}`" for s in syms)
        ctx.user_data["state"] = "wizard_alloc_mode"
        await _reply(
            update,
            f"✅ العملات: {sym_str}\n\n"
            "*الخطوة 3/5* — كيف تريد توزيع النسب؟",
            _kb_alloc_mode(),
        )

    # From manual alloc or deviation step → nothing pending
    elif state in ("wizard_manual_alloc", "wizard_deviation", "wizard_balance",
                   "wizard_balance_amount", "wizard_confirm"):
        await _reply(update, "⚠️ أكمل الخطوة الحالية أولاً.", _kb_cancel())

    else:
        await _reply(update, "لا يوجد شيء لحفظه.", _kb_main())

# ── Entry point ────────────────────────────────────────────────────────────────
def run_bot(
    start_fn:             Callable,
    stop_fn:              Callable,
    rebalance_fn:         Callable,
    list_portfolios_fn:   Callable,
    is_running_fn:        Callable,
    get_portfolio_fn:     Callable,
    save_portfolio_fn:    Callable,
    buy_fn:               Callable,
    sell_fn:              Callable,
    get_balances_fn:      Callable,
    update_portfolio_fn:  Callable = lambda pid, cfg: None,
    # backward-compat — unused
    get_status_fn:        Callable = lambda: {},
    get_history_fn:       Callable = lambda limit, portfolio_id=1: [],
    # SuperTrend
    st_start_fn:     Callable = lambda bid: None,
    st_stop_fn:      Callable = lambda bid: None,
    st_is_running_fn: Callable = lambda bid: False,
    st_create_fn:    Callable = lambda name, cfg: 0,
    st_get_fn:       Callable = lambda bid: None,
    st_list_fn:      Callable = lambda: [],
    st_update_fn:    Callable = lambda bid, cfg: None,
    st_delete_fn:    Callable = lambda bid: None,
    st_signals_fn:   Callable = lambda bid, limit=10: [],
    st_loop_info_fn: Callable = lambda bid: None,
) -> None:
    global _start_fn, _stop_fn, _rebalance_fn, _list_portfolios
    global _is_running_fn, _get_portfolio_fn, _save_portfolio_fn
    global _update_portfolio_fn, _buy_fn, _sell_fn, _get_balances_fn
    global _st_start_fn, _st_stop_fn, _st_is_running, _st_create_fn
    global _st_get_fn, _st_list_fn, _st_update_fn, _st_delete_fn
    global _st_signals_fn, _st_loop_info_fn

    _start_fn             = start_fn
    _stop_fn              = stop_fn
    _rebalance_fn         = rebalance_fn
    _list_portfolios      = list_portfolios_fn
    _is_running_fn        = is_running_fn
    _get_portfolio_fn     = get_portfolio_fn
    _save_portfolio_fn    = save_portfolio_fn
    _update_portfolio_fn  = update_portfolio_fn
    _buy_fn               = buy_fn
    _sell_fn              = sell_fn
    _get_balances_fn      = get_balances_fn

    _st_start_fn     = st_start_fn
    _st_stop_fn      = st_stop_fn
    _st_is_running   = st_is_running_fn
    _st_create_fn    = st_create_fn
    _st_get_fn       = st_get_fn
    _st_list_fn      = st_list_fn
    _st_update_fn    = st_update_fn
    _st_delete_fn    = st_delete_fn
    _st_signals_fn   = st_signals_fn
    _st_loop_info_fn = st_loop_info_fn

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set — bot disabled")
        return

    import asyncio
    import signal

    async def _main():
        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("menu",  cmd_start))
        app.add_handler(CommandHandler("done",  cmd_done))
        app.add_handler(CallbackQueryHandler(handle_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        log.info("Telegram bot polling started")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

    asyncio.run(_main())
