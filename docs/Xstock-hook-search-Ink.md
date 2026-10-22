# Поиск публичной оракульной стороны среди xStocks hooks Ink

Продолжение [первичной проверки площадок](Oracle-venues-Ink.md), 22 октября
2026 UTC. xChange оставлен на будущее по решению владельца; клиентский доступ
не использовался. Исходный Arbitr не изменялся.

## Результат

Публичный API эмитента полностью перечислил **1 474** актива с deployment на
Ink (15 страниц, последняя содержит 74 записи, `hasNextPage: false`, повторов
ID нет). На закреплённом блоке `59206934` проверены **1 836** обнаруженных
v4-пулов с их адресами. У **233** положительная текущая ликвидность; они связаны
с **38** ненулевыми hook-адресами. Доступны исходники либо proxy-описание
**18** адресов, **20** остаются неразобранными.
Пригодная публичная оракульная сторона среди разобранных вариантов пока
**не подтверждена**. Это не утверждение об отсутствии площадки во всей сети.

Scope — 2 951 адрес токенов/wrappers из полного ответа публичного API эмитента,
включая SPYx, NVDAx, AAPLx, TSLAx и CRCLx. Адреса не выбирались по символам из
AMM. Набор пулов взят из сохранённого discovery-checkpoint и двух свежих
страниц Initialize events, затем состояние прочитано заново. Положительная
`liquidity` в текущем тике не доказывает глубину или исполнимость нужного объёма.

Отдельно повторно прочитаны две страницы самых новых Initialize events
explorer: 100 событий, диапазон блоков `58991782–59197450`, с перекрытием
предыдущего checkpoint. Найден один дополнительный pool ID, но ни одного
нового пула этих пяти xStocks. [Ответ discovery сохранён](validation/xstock-hook-research/newest-initialize-pages.json.gz).
Это проверка опубликованного индекса explorer, не гарантия его отсутствующего
лага или полного покрытия всей сети.

[Полный pinned-отчёт](validation/xstock-hook-research/expanded-pinned.json.gz)
содержит определения пулов, состояния, runtime hashes и неразрешённые адреса.
Все **38** выбранных hook-runtime совпали с байткодом, полученным от explorer. Это привязка runtime,
а не независимая проверка компиляции опубликованных исходников.

## Что удалось разобрать

| Семейство | Наблюдение по опубликованному коду | Значение для стратегии |
| --- | --- | --- |
| SentryInkXStockFeeHook | Комиссия и reflections поверх launch-пула; при заданном `swapRouter` ограничивает отправителя swap, отдельно допускает Quoter | Положительный quote нельзя считать допуском нашего исполнителя. Внешняя исполнимая цена не подтверждена. |
| QuotronStockHook, два deployment | Обнуляет LP fee и берёт owner-steppable комиссию с USDG, ведёт terminal/LP pots | Комиссионный AMM hook; отдельная внешняя котировка не найдена. |
| LiquinkFeeHook, три deployment | Код задаёт 2% комиссии в paired asset, принимает exact-input | Не замена Caliber; объём и дополнительную плату нельзя игнорировать. |
| StocktopusV4FeeHook | Код задаёт сбор 1,5%, регистрацию пулов и graduation-проверку | Не подтверждённая оракульная площадка. |
| MemeowGuardHook, MasterLaunchHook / C, OctopadHook, FoundryHookInkG2 | Launch/fee/guard/liquidity механика | Публикация исходников не устанавливает наличие внешней исполнимой цены. |
| AegisHook | Dynamic fees, TWAP, limit orders и reinvest | TWAP наблюдает AMM; это не доказательство отдельного контрагента с запаздывающей внешней ценой. |
| StocksHook `0x6794f750f988cdf745895488d37b73f0f1dfaac4` | TWAMM orders и распределение output-fee между burn/treasury/protocol | Публикация кода не подтверждает оракульную внешнюю цену. |
| ERC1967Proxy → QuotedLimitInkyPumpHook | Explorer указывает implementation, опубликованы parent/handler sources с launch и limit-order механикой | Implementation-slot на pinned-блоке отдельно не проверен; оракульный контрагент не квалифицирован. |

Особенно важный пример —
[Sentry `0x18195c33d8150b2e7f5166fc79e29a6c2b1fb0cc`](https://explorer.inkonchain.com/address/0x18195c33d8150b2e7f5166fc79e29a6c2b1fb0cc?tab=contract).
В `_requireSwapSender` Quoter допускается отдельно от исполнителя. Это
конкретная причина проверять полный круг от нашего контракта, а не выдавать
успешный Quoter-ответ за торгуемый маршрут.

Для небольших неизвестных `0x26b86c03bd6576b8661d53a48d70faf8c85b8044`
и `0x84d3ce2227aa488d698a4d103c7f261cec50c044` опубликованных исходников
не найдено. Dispatcher selectors совместимы, среди них registry и fee-getters;
имена из signature database служат только подсказкой и могут иметь коллизии.
Это не основание считать контракты разобранными. Аналогично неизвестный
`0xf2b52a96554b3b12409110af54534841024a80cc` не квалифицирован по именам
fee-getters. [Проверка всех 20 неразобранных адресов в Sourcify](validation/xstock-hook-research/sourcify-unresolved.json)
вернула HTTP 404 для каждого; это не устанавливает назначение контрактов.
получить IPFS metadata через проверенный gateway не удалось (403).

## Данные и повторение

- [Полные страницы каталога эмитента](validation/xstock-hook-research/issuer-catalog-pages.json.gz),
  источник `https://api.xstocks.fi/api/v2/public/assets?network=Ink&pageSize=100&page={page}`.
- [Explorer contracts и дополнительные исходники](validation/xstock-hook-research/explorer-contracts.json.gz),
  источник `https://explorer.inkonchain.com/api/v2/smart-contracts/{address}`.
  Ответы включают как полную, так и частичную verification; неизвестные
  контракты сохранены с байткодом и без выдуманных source/name.
- [Отфильтрованный discovery](validation/xstock-hook-research/expanded-discovery.json.gz)
  явно помечен неполным. Он не заменяет новый chain-wide crawl.

```bash
/workspace/.venvs/arbitr/bin/python scripts/inspect_xstock_hooks.py \
  --discovery docs/validation/xstock-hook-research/expanded-discovery.json.gz \
  --issuer-assets docs/validation/xstock-hook-research/issuer-catalog-pages.json.gz \
  --explorer-archive docs/validation/xstock-hook-research/explorer-contracts.json.gz \
  --block-number 59206934 --output data/xstock-hooks.json
```

Скрипт сверяет PoolId с PoolKey, chain ID, неизменность hash блока и число
Multicall-ответов. Он не определяет пригодность контракта по имени и не выдаёт
положительную ликвидность или наличие исходников за oracle edge. Без
`--block-number` читает новый sealed-блок для того же набора pool IDs.

Дополнительный поиск по публичным каталогам/исходникам Swaap, DODO, BinaryFi,
Metric и Tristero не дал подтверждённого подходящего deployment на Ink.
У Tristero в прочитанном [списке chains адаптера](https://github.com/DefiLlama/dimension-adapters/blob/5710ba6f10002d89bdf6c5698e5d7c9d50e4d1d9/helpers/tristero.ts)
Ink отсутствует; это ограничение данного источника, а не доказательство
отсутствия протокола в сети. Найденный ранее Kaliber остаётся отдельным
подтверждённым deployment с неработавшими на проверенных блоках quotes.

Следующая содержательная проверка — оставшиеся неизвестные deployments и
новые адреса за пределами checkpoint. Исполнительный адаптер и live-торговля
не добавлялись: подходящая публичная сторона ещё не установлена.
