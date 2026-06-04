"""
Bot de Telegram para monitorear precios de Falabella Perú.

Comandos:
  /start          - Mensaje de bienvenida
  /agregar <kw>   - Agrega un keyword a monitorear
  /eliminar <kw>  - Elimina un keyword
  /lista          - Lista todos los keywords activos
  /precios <kw>   - Muestra los productos en oferta registrados para un keyword
  /verificar      - Ejecuta una verificación manual de precios ahora
  /ayuda          - Muestra la ayuda
"""

import logging
from functools import wraps
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database as db
import scraper
import notifier

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _fmt_compact(price: float) -> str:
    """S/ 399 para enteros, S/ 399.90 si tiene centavos."""
    if price == int(price):
        return f"S/ {int(price):,}"
    return f"S/ {price:,.2f}"


def admin_only(handler):
    """Decorador que restringe un comando al ADMIN_CHAT_ID configurado en .env."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if config.ADMIN_CHAT_ID and str(update.effective_chat.id) != config.ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ No tienes permisos para usar este comando.")
            logger.warning(
                "Acceso denegado a /%s para chat_id=%s",
                handler.__name__.replace("cmd_", ""),
                update.effective_chat.id,
            )
            return
        return await handler(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Tarea de monitoreo (JobQueue integrado de PTB)
# ---------------------------------------------------------------------------

async def check_prices_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consulta Falabella para todos los keywords y notifica bajadas de oferta."""
    keywords = await db.get_all_keywords()
    if not keywords:
        logger.info("No hay keywords registrados.")
        return

    logger.info("Verificando %d keyword(s)...", len(keywords))

    for kw_row in keywords:
        chat_id: str = kw_row["chat_id"]
        keyword: str = kw_row["keyword"]
        keyword_id: int = kw_row["id"]

        if keyword == config.TOP_OFFERS_KEYWORD:
            # Modo especial: top ofertas filtradas por umbral de descuento
            all_top = scraper.search_top_discounts(n=50)
            products = [
                p for p in all_top
                if p["discount_pct"] >= config.TOP_OFFERS_MIN_DISCOUNT
            ]
        else:
            products = scraper.search_products(keyword)

        for product in products:
            change = await db.upsert_product(
                product_id=product["product_id"],
                keyword_id=keyword_id,
                name=product["name"],
                url=product["url"],
                normal_price=product["normal_price"],
                offer_price=product["offer_price"],
            )
            if change is None:
                continue

            # __top_ofertas__: notificar nuevos descuentos Y bajadas posteriores
            # Keywords normales: solo notificar cuando el precio baja (no en primera aparición)
            should_notify = (
                keyword == config.TOP_OFFERS_KEYWORD or not change["is_new"]
            )
            if should_notify:
                await notifier.notify_price_drop(
                    chat_id=chat_id,
                    product_name=product["name"],
                    url=product["url"],
                    normal_price=change["normal_price"],
                    offer_price=change["offer_price"],
                    keyword=keyword,
                )

    logger.info("Verificación completada.")


# ---------------------------------------------------------------------------
# Handlers de comandos
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 <b>Monitor de Precios Falabella Perú</b>\n\n"
        "Te aviso cuando productos en oferta bajen aún más de precio.\n\n"
        "📋 <b>Comandos:</b>\n"
        "/agregar &lt;keyword&gt; — Monitorear productos\n"
        "/eliminar &lt;keyword&gt; — Dejar de monitorear\n"
        "/lista — Ver keywords activos\n"
        "/precios &lt;keyword&gt; — Ver ofertas registradas\n"
        "/verificar — Revisar precios ahora\n"
        "/topofertas — Ver top 10 ofertas ahora\n"
        "/ayuda — Esta ayuda\n\n"
        "💡 <b>Tip:</b> Usa <code>/agregar __top_ofertas__</code> para recibir alertas automáticas\n"
        f"de productos con descuento ≥ {config.TOP_OFFERS_MIN_DISCOUNT}% cada {config.CHECK_INTERVAL_MINUTES} min."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


@admin_only
async def cmd_agregar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    keyword = " ".join(context.args).strip() if context.args else ""

    if not keyword:
        await update.message.reply_text(
            "⚠️ Debes especificar un keyword.\n"
            "Ejemplo: <code>/agregar laptop gaming</code>",
            parse_mode="HTML",
        )
        return

    added = await db.add_keyword(chat_id, keyword)

    if added:
        await update.message.reply_text(
            f"✅ Keyword <b>{keyword}</b> agregado.\n"
            f"Te avisaré cuando el precio de oferta baje.",
            parse_mode="HTML",
        )
        logger.info("Chat %s agregó keyword: '%s'", chat_id, keyword)
    else:
        await update.message.reply_text(
            f"ℹ️ El keyword <b>{keyword}</b> ya estaba en tu lista.",
            parse_mode="HTML",
        )


@admin_only
async def cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    keyword = " ".join(context.args).strip() if context.args else ""

    if not keyword:
        await update.message.reply_text(
            "⚠️ Debes especificar el keyword a eliminar.\n"
            "Ejemplo: <code>/eliminar laptop gaming</code>",
            parse_mode="HTML",
        )
        return

    removed = await db.remove_keyword(chat_id, keyword)

    if removed:
        await update.message.reply_text(
            f"🗑️ Keyword <b>{keyword}</b> eliminado.",
            parse_mode="HTML",
        )
        logger.info("Chat %s eliminó keyword: '%s'", chat_id, keyword)
    else:
        await update.message.reply_text(
            f"⚠️ No encontré el keyword <b>{keyword}</b> en tu lista.",
            parse_mode="HTML",
        )


@admin_only
async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    keywords = await db.get_keywords(chat_id)

    if not keywords:
        await update.message.reply_text(
            "📭 No tienes keywords registrados.\n"
            "Usa <code>/agregar &lt;keyword&gt;</code> para comenzar.",
            parse_mode="HTML",
        )
        return

    lines = "\n".join(
        f"  {i+1}. <code>{kw['keyword']}</code>" for i, kw in enumerate(keywords)
    )
    await update.message.reply_text(
        f"📋 <b>Keywords monitoreados ({len(keywords)}):</b>\n\n"
        f"{lines}\n\n"
        f"🕐 Verificación automática cada <b>{config.CHECK_INTERVAL_MINUTES} minutos</b>.",
        parse_mode="HTML",
    )


@admin_only
async def cmd_precios(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    keyword = " ".join(context.args).strip() if context.args else ""

    if not keyword:
        await update.message.reply_text(
            "⚠️ Especifica el keyword.\nEjemplo: <code>/precios zapatillas adidas</code>",
            parse_mode="HTML",
        )
        return

    products = await db.get_products_for_keyword(chat_id, keyword)

    if not products:
        await update.message.reply_text(
            f"ℹ️ No hay productos en oferta registrados para <b>{keyword}</b>.\n"
            "Usa <code>/verificar</code> para hacer una consulta ahora.",
            parse_mode="HTML",
        )
        return

    lines = []
    for p in products[:8]:
        normal = p["normal_price"]
        offer = p["offer_price"]
        pct = round((normal - offer) / normal * 100) if normal else 0
        name_short = p["name"][:65] + ("…" if len(p["name"]) > 65 else "")
        url = p["url"] or ""

        line = (
            f"🔥 <b>{name_short}</b>\n"
            f"   💰 Normal: S/ {normal:,.2f}  →  ✅ Oferta: S/ {offer:,.2f}  📉 {pct}%"
        )
        if url:
            line += f"\n   🔗 {url}"
        lines.append(line)

    await update.message.reply_text(
        f"🔍 Ofertas registradas para <b>{keyword}</b> ({len(products)} productos):\n\n"
        + "\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@admin_only
async def cmd_verificar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    keywords = await db.get_keywords(chat_id)

    if not keywords:
        await update.message.reply_text(
            "📭 No tienes keywords para verificar.\n"
            "Agrega uno con <code>/agregar &lt;keyword&gt;</code>.",
            parse_mode="HTML",
        )
        return

    msg = await update.message.reply_text(
        f"🔄 Verificando {len(keywords)} keyword(s)… por favor espera."
    )

    drops_found = 0

    for kw_row in keywords:
        keyword = kw_row["keyword"]
        keyword_id = kw_row["id"]
        products = scraper.search_products(keyword)

        for product in products:
            change = await db.upsert_product(
                product_id=product["product_id"],
                keyword_id=keyword_id,
                name=product["name"],
                url=product["url"],
                normal_price=product["normal_price"],
                offer_price=product["offer_price"],
            )
            if change and not change["is_new"]:
                drops_found += 1
                await notifier.notify_price_drop(
                    chat_id=chat_id,
                    product_name=product["name"],
                    url=product["url"],
                    normal_price=change["normal_price"],
                    offer_price=change["offer_price"],
                    keyword=keyword,
                )

    await msg.edit_text(
        f"✅ Verificación completada.\n"
        f"📦 Keywords revisados: <b>{len(keywords)}</b>\n"
        f"📉 Bajadas de precio encontradas: <b>{drops_found}</b>",
        parse_mode="HTML",
    )


async def cmd_topofertas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("🔍 Buscando las mejores ofertas de Falabella…")

    products = scraper.search_top_discounts(n=10)

    if not products:
        await msg.edit_text("⚠️ No se encontraron ofertas en este momento. Intenta más tarde.")
        return

    lines = ["🏆 <b>Top ofertas Falabella ahora</b>\n"]
    for i, p in enumerate(products, 1):
        lines.append(
            f"{i}. <b>{p['name']}</b>\n"
            f"   💰 {_fmt_compact(p['normal_price'])} → {_fmt_compact(p['offer_price'])} ({p['discount_pct']}% off)\n"
            f"   🔗 {p['url']}"
        )

    await msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "❓ No reconozco ese comando. Usa /ayuda para ver los disponibles."
    )


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------

async def post_shutdown(application: Application) -> None:
    await db.close_db()


async def post_init(application: Application) -> None:
    await db.init_db()

    await application.bot.set_my_commands([
        BotCommand("start", "Bienvenida e instrucciones"),
        BotCommand("agregar", "Agregar keyword a monitorear"),
        BotCommand("eliminar", "Eliminar keyword"),
        BotCommand("lista", "Ver keywords activos"),
        BotCommand("precios", "Ver ofertas registradas de un keyword"),
        BotCommand("verificar", "Verificar precios ahora"),
        BotCommand("topofertas", "Ver top 10 ofertas de Falabella ahora"),
        BotCommand("ayuda", "Mostrar ayuda"),
    ])

    application.job_queue.run_repeating(
        check_prices_job,
        interval=config.CHECK_INTERVAL_MINUTES * 60,
        first=60,
        name="price_check",
    )

    logger.info(
        "Bot listo. Verificación cada %d min. Token: ...%s",
        config.CHECK_INTERVAL_MINUTES,
        config.TELEGRAM_TOKEN[-6:],
    )


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    application = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("ayuda", cmd_ayuda))
    application.add_handler(CommandHandler("agregar", cmd_agregar))
    application.add_handler(CommandHandler("eliminar", cmd_eliminar))
    application.add_handler(CommandHandler("lista", cmd_lista))
    application.add_handler(CommandHandler("precios", cmd_precios))
    application.add_handler(CommandHandler("verificar", cmd_verificar))
    application.add_handler(CommandHandler("topofertas", cmd_topofertas))
    application.add_handler(MessageHandler(filters.COMMAND, handle_unknown))

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
