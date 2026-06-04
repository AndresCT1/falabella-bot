import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "falabella_prices.db")
MAX_PRODUCTS_PER_KEYWORD: int = int(os.getenv("MAX_PRODUCTS_PER_KEYWORD", "10"))
PRICE_DROP_THRESHOLD: float = float(os.getenv("PRICE_DROP_THRESHOLD", "0.0"))
TOP_OFFERS_MIN_DISCOUNT: int = int(os.getenv("TOP_OFFERS_MIN_DISCOUNT", "30"))  # % mínimo para alertas de __top_ofertas__
TOP_OFFERS_KEYWORD = "__top_ofertas__"

# Vendedores oficiales permitidos — comparación case-insensitive con sellerName del producto
TIENDAS_OFICIALES: list[str] = [
    s.lower().strip()
    for s in os.getenv(
        "TIENDAS_OFICIALES",
        "falabella,tottus,sodimac,banco falabella,fpay,marvel,hites",
    ).split(",")
]

ADMIN_CHAT_ID: str = os.getenv("ADMIN_CHAT_ID", "")

FALABELLA_BASE_URL = "https://www.falabella.com.pe"

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN no está configurado en el archivo .env")
