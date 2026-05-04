"""
Telegram command handlers for the copy-trade engine.

Commands:
  /copy_status   — show engine status and stats
  /copy_start    — enable copy trading
  /copy_stop     — disable copy trading (keeps engine running, pauses execution)
  /copy_history  — last 10 copy trades
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from bot.telegram_bot import _is_allowed, _deny, send_notification

logger = logging.getLogger(__name__)

_copy_engine = None


def set_copy_engine(engine) -> None:
    global _copy_engine
    _copy_engine = engine


# ── Keyboards ──────────────────────────────────────────────────────────────────

def _copy_menu_kb(engine=None) -> InlineKeyboardMarkup:
    eng = engine or _copy_engine
    if eng and eng.enabled:
        toggle_label = "⏸ إيقاف مؤقت"
        toggle_cb    = "copy_pause"
    else:
        toggle_label = "▶️ تفعيل"
        toggle_cb    = "copy_resume"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 الحالة",       callback_data="copy_status_cb"),
         InlineKeyboardButton("📋 آخر الصفقات",  callback_data="copy_history_cb")],
        [InlineKeyboardButton(toggle_label,       callback_data=toggle_cb),
         InlineKeyboardButton("🏠 القائمة",       callback_data="menu:back")],
    ])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _status_text(engine=None) -> str:
    eng = engine or _copy_engine
    if eng is None:
        return "❌ محرك النسخ غير مُهيأ."

    state  = "🟢 يعمل" if eng.is_running() else "🔴 متوقف"
    active = "✅ مفعّل" if eng.enabled else "⏸ موقوف مؤقتاً"
    return (
        "🔁 *نسخ التجارة — BSC*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الحالة:       `{state}`\n"
        f"التنفيذ:      `{active}`\n"
        f"المحفظة:      `{eng.target_wallet[:10]}...`\n"
        f"حجم الصفقة:   `${float(eng.trade_usdt):.2f} USDT`\n"
        f"نسخ البيع:    `{'نعم' if eng.copy_sells else 'لا'}`\n"
        f"Slippage:     `3%`\n"
        f"Gas boost:    `+15%`\n"
    )


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_copy_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text(
        _status_text(),
        parse_mode="Markdown",
        reply_markup=_copy_menu_kb(),
    )


async def cmd_copy_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if _copy_engine is None:
        await update.message.reply_text("❌ محرك النسخ غير مُهيأ — تحقق من متغيرات البيئة.")
        return
    _copy_engine.enabled = True
    await update.message.reply_text(
        "✅ *تم تفعيل نسخ التجارة*\n"
        f"سيتم نسخ صفقات `{_copy_engine.target_wallet[:10]}...` تلقائياً.",
        parse_mode="Markdown",
        reply_markup=_copy_menu_kb(),
    )


async def cmd_copy_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if _copy_engine is None:
        await update.message.reply_text("❌ محرك النسخ غير مُهيأ.")
        return
    _copy_engine.enabled = False
    await update.message.reply_text(
        "⏸ *تم إيقاف نسخ التجارة مؤقتاً*\n"
        "المحرك لا يزال يراقب الـ mempool لكن لن ينفذ صفقات.\n"
        "استخدم /copy_start للاستئناف.",
        parse_mode="Markdown",
        reply_markup=_copy_menu_kb(),
    )


async def cmd_copy_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    try:
        from utils.db_manager import get_copy_trade_history
        trades = await get_copy_trade_history(limit=10)
    except Exception as exc:
        await update.message.reply_text(f"❌ خطأ في قراءة السجل: {exc}")
        return

    if not trades:
        await update.message.reply_text(
            "📋 لا توجد صفقات منسوخة بعد.",
            reply_markup=_copy_menu_kb(),
        )
        return

    lines = ["📋 *آخر الصفقات المنسوخة*\n━━━━━━━━━━━━━━━━━━━━"]
    for t in trades:
        side_icon = "🟢 شراء" if t["side"] == "buy" else "🔴 بيع"
        token = t["token_out"] if t["side"] == "buy" else t["token_in"]
        token_short = token[:10] + "..."
        ts = t["executed_at"].strftime("%m/%d %H:%M") if t.get("executed_at") else "—"
        lines.append(
            f"{side_icon} `{token_short}`\n"
            f"  💵 `${t['amount_in_usdt']:.2f}` | {ts}\n"
            f"  🔗 `{t['tx_hash'][:16]}...`"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_copy_menu_kb(),
    )


# ── Callback query handlers ────────────────────────────────────────────────────

async def copy_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_allowed(update):
        return await _deny(update)

    # Try bot_data first, fall back to module global
    engine = ctx.bot_data.get("copy_engine") if ctx and hasattr(ctx, "bot_data") else None
    if engine is not None and _copy_engine is None:
        set_copy_engine(engine)

    active_engine = engine or _copy_engine

    # Engine not configured — show setup instructions
    if active_engine is None:
        await query.edit_message_text(
            "🔁 *نسخ التجارة — BSC*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "❌ *المحرك غير مُفعَّل*\n\n"
            "تأكد من وجود هذه المتغيرات في Railway:\n"
            "• `BSC_HTTP_RPC_URL`\n"
            "• `MY_BSC_PRIVATE_KEY`\n"
            "• `BSCSCAN_API_KEY`\n"
            "• `COPY_TARGET_WALLET`\n\n"
            "ثم أعد تشغيل البوت.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:back")],
            ]),
        )
        return

    data = query.data

    if data == "copy_status_cb":
        await query.edit_message_text(
            _status_text(active_engine),
            parse_mode="Markdown",
            reply_markup=_copy_menu_kb(active_engine),
        )

    elif data == "copy_pause":
        active_engine.enabled = False
        await query.edit_message_text(
            "⏸ *تم إيقاف التنفيذ مؤقتاً*\nالمراقبة مستمرة.",
            parse_mode="Markdown",
            reply_markup=_copy_menu_kb(),
        )

    elif data == "copy_resume":
        active_engine.enabled = True
        await query.edit_message_text(
            "✅ *تم استئناف نسخ التجارة*",
            parse_mode="Markdown",
            reply_markup=_copy_menu_kb(),
        )

    elif data == "copy_history_cb":
        try:
            from utils.db_manager import get_copy_trade_history
            trades = await get_copy_trade_history(limit=10)
        except Exception as exc:
            await query.edit_message_text(f"❌ خطأ: {exc}")
            return

        if not trades:
            await query.edit_message_text(
                "📋 لا توجد صفقات منسوخة بعد.",
                reply_markup=_copy_menu_kb(),
            )
            return

        lines = ["📋 *آخر الصفقات المنسوخة*\n━━━━━━━━━━━━━━━━━━━━"]
        for t in trades:
            side_icon = "🟢 شراء" if t["side"] == "buy" else "🔴 بيع"
            token = t["token_out"] if t["side"] == "buy" else t["token_in"]
            token_short = token[:10] + "..."
            ts = t["executed_at"].strftime("%m/%d %H:%M") if t.get("executed_at") else "—"
            lines.append(
                f"{side_icon} `{token_short}`\n"
                f"  💵 `${t['amount_in_usdt']:.2f}` | {ts}\n"
                f"  🔗 `{t['tx_hash'][:16]}...`"
            )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_copy_menu_kb(),
        )


# ── Registration helper ────────────────────────────────────────────────────────

def register_copy_handlers(application) -> None:
    """Register all copy-trade command and callback handlers."""
    application.add_handler(CommandHandler("copy_status",  cmd_copy_status))
    application.add_handler(CommandHandler("copy_start",   cmd_copy_start))
    application.add_handler(CommandHandler("copy_stop",    cmd_copy_stop))
    application.add_handler(CommandHandler("copy_history", cmd_copy_history))
    application.add_handler(
        CallbackQueryHandler(copy_callback, pattern=r"^copy_"),
    )
    logger.info("Copy-trade handlers registered")


# ── Notification senders (injected into engine) ────────────────────────────────

async def notify_copy_buy(
    token: str,
    usdt_amount: float,
    tx_hash: str,
    my_entry: float | None = None,
    target_entry: float | None = None,
) -> None:
    # Price difference line
    price_line = ""
    if my_entry and target_entry and target_entry > 0:
        diff_pct = (my_entry - target_entry) / target_entry * 100
        if diff_pct > 0:
            price_line = f"📊 دخلت *بعده* بـ `+{diff_pct:.2f}%` (أغلى)\n"
        elif diff_pct < 0:
            price_line = f"📊 دخلت *قبله* بـ `{diff_pct:.2f}%` (أرخص) ✅\n"
        else:
            price_line = f"📊 نفس سعر الدخول\n"

    my_price_line    = f"💰 سعر دخولي:  `${my_entry:.8f}`\n"     if my_entry     else ""
    target_price_line = f"🎯 سعر دخوله:  `${target_entry:.8f}`\n" if target_entry else ""

    await send_notification(
        f"🟢 *نسخ شراء منفذ*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 العقد: `{token}`\n"
        f"💵 المبلغ: `${usdt_amount:.2f} USDT`\n"
        f"{my_price_line}"
        f"{target_price_line}"
        f"{price_line}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"[📈 GMGN](https://gmgn.ai/bsc/token/{token}) | "
        f"[🔍 BSCScan](https://bscscan.com/tx/{tx_hash})",
    )


async def notify_copy_sell(token: str, amount: float, tx_hash: str) -> None:
    await send_notification(
        f"🔴 *نسخ بيع منفذ*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 العقد: `{token}`\n"
        f"📦 الكمية: `{amount:.4f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"[📈 GMGN](https://gmgn.ai/bsc/token/{token}) | "
        f"[🔍 BSCScan](https://bscscan.com/tx/{tx_hash})",
    )


async def notify_copy_err(message: str) -> None:
    await send_notification(f"⚠️ *خطأ في نسخ التجارة*\n{message}")
