import logging
from telegram import Bot
from telegram.error import TelegramError
from config import TELEGRAM_TOKEN

logger = logging.getLogger(__name__)

_bot = Bot(token=TELEGRAM_TOKEN)


def _fmt(price: float) -> str:
    return f"S/ {price:,.2f}"


def _discount_pct(normal: float, offer: float) -> int:
    if normal <= 0:
        return 0
    return round((normal - offer) / normal * 100)


async def notify_price_drop(
    chat_id: str,
    product_name: str,
    url: str,
    normal_price: float,
    offer_price: float,
    keyword: str,
) -> None:
    """
    Envía alerta de bajada de precio con el formato acordado:

        🔥 Zapatillas Urbanas Hombre Adidas Break Start
        💰 Precio normal: S/ 249.00
        ✅ Precio oferta: S/ 199.20
        📉 Descuento: 20%
        🔗 https://...
    """
    pct = _discount_pct(normal_price, offer_price)

    lines = [
        f"🔥 <b>{product_name}</b>",
        f"💰 Precio normal: {_fmt(normal_price)}",
        f"✅ Precio oferta: {_fmt(offer_price)}",
        f"📉 Descuento: {pct}%",
    ]
    if url:
        lines.append(f"🔗 {url}")

    message = "\n".join(lines)

    try:
        await _bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(
            "Alerta enviada a %s: '%s' → oferta %s (-%d%% de %s)",
            chat_id, product_name, _fmt(offer_price), pct, _fmt(normal_price),
        )
    except TelegramError as e:
        logger.error("Error enviando alerta a %s: %s", chat_id, e)


async def send_message(chat_id: str, text: str, parse_mode: str = "HTML") -> None:
    """Envía mensaje genérico."""
    try:
        await _bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        logger.error("Error enviando mensaje a %s: %s", chat_id, e)
