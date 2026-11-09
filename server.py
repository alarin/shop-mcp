# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "websockets>=13"]
# ///
"""shop MCP: search Avito / Ozon / AliExpress in the logged-in Chrome on the Mac mini.

Run over ssh as a stdio MCP server, or with --http as a streamable HTTP server on
127.0.0.1:8765 (published through a tunnel, see launchd/local.shop-http.plist).
Output is compact TSV to keep token use low.
No tool places orders or pays: Ozon and AliExpress stop at the cart.
Reviews: the *_leave_review tools post a review under the user's name.
Avito messenger: avito_send sends a message under the user's name.
"""
import functools
import importlib
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

import ali
import avito
import messenger
import orders
import ozon
import pics
import reviews
from shoplog import log

mcp = FastMCP("shop")

# A stdio server lives as long as the Claude session that started it, often for days, and kept
# running old code after a fix was committed (2026-09-27: ozon_orders saw 157 of 944 orders).
# So before every call, reload the modules whose files changed, dependencies first.
# Modules after a changed one are reloaded too: `from cdp import tab_for` in ozon.py keeps the
# old function until ozon itself is reloaded.
# shoplog is left alone: reloading it would add a second log handler.
# A new tool still needs a restart: tools are registered from server.py once, at start.
_RELOADABLE = ("cdp", "pics", "ali", "ozon", "avito", "messenger", "orders", "reviews")
_mtimes = {m: Path(sys.modules[m].__file__).stat().st_mtime for m in _RELOADABLE}
_reload_lock = threading.Lock()


def _reload_changed():
    with _reload_lock:
        changed = False
        for m in _RELOADABLE:
            mt = Path(sys.modules[m].__file__).stat().st_mtime
            if changed or mt != _mtimes[m]:
                importlib.reload(sys.modules[m])
                _mtimes[m] = mt
                changed = True
                log.info("reloaded %s.py", m)


def logged(fn):
    """Log every tool call to logs/shop.log: arguments, time, first line of the answer or the error."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t = time.time()
        call = f"{fn.__name__} {args or ''}{kwargs}"
        try:
            _reload_changed()
            out = fn(*args, **kwargs)
        except Exception:
            log.exception("%s failed in %.1f s", call, time.time() - t)
            raise
        # the *_item_images tools return [text, image, …]; log their text part
        text = out if isinstance(out, str) else "\n".join(x for x in out if isinstance(x, str))
        warn = any(w in text for w in ("⚠️", "блокировку", "проверка Авито"))
        log.log(30 if warn else 20, "%s ok in %.1f s, %d lines: %s",
                call, time.time() - t, text.count("\n") + 1, text.split("\n", 1)[0][:150])
        return out
    return wrapper


@mcp.tool()
@logged
def avito_search(query: str, region: str = "sankt-peterburg", price_min: int | None = None,
                 price_max: int | None = None, sort: str = "default", page: int = 1,
                 limit: int = 30, delivery: bool = False, shipping: int = 15) -> str:
    """Поиск на Авито под аккаунтом пользователя.

    region — часть адреса Авито: sankt-peterburg, moskva, all (вся Россия) и т.п.
    sort — default | date | price | price_desc. delivery=True — только с Авито Доставкой.
    shipping=N — для первых N объявлений с Авито Доставкой узнать цену доставки в город
    из профиля и посчитать «итого» (цена + самая дешёвая доставка); 0 — не узнавать.
    Это ~2.5 с на объявление, чаще Авито включает проверку безопасности. Сортировка Авито по цене доставку
    не учитывает — самое дешёвое с доставкой ищи по колонке «итого».
    Зарезервированные объявления из этих N скрываются (в выдаче поиска Авито резерв не виден;
    при shipping=0 не проверяется). Не советуй объявление, не проверив его через avito_item.
    Возвращает TSV: цена, доставка ₽, итого, название, город, срок доставки, рейтинг, дата, ссылка.
    """
    return avito.search(query, region, price_min, price_max, sort, page, limit, delivery, shipping)


@mcp.tool()
@logged
def avito_item(url: str) -> str:
    """Детали объявления Авито: резерв (строка «⛔ ЗАРЕЗЕРВИРОВАН» — не советовать), цена, доставка в город из профиля и итог, адрес, продавец, параметры, описание (до 2500 символов). Фотографии — avito_item_images."""
    return avito.item(url)


@mcp.tool(structured_output=False)
@logged
def avito_item_images(url: str, limit: int = 6) -> list:
    """Фотографии объявления Авито в порядке галереи, до 1280×960 px.

    url — полная ссылка или путь из avito_search. limit — сколько фото показать, от 1 до 20.
    Смотри фото для проверки состояния, комплектации, маркировки и размеров.
    Возвращает сами изображения; каждая фотография расходует токены.
    """
    return avito.images(url, limit)


@mcp.tool()
@logged
def avito_chats(limit: int = 20, unread_only: bool = False, before: int | None = None) -> str:
    """Чаты в мессенджере Авито, новые сверху. Чаты не отмечаются прочитанными.

    unread_only=True — только непрочитанные. before — значение из строки «следующая страница».
    Возвращает TSV: id чата (для avito_chat и avito_send), ● если не прочитан, когда, собеседник,
    объявление, цена, последнее сообщение («я:» — моё), путь объявления.
    """
    return messenger.chats(limit, unread_only, before)


@mcp.tool()
@logged
def avito_chat(chat_id: str, limit: int = 30, offset: int = 0) -> str:
    """Переписка в чате Авито по id из avito_chats или ссылке на чат: собеседник, объявление,
    сообщения от старых к новым («я» — мои, «Авито» — системные). Чат не отмечается прочитанным.
    offset — сколько последних сообщений пропустить, чтобы листать назад.
    """
    return messenger.chat(chat_id, limit, offset)


@mcp.tool()
@logged
def avito_send(text: str, chat_id: str | None = None, item_url: str | None = None) -> str:
    """Отправить сообщение на Авито от имени пользователя: в существующий чат (chat_id из avito_chats)
    или продавцу объявления (item_url — ссылка на объявление; чат откроется кнопкой «Написать сообщение»).
    Только текст, без фото и файлов.

    Перед вызовом покажи пользователю получателя и итоговый текст и дождись согласия.
    Не пиши от его имени того, чего он не просил: цены, обещания, договорённости — только с его слов.
    Ответ «отправлено» значит, что сообщение видно в истории чата. При «⚠️» сначала проверь
    чат через avito_chat и не отправляй повторно вслепую.
    """
    return messenger.send(text, chat_id, item_url)


@mcp.tool()
@logged
def ozon_search(query: str, price_min: int | None = None, price_max: int | None = None,
                sort: str = "score", limit: int = 30) -> str:
    """Поиск на Ozon под аккаунтом пользователя (доставка — на его адрес).

    sort — score (популярные) | new | price | price_desc | rating | discount.
    Возвращает TSV: sku, цена, цена без скидки, название, рейтинг, отзывы, когда привезут.
    """
    return ozon.search(query, price_min, price_max, sort, limit)


@mcp.tool()
@logged
def ozon_item(sku_or_url: str) -> str:
    """Карточка товара Ozon по sku или ссылке: цена, наличие, когда привезут, рейтинг, продавец
    с юрлицом и адресом (строка «⚠️ из-за рубежа», если продавец иностранный), все характеристики,
    описание (до 2500 символов) и блоки продавца вроде «Комплектация». Проверяй товар через неё,
    прежде чем советовать или класть в корзину. Комплектацию и размеры продавцы часто пишут только
    на картинках: их показывает ozon_item_images."""
    return ozon.item(sku_or_url)


@mcp.tool(structured_output=False)
@logged
def ozon_item_images(sku_or_url: str, limit: int = 6) -> list:
    """Картинки карточки Ozon: галерея по порядку, потом картинки из описания, около 1000 px.

    Смотри их, когда в тексте карточки не хватает комплектации, размеров или совместимости:
    продавцы часто пишут это только на картинках. Каждая картинка ~1,3 тыс. токенов, limit — сколько показать.
    """
    return ozon.images(sku_or_url, limit)


@mcp.tool()
@logged
def ozon_item_reviews(sku_or_url: str, sort: str = "useful", all_variants: bool = False,
                      query: str | None = None, limit: int = 20) -> str:
    """Отзывы покупателей на товар Ozon: оценка, дата, автор, сколько сочли полезным, фото, текст
    с достоинствами и недостатками. В шапке — общий рейтинг и сколько отзывов на каждую оценку.

    sort — useful (полезные) | low (сначала низкие оценки) | high. Жалобы ищи через low.
    all_variants=False — только этот вариант карточки. Продавцы склеивают разные товары
    под один рейтинг, тогда all_variants=True покажет и чужие отзывы с пометкой варианта.
    query — слова, которые все должны быть в отзыве, например «высота центра»; несколько
    вариантов через «|». С query листает до 300 отзывов и отдаёт подходящие.
    """
    return ozon.reviews(sku_or_url, sort, all_variants, query, limit)


@mcp.tool()
@logged
def ozon_add_to_cart(sku_or_url: str, quantity: int = 1) -> str:
    """Положить товар Ozon в корзину по sku или ссылке. Заказ не оформляет."""
    return ozon.add_to_cart(sku_or_url, quantity)


@mcp.tool()
@logged
def ozon_cart() -> str:
    """Содержимое корзины Ozon: sku, количество, цена, название, итог."""
    return ozon.cart()


@mcp.tool()
@logged
def ozon_remove_from_cart(sku_or_url: str) -> str:
    """Убрать товар из корзины Ozon по sku или ссылке."""
    return ozon.remove_from_cart(sku_or_url)


@mcp.tool()
@logged
def ali_search(query: str, price_min: int | None = None, price_max: int | None = None,
               sort: str = "default", limit: int = 30) -> str:
    """Поиск на AliExpress (aliexpress.ru) под аккаунтом пользователя.

    sort — default | orders (по числу покупок) | price | price_desc.
    Возвращает TSV: id, sku, цена, название, рейтинг, купили, «привезут до» (оценка),
    срок и цена доставки из карточки, магазин. Точные даты — в ali_item.
    """
    return ali.search(query, price_min, price_max, sort, limit)


@mcp.tool()
@logged
def ali_item(id_or_url: str, sku: str | None = None, list_variants: bool = True) -> str:
    """Товар AliExpress: цена, точные даты и цена доставки по способам, варианты (название, sku, цена).

    list_variants=False быстрее: не перебирает варианты.
    Описание и характеристики у Али обычно на картинках: их показывает ali_item_images.
    """
    return ali.item(id_or_url, sku, list_variants)


@mcp.tool(structured_output=False)
@logged
def ali_item_images(id_or_url: str, part: str = "all", limit: int = 6) -> list:
    """Картинки товара AliExpress, около 960 px: галерея, потом картинки описания.

    part — all | gallery | description. На картинках описания у Али обычно комплектация,
    размеры и характеристики. Каждая картинка ~1,2 тыс. токенов, limit — сколько показать.
    """
    return ali.images(id_or_url, part, limit)


@mcp.tool()
@logged
def ali_item_reviews(id_or_url: str, sort: str = "useful", query: str | None = None, limit: int = 20) -> str:
    """Отзывы покупателей на товар AliExpress: оценка, дата, автор, вариант товара, фото, текст
    и ответ продавца. Отзывы общие на все варианты карточки, вариант указан у каждого.

    sort — useful (полезные) | new (новые) | low (сначала низкие оценки). Жалобы ищи через low.
    query — слова, которые все должны быть в отзыве, например «S08K»; несколько вариантов
    через «|». С query листает до 80 отзывов (по 10 за запрос) и отдаёт подходящие.
    """
    return ali.reviews(id_or_url, sort, query, limit)


@mcp.tool()
@logged
def ali_add_to_cart(id_or_url: str, sku: str | None = None, options: dict[str, str] | None = None) -> str:
    """Положить товар AliExpress в корзину, 1 шт. Заказ не оформляет.

    Вариант задаётся sku (из ali_item) или options, например {"Цвет": "Silver"}.
    Возвращает выбранный вариант, цену, даты и цену доставки.
    """
    return ali.add_to_cart(id_or_url, sku, options)


@mcp.tool()
@logged
def ali_cart() -> str:
    """Корзина AliExpress: cart_id, выбран ли к оформлению, id товара, шт, цена, доставка, итог."""
    return ali.cart()


@mcp.tool()
@logged
def ali_remove_from_cart(cart_id_or_item_id: str) -> str:
    """Убрать строку из корзины AliExpress по cart_id или id товара. Выбор остальных строк сохраняется."""
    return ali.remove_from_cart(cart_id_or_item_id)


@mcp.tool()
@logged
def ozon_orders(query: str | None = None, year: int | None = None, limit: int = 30, max_new: int = 60) -> str:
    """История заказов Ozon с названиями товаров; query — слова, которые все должны быть в названии
    или варианте товара (без учёта регистра), например «вентилятор 5015». Несколько вариантов
    через «|» ищутся за один вызов: «usb хаб | разветвитель usb | hub» — не вызывай три раза.

    Идёт от новых к старым: текущие заказы, потом архив по годам (year — только этот год).
    Списки и заказы кэшируются: обычный вызов — секунды, повторный в течение 10 минут — мгновенно.
    max_new — предел новых загрузок за вызов: если он достигнут, в шапке будет сказано,
    и тот же вызов ещё раз продолжит поиск.
    Возвращает TSV: заказ, дата/статус, сумма заказа, товар, вариант, цена, ссылка.
    """
    return orders.ozon_orders(query, year, limit, max_new)


@mcp.tool()
@logged
def ali_orders(query: str | None = None, limit: int = 30, max_new: int = 60) -> str:
    """История заказов AliExpress (aliexpress.ru) с названиями товаров; query — слова, которые все
    должны быть в названии или варианте (без учёта регистра), например «улитка» или «blower 5015».
    Несколько вариантов через «|» ищутся за один вызов: «дисплей | экран | touch».

    Текущие заказы, потом архив. Списки и заказы кэшируются, повторный вызов в течение
    10 минут мгновенный. max_new — предел новых загрузок за вызов, повторный вызов продолжит.
    Возвращает TSV: заказ, «дата · статус», сумма заказа, товар, вариант, цена, ссылка.
    """
    return orders.ali_orders(query, limit, max_new)


@mcp.tool()
@logged
def ozon_reviews_waiting(limit: int = 30) -> str:
    """Купленные на Ozon товары, которые ждут отзыва: sku, название, вариант."""
    return reviews.ozon_waiting(limit)


@mcp.tool()
@logged
def ozon_leave_review(sku_or_url: str, rating: int, text: str, anonymous: bool = False,
                      accept_conditions: bool = False) -> str:
    """Оставить отзыв на купленный товар Ozon: rating 1–5, text до 3000 символов.
    Отзыв публикуется от имени пользователя. Оценку и смысл текста бери только у него:
    не придумывай впечатления и не ставь оценку сам. Перед вызовом покажи ему итоговый текст
    и оценку и дождись согласия. Фото не прикладываются.

    Если за отзыв на товар дают баллы, Ozon сначала показывает условия; тогда отзыв не уходит,
    а вернутся условия. Отправить всё равно — повторить с accept_conditions=True.
    Уже оставленный отзыв не редактирует.
    """
    return reviews.ozon_review(sku_or_url, rating, text, anonymous, accept_conditions)


@mcp.tool()
@logged
def ali_reviews_waiting(limit: int = 30) -> str:
    """Купленные на AliExpress товары, которые ждут отзыва: строка заказа (для ali_leave_review),
    id товара, название, вариант."""
    return reviews.ali_waiting(limit)


@mcp.tool()
@logged
def ali_leave_review(order_line_id: str, rating: int, text: str, anonymous: bool = False) -> str:
    """Оставить отзыв на купленный товар AliExpress по строке заказа из ali_reviews_waiting:
    rating 1–5, text до 10000 символов. Отзыв уходит на модерацию, её проходят за 48 часов.
    Отзыв публикуется от имени пользователя. Оценку и смысл текста бери только у него:
    не придумывай впечатления и не ставь оценку сам. Перед вызовом покажи ему итоговый текст
    и оценку и дождись согласия. Фото не прикладываются.
    """
    return reviews.ali_review(order_line_id, rating, text, anonymous)


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = 8765
        mcp.settings.stateless_http = True
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
