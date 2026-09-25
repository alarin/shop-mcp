# shop-mcp

MCP-сервер для поиска на Авито, Ozon и AliExpress под своим аккаунтом. Он управляет обычным Chrome, в котором вы уже вошли на эти сайты, и отдаёт результаты компактным TSV, а не снимками страниц. Так поиск стоит сотни токенов, а не десятки тысяч.

Что умеет:

- Авито: поиск с ценой доставки в ваш город и итоговой суммой, карточка объявления с адресом.
- Ozon: поиск, корзина (положить, посмотреть, убрать).
- AliExpress: поиск, карточка с датами и ценой доставки по вариантам, корзина.

Заказы сервер не оформляет и ничего не оплачивает: Ozon и AliExpress заканчиваются на корзине.

## как устроено

Chrome запущен с отдельным профилем и `--remote-debugging-port=9222` на 127.0.0.1. Сервер подключается к нему по DevTools Protocol, держит по одной вкладке на сайт и вызывает собственное API сайта через `fetch()` изнутри вкладки. Cookies и авторизация остаются в профиле Chrome, в коде их нет.

У меня Chrome и сервер живут на отдельном Mac mini, а Claude Code ходит к нему по ssh. Запускать можно и на той же машине.

## установка

1. Запустить Chrome с отдельным профилем и отладочным портом, например LaunchAgent `~/Library/LaunchAgents/local.shop-chrome.plist`:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0"><dict>
   <key>Label</key><string>local.shop-chrome</string>
   <key>ProgramArguments</key><array>
   <string>/Applications/Google Chrome.app/Contents/MacOS/Google Chrome</string>
   <string>--user-data-dir=/Users/YOU/Library/Application Support/shop-chrome</string>
   <string>--remote-debugging-port=9222</string>
   <string>--remote-debugging-address=127.0.0.1</string>
   <string>--no-first-run</string>
   <string>--restore-last-session</string>
   </array>
   <key>RunAtLoad</key><true/>
   <key>KeepAlive</key><true/>
   <key>LimitLoadToSessionType</key><string>Aqua</string>
   </dict></plist>
   ```

2. В этом Chrome войти на avito.ru, ozon.ru и aliexpress.ru и указать адрес доставки.
3. Положить репозиторий в `~/shop-mcp`. Нужен [uv](https://docs.astral.sh/uv/): зависимости описаны в заголовке `server.py`.
4. Подключить к Claude Code:

   ```sh
   # на той же машине
   claude mcp add shop -s user -- ~/shop-mcp/run.sh
   # на другой машине по ssh
   claude mcp add shop -s user -- ssh HOST ~/shop-mcp/run.sh
   ```

Лог вызовов пишется в `logs/shop.log`, тела неудачных ответов — в `logs/bodies/`. В них бывают ваши данные со страниц сайтов, поэтому `logs/` в `.gitignore`.

## ограничения

- Сайты меняют вёрстку и API, и тогда разбор ломается.
- Авито при частых запросах включает проверку и отвечает 439. Сервер делает паузы между запросами, но капчу иногда приходится проходить руками в этом Chrome.
- Регион Авито по умолчанию — `sankt-peterburg`, меняется параметром `region`.
- Используйте на свой страх и риск: автоматизация может противоречить правилам сайтов.
