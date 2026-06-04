"""
Scraper de Falabella Perú.

Falabella usa Next.js con SSR: la página HTML embebe los resultados en
window.__NEXT_DATA__ (un JSON dentro de <script id="__NEXT_DATA__">).
El BFF interno (falabella-listing-bff-service) NO es público; no hay
ningún endpoint JSON accesible desde fuera — hay que parsear el HTML.

URL de búsqueda: https://www.falabella.com.pe/falabella-pe/search?Ntt=<keyword>&currentPage=<n>
Datos en:        __NEXT_DATA__ → props.pageProps.results  (array de productos)
Precios en:      product.prices[] → {type, crossed, price: ["199.90"]}

Solo se incluyen productos que tienen AMBOS precios:
  - normal_price: el precio tachado (crossed: true)  → referencia
  - offer_price:  el precio de oferta (crossed: false) → precio actual
"""

import json
import logging
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import FALABELLA_BASE_URL, MAX_PRODUCTS_PER_KEYWORD, TIENDAS_OFICIALES

logger = logging.getLogger(__name__)

_SEARCH_URL = f"{FALABELLA_BASE_URL}/falabella-pe/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache",
}


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(_HEADERS)
    return session


_session = _build_session()

_OFFICIAL_SELLERS: frozenset[str] = frozenset(TIENDAS_OFICIALES)


def _is_official_seller(seller_name: str) -> bool:
    """Retorna True si el vendedor pertenece a la lista de tiendas oficiales."""
    return seller_name.lower().strip() in _OFFICIAL_SELLERS


def _extract_next_data(html: str) -> dict | None:
    """Extrae el JSON de <script id="__NEXT_DATA__"> del HTML."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.error("Error parseando __NEXT_DATA__: %s", e)
        return None


def _parse_price_value(raw: list) -> float | None:
    """Convierte el array de precio ["199.20"] a float."""
    if not raw:
        return None
    try:
        value = float(str(raw[0]).replace(",", ""))
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None


def _extract_prices(prices: list) -> tuple[float | None, float | None]:
    """
    Extrae (normal_price, offer_price) de la lista de precios de Falabella.

    Retorna (None, None) si el producto no tiene precio de oferta,
    lo que indica que debe ignorarse (precio regular sin descuento).

    Falabella devuelve:
        {"type": "normalPrice",   "crossed": true,  "price": ["249"]}   → normal_price
        {"type": "internetPrice", "crossed": false, "price": ["199.20"]} → offer_price
    """
    normal_price: float | None = None
    offer_price: float | None = None

    for entry in prices:
        value = _parse_price_value(entry.get("price", []))
        if value is None:
            continue

        if entry.get("crossed", False):
            # Precio tachado = precio de referencia normal
            if normal_price is None or value > normal_price:
                normal_price = value
        else:
            # Precio activo = precio de oferta
            if offer_price is None or value < offer_price:
                offer_price = value

    return normal_price, offer_price


def search_top_discounts(n: int = 10) -> list[dict]:
    """
    Retorna los N productos con mayor % de descuento en Falabella Perú ahora.

    Estrategia: Falabella no expone un sort nativo por descuento, así que se
    busca el keyword 'oferta' (que devuelve ~48 productos con descuentos reales)
    y se ordena client-side por porcentaje de descuento descendente.

    Retorna lista de dicts con: product_id, name, url, normal_price, offer_price, discount_pct.
    """
    params = {"Ntt": "oferta", "currentPage": 1}

    try:
        response = _session.get(_SEARCH_URL, params=params, timeout=20)
        response.raise_for_status()
    except requests.HTTPError as e:
        logger.error("HTTP %s en search_top_discounts: %s", e.response.status_code, e)
        return []
    except requests.RequestException as e:
        logger.error("Error de red en search_top_discounts: %s", e)
        return []

    next_data = _extract_next_data(response.text)
    if not next_data:
        logger.warning("No se encontró __NEXT_DATA__ en search_top_discounts")
        return []

    try:
        raw_results: list = next_data["props"]["pageProps"]["results"]
    except (KeyError, TypeError):
        logger.warning("Estructura inesperada en __NEXT_DATA__ (search_top_discounts)")
        return []

    products: list[dict] = []

    for item in raw_results:
        product_id = str(item.get("productId", "")).strip()
        name = item.get("displayName", "Sin nombre").strip()
        url = item.get("url", "").strip()

        if not product_id:
            continue

        if not _is_official_seller(item.get("sellerName", "")):
            continue

        normal_price, offer_price = _extract_prices(item.get("prices", []))
        if normal_price is None or offer_price is None:
            continue

        if url and not url.startswith("http"):
            url = f"{FALABELLA_BASE_URL}{url}"

        discount_pct = round((normal_price - offer_price) / normal_price * 100)
        products.append({
            "product_id": product_id,
            "name": name,
            "url": url,
            "normal_price": normal_price,
            "offer_price": offer_price,
            "discount_pct": discount_pct,
        })

    products.sort(key=lambda p: p["discount_pct"], reverse=True)
    top = products[:n]
    logger.info(
        "search_top_discounts: %d con oferta y tienda oficial, top %d seleccionados.",
        len(products), len(top),
    )
    return top


def search_products(keyword: str) -> list[dict]:
    """
    Busca productos en Falabella Perú por keyword.

    Retorna lista de dicts con: product_id, name, url, normal_price, offer_price.
    Solo incluye productos que tienen AMBOS precios (oferta activa).
    """
    params = {"Ntt": keyword, "currentPage": 1}

    try:
        response = _session.get(_SEARCH_URL, params=params, timeout=20)
        response.raise_for_status()
    except requests.HTTPError as e:
        logger.error("HTTP %s buscando '%s': %s", e.response.status_code, keyword, e)
        return []
    except requests.RequestException as e:
        logger.error("Error de red buscando '%s': %s", keyword, e)
        return []

    next_data = _extract_next_data(response.text)
    if not next_data:
        logger.warning("No se encontró __NEXT_DATA__ para '%s'", keyword)
        return []

    try:
        raw_results: list = next_data["props"]["pageProps"]["results"]
    except (KeyError, TypeError):
        logger.warning("Estructura inesperada en __NEXT_DATA__ para '%s'", keyword)
        return []

    products: list[dict] = []

    for item in raw_results:
        product_id = str(item.get("productId", "")).strip()
        name = item.get("displayName", "Sin nombre").strip()
        url = item.get("url", "").strip()
        prices = item.get("prices", [])

        if not product_id:
            continue

        if not _is_official_seller(item.get("sellerName", "")):
            continue

        normal_price, offer_price = _extract_prices(prices)

        # Ignorar productos sin oferta activa (solo tienen un precio)
        if normal_price is None or offer_price is None:
            continue

        if url and not url.startswith("http"):
            url = f"{FALABELLA_BASE_URL}{url}"

        products.append({
            "product_id": product_id,
            "name": name,
            "url": url,
            "normal_price": normal_price,
            "offer_price": offer_price,
        })

        if len(products) >= MAX_PRODUCTS_PER_KEYWORD:
            break

    logger.info(
        "Falabella '%s': %d productos con oferta de %d totales.",
        keyword, len(products), len(raw_results),
    )
    return products
