# INCYArb

Первый компонент переноса Arbitr на Ink — read-only observer. Он захватывает
subblocks, сохраняет исходные сообщения для дальнейшего replay и сверяет каждый
полностью увиденный payload с sealed блоком через HTTP RPC.

Observer не содержит private key, подписи, `eth_sendRawTransaction` или кода
исполнения сделок.

## Запуск

```bash
cd /workspace/INCYArb
/workspace/.venvs/arbitr/bin/python -m pip install -e '.[dev]'
/workspace/.venvs/arbitr/bin/python -m incy_arb observe --duration 60 \
  --output data/ink-subblocks.jsonl
```

Параметр `--duration 0` оставляет observer работать до `Ctrl-C`. Файл JSONL
содержит исходные stream messages, вычисленные keccak-хэши транзакций,
результат sealed-сверки транзакций/receipt logs и причину любой неполноты. Его можно использовать как
вход будущего replay, но запись с `verified: false` не годится как основа для
торгового решения.

## Гарантии текущего среза

- payload принимается только после увиденного `index = 0` с `base`;
- payload закрывается появлением следующего payload с `index = 0`, а не
  константой числа subblocks;
- отсутствие receipt, дубликат/пропуск индекса или смена block number делает
  запись невалидной;
- raw transaction сравнивается с sealed `transactions` через `keccak(raw)`;
- HTTP клиент реализует только read methods, нужные observer.

Inventory пулов, котировки и paper-engine будут следующими слоями поверх этих
проверенных captures.

## Inventory AMM

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb inventory \
  --output data/ink-pools.json
```

Команда закрепляет один block number и читает через Multicall3 фабрики
Velodrome V2, Slipstream, Uniswap V2 и DyorSwap. Для V2 сохраняются
`has_nonzero_reserves`, возраст последнего Sync и `recently_updated` (по
умолчанию — не старше часа); для Slipstream —
`has_positive_liquidity`. Эти поля не являются оценкой глубины, экономической
пригодности или гарантией quote parity. Uniswap V2 использует подтверждённые
30 bps; DyorSwap включается в research только для распознанного runtime hash
с проверенной 30 bps моделью, остальные реализации остаются inventory-only. v4 намеренно не включён в этот вывод: у
PoolManager нет перечисления пулов, поэтому для него нужен checkpointed crawler
`Initialize` events.

## Flash-accounting capacity

Arbitr финансирует круг через `Uniswap v4 PoolManager.unlock/take`, а не через
обычный Aave-style flash loan. Физический верхний предел `take` по валюте —
текущий ERC-20 баланс PoolManager. Его можно закрепить на sealed block:

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb flash-capacity \
  --token 0x0200c29006150606b650577bbe7b6248f58470c1 \
  --token 0x4200000000000000000000000000000000000006 \
  --output data/ink-flash-capacity.json
```

Этот баланс не является разрешённым размером сделки. Для внешнего первого шага
оптимизатор должен взять минимум из market-depth cap и PoolManager balance с
запасом, повторно проверить pending state и исключить
fee-on-transfer/нестандартные токены. Первый v4-шаг не переводит стартовый
токен через `take`, поэтому для него нужен отдельный анализ всех последующих
переводов и симуляция полного маршрута.
Вывод также учитывает жёсткий предел одной transient-дельты `2^127-1`.

Для локальной или on-chain quote-функции доступен
`incy_arb.sizing.optimize_size`. Он ограничивает диапазон market/risk/flash
лимитами, проверяет логарифмическую сетку и отдельно уточняет каждый найденный
локальный максимум. Это read-only расчёт; результат нужно подтвердить точной
симуляцией полного маршрута и полной стоимостью газа.

## Inventory v4 PoolManager

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb v4-inventory \
  --max-pages 10 --output data/ink-v4-pools.json
```

Команда обходит только страницы события `Initialize` PoolManager от новых к старым.
После каждой
страницы атомарно обновляется JSON checkpoint с `next_page_params`, поэтому
следующий запуск продолжает именно с сохранённого cursor. `complete: true`
означает, что доступная история дочитана; пока это `false`, файл является
частичным inventory и не может использоваться как полный universe пулов.
`--restart` удаляет только логическое продолжение crawl и начинает новый снимок
с последних событий. Для медленного explorer доступны `--timeout-seconds`
(по умолчанию 60) и `--attempts` (3): transport errors, HTTP 429/500/502/503/504
повторяют ту же страницу с ограниченной задержкой. Последний успешный checkpoint
сохраняется при исчерпании попыток. Ошибки 401/403/404, некорректный или
непродвигающийся cursor завершают crawl, не объявляя историю полной.

## Явный trade universe

```bash
cp config/ink-allowlist.example.json config/ink-allowlist.json
# заполняются только подтверждённые адреса токенов и пулов
/workspace/.venvs/arbitr/bin/python -m incy_arb universe \
  --inventory data/ink-pools.json \
  --policy config/ink-allowlist.json \
  --output data/ink-trade-universe.json
```

Пустой template намеренно даёт ноль candidates. Пул должен одновременно быть
разрешён отдельной записью, иметь обе стороны в token allowlist и пройти
проверку текущей ликвидности (а для V2 ещё и свежести reserves). Список
обнаруженных пулов сам по себе не становится торговым universe.

## Quote volatile V2

`quote-v2` реализует ровно формулу `_v2Out` из `Arbitr/contracts/src/XArb2.sol`
для volatile constant-product пары. Он берёт данные из закреплённого inventory и
разрешённого universe, поэтому результат — исследовательская оценка на
конкретном block, а не текущая цена и не торговая команда.

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb quote-v2 \
  --universe data/ink-trade-universe.json \
  --pool 0x... --token-in 0x... --amount-in 1000000
```

Stable V2, Slipstream и v4 не проходят через эту формулу: для них сначала
нужны их собственные инварианты/тиковая модель и проверка against on-chain quote.

Если нужен свежий расчёт с теми же policy checks, используйте `quote-v2-live`:

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb quote-v2-live \
  --universe data/ink-trade-universe.json \
  --pool 0x... --token-in 0x... --amount-in 1000000
```

Команда читает `getReserves()` и factory `getFee()` на одном sealed block. Она
не трактует это как симуляцию исполнения: transfer-tax, изменение состояния до
включения транзакции и газ не входят в результат. По умолчанию она отклоняет
reserves, у которых последний `Sync` старше часа; порог меняется
`--max-reserve-age-seconds`.

## Quote Slipstream

Для допущенного Slipstream-пула используется опубликованный на Ink Quoter,
сначала сверяя token sides, tick spacing и liquidity на том же sealed block:

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb quote-slipstream-live \
  --universe data/ink-trade-universe.json \
  --pool 0x... --token-in 0x... --amount-in 1000000
```

Ответ включает результат Quoter, число пересечённых инициализированных тиков и
его gas estimate. Это независимая опора для переноса CL-модели Arbitr, пока
пошаговая локальная модель тиков не прошла parity-проверку.

## Состояние v4

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb v4-state \
  --discovery data/ink-v4-pools.json --output data/ink-v4-state.json
```

Команда читает `StateView` на одном sealed block, а не предполагает layout
storage PoolManager. Для каждого уже обнаруженного pool ID сохраняются
`slot0`, liquidity, фактический LP fee и protocol fee. Флаг
`eligible_for_future_quote` требует положительную liquidity, zero-hook и
статическую fee; поддерживаются и native ETH currencies (`address(0)`);
это только техническая предпосылка для будущей котировки,
не разрешение на сделку. Для массового snapshot по умолчанию используется
один RPC request в секунду и повторяются временные transport/503 ошибки;
скорость можно явно менять `--requests-per-second`.
Для оперативного частичного среза есть `--max-pools`; JSON явно различает
`discovered_pool_ids` и `selected_pool_ids`.

## Поиск двухшаговых связок

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb scan-two-hop \
  --inventory data/ink-pools.json --amount 1000000 --amount 10000000
```

Команда находит две ликвидные Slipstream-пары с одинаковыми token sides и
котирует оба направления на одном sealed block. Результат показывает только
`gross_pnl` до газа и не является торговым сигналом.

Arbitr-style перечисление простых циклов длиной 2–4 запускается так:

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb scan-cycles \
  --inventory data/ink-pools.json --v4-discovery data/ink-v4-pools.json \
  --v4-state data/ink-v4-state.json \
  --start-token 0x0200c29006150606b650577bbe7b6248f58470c1 \
  --amount 100000 --max-steps 4
```

Как в Arbitr, DFS не повторяет площадку или промежуточный токен. Stable V2
котируется Router-ом, Slipstream и v4 — их on-chain Quoter-ами.


## Подбор объёма в сканере циклов

`--size-range TOKEN=MIN_RAW:MAX_RAW` включает поиск объёма отдельно для
стартового токена. Все значения задаются в минимальных единицах соответствующего
токена, например диапазон 0,1–100 USDt0 при 6 decimals:

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb scan-cycles \
  --inventory data/ink-pools.json --v4-discovery data/ink-v4-pools.json \
  --v4-state data/ink-v4-state.json \
  --start-token 0x0200c29006150606b650577bbe7b6248f58470c1 \
  --size-range 0x0200c29006150606b650577bbe7b6248f58470c1=100000:100000000 \
  --max-steps 4 --size-grid-points 17 --size-refinement-iterations 16
```

Диапазон заменяет `--amount`/`--start-amount` только для указанного токена.
Для остальных стартовых токенов сохраняется прежний режим. Верхняя граница —
явный исследовательский лимит пользователя, а не автоматически доказанная
глубина рынка. Для внешнего первого шага она дополнительно обрезается балансом
PoolManager; для первого v4-шага учитывается `int128`, без требования начального
`take`. Проверка всех последующих переводов и дельт остаётся задачей полной
симуляции исполнения.

Все котировки одного запуска закреплены на одном sealed block. Асинхронный
оптимизатор использует тот же алгоритм, что `optimize_size`, и кэширует каждый
исследованный объём; выбранная котировка вместе с quoter gas повторно берётся
из кэша. На цикл выполняется сетка из 17 точек и до 16 шагов уточнения каждого
видимого пика по умолчанию. Число RPC вызовов растёт с числом циклов, шагов и
видимых пиков; rate limit RPC клиента продолжает действовать.

В режиме подбора `findings` содержит лучший найденный вариант с положительным
**gross** PnL для каждого цикла. `sizing` сохраняет диагностическую запись и для
циклов без результата: диапазон, фактическую верхнюю границу, число котировок,
число ошибок и статус. `all_quotes_failed` означает отсутствие данных для вывода
о доходности. Пустой `findings` не доказывает отсутствие арбитража: сетка может
пропустить узкий пик, история v4 может быть неполной, а отдельные котировки не
моделируют взаимодействие нескольких v4-шагов внутри одного PoolManager.
Положительный gross PnL также не подтверждает прибыль после газа или
исполнимость маршрута.

## Полная симуляция цикла

Сохраните результаты нового fixed-amount или sizing scan с `--output data/scan.json`,
затем проверьте выбранный finding:

```bash
/workspace/.venvs/arbitr/bin/python -m incy_arb simulate-cycle \
  --scan data/scan.json --finding 0 --output data/simulation.json
```

В новом scan каждый finding содержит `simulation_route` с определениями шагов
и V2 fee, прочитанной на quote block. Старые captures без этого поля нужно
пересоздать. Симулятор принимает volatile V2, stable Velodrome V2, Slipstream и
v4 с нулевыми hooks и статической fee. Native ETH допускается только на v4-шаге; adapters отклоняются.

`InkArb` — производный от `Arbitr/XArb2` контракт без X Layer-адаптеров.
Сначала `eth_call` конструктора создаёт runtime с Ink PoolManager в immutable;
затем state overrides подставляют runtime, executor permission и ETH balance
двух фиктивных аккаунтов. Кодом getter-ов проверяются immutable и storage layout.
Рыночные контракты, токены, резервы и балансы PoolManager не подменяются.
Все вызовы используют quote block; его hash проверяется до и после расчёта.

`success` означает успешное гипотетическое исполнение всего круга; `gas_estimate`
— результат `eth_estimateGas` полного вызова, а не сумма оценок Quoter. Ошибки
RPC, ошибки оценки газа и reorg не превращаются в успешный результат.
Убыточный круг возвращает диагностический revert `NegativeProfit(int256)`:
`simulated_gross_pnl` сохраняет точный отрицательный результат, а `quote_difference`
показывает отклонение от последовательных котировок. Поддержки деплоя, ключей,
подписей и отправки транзакций нет. Для успешных вызовов рассчитывается отдельный бюджет полной комиссии,
описанный ниже.

Артефакт `src/incy_arb/artifacts/InkArb.json` содержит compiler/settings,
source hashes и проверенный storage layout. Пересборка:

```bash
python scripts/build_simulator.py /path/to/solc-0.8.28
forge test --root contracts
python -m pytest
```


## Полная комиссия и net PnL

Успешная `simulate-cycle` теперь возвращает `fee_budget`: gas limit с запасом
20% к полной оценке газа, L2-расход при fee cap, L1 data fee через
`GasPriceOracle.getL1FeeUpperBound` и operator fee через `getOperatorFee`.
Все параметры читаются на том же sealed block. Type-2 unsigned envelope
учитывает calldata, nonce, адрес, gas limit и fee caps. Oracle сам добавляет
68 байт на подпись; симулятор не добавляет их второй раз и ничего не подписывает.

По умолчанию max fee — удвоенная base fee закреплённого блока, priority fee — 0.
Параметры можно задать явно: `--gas-margin-bps`, `--max-fee-per-gas` и
`--priority-fee-per-gas` (последние два — wei за gas).
Это бюджет исследовательского расчёта; ставки и стоимость включения могут
измениться к реальной отправке. Верхняя оценка Fjord compression статистическая,
а не абсолютная гарантия для произвольных данных.

`net_profit_assessment` вычитает ETH-расход только из результата с начальным ETH или WETH
(1 WETH = 1 ETH). Для USDt0 и других валют возвращается
`start_token_valuation_required`: без проверенной оценки валюты положительный
net PnL не объявляется. Недоступность fee oracle или оценки газа тоже исключает
такое заключение. Убыточный откат не получает оценку газа успешной сделки.
Оценка не включает деплой, подготовку капитала или внешний вывод средств.


## Stable Velodrome V2 в симуляции

Для stable-пары InkArb использует отдельный packed kind `5 | pool(20) | tokenOut(20)`.
Он читает фактически поступивший в пару input (`balanceOf - reserveIn`) и
вызывает её `getAmountOut(input, tokenIn)`. Так используется invariant,
нормализация decimals и текущая fee самой пары. Формула volatile V2 и её
упакованная fee к этому шагу не применяются. Выход может сразу передаваться
на следующий V2/stable V2 пул; финальная прибыль и все дельты по-прежнему
проверяются внутри одного `PoolManager.unlock`.

`ink_execution_compatible` отражает поддержку текущим InkArb. Поле
`xarb2_execution_compatible` сохраняет совместимость с исходным X Layer XArb2,
который stable kind не поддерживает. Доступность этого шага расширяет
исследовательское покрытие и не заменяет token/pool allowlist для торговли.


## Повторные котировки и контроль блока

Цикловой сканер использует общий кэш независимых `eth_call` на закреплённом
блоке (до 20000 записей). Совпадающие первые шаги разных циклов не требуют
повторных запросов. Успешные ответы и execution reverts кэшируются, transport
ошибки остаются повторяемыми. RPC-клиент не повторяет детерминированные reverts,
неподдерживаемые методы и неверные параметры как временные ошибки сети.

Кэш не используется для pending/latest и для полного исполнения со state
overrides. Scan проверяет chain ID и hash блока до/после, сохраняет
`quote_block_hash` и статистику `quote_cache`. Симулятор отклоняет scan, если
его hash больше не соответствует блоку. Замена блока требует пересканирования.

## Воспроизводимый рыночный capture

Полный исторический v4 inventory, StateView snapshot, AMM inventory,
сканы и свежий subblock capture сохранены в
[docs/validation/market-2026-10-22](docs/validation/market-2026-10-22/README.md).
Данные сжаты gzip; manifest содержит SHA-256 исходных и сжатых файлов.
`scan-cycles --block-number N` позволяет повторить котирование на конкретном
историческом sealed block (нужен RPC с historical state). Без этого параметра
сохраняется поиск на новом sealed block.

## Локальная CL-математика и проверка округления

`cl-parity` загружает tick bitmap и liquidity net/gross вокруг текущего тика
Slipstream или static zero-hook v4 на блоке сохранённого scan, затем сравнивает
локальный exact-input расчёт с on-chain Quoter. Объёмы задаются отдельно в raw
units token0 и token1:

```bash
python -m incy_arb cl-parity \
  --scan data/market-2026-10-22/scan-two-hop.json --finding 12 --step 0 \
  --amount0 1000 --amount1 1000 --words-around 3 \
  --output data/cl-parity.json
```

Математика перенесена из `Arbitr/bot/xarb_bot/v3math.py`. Tick iteration сохраняет
и пустые границы bitmap words, поскольку округление SwapMath происходит
на каждом таком шаге. Также учтены Solidity overflow fallback для token0 input,
направленная protocol fee v4 и границы загруженного диапазона. Для v4 ticks
используются методы StateView, без предположения о storage layout PoolManager.

При неполном покрытии, исчерпании liquidity или неподдержанной fee локальная
quote возвращает `None`; результат обозначается `not_fully_modeled` и не
экстраполируется. Quoter может вернуть выход частично исполненного swap при
большом запрошенном input — текущая локальная модель принимает только полные
fills. `quoter_error` и `difference` выделены отдельно. Проверка hash до/после
и соответствие scan block обязательны.

На block 59198585 проверены 18 пулов (9 Slipstream + 9 v4) в двух направлениях:
130 точных совпадений, 0 различий, 14 Quoter errors. Эти 130 независимых
on-chain значений включены в регрессионные тесты. Capture:
[cl-parity-59198585.json](docs/validation/cl-parity-59198585.json).
Это база для локального поиска по subblocks; обновление модели по их логам
остаётся следующим слоем.


## Проверка receipt logs перед replay

Observer по умолчанию сверяет receipts через `eth_getBlockReceipts`: status,
type, cumulativeGasUsed, порядок и содержимое логов каждого raw transaction.
Проверяются transaction index, номер/hash блока и неизменность sealed hash
после чтения receipts. Лишние receipts без raw transaction, неполные данные,
ошибка RPC или заменённый блок исключают `verified: true`.

`receipt_verification` хранит отдельный результат и счётчики. Для будущего
replay логов требуется именно `receipt_verification.verified`, а не только
совпадение transaction hashes. Старые captures без этого поля не подтверждают
логи. `--skip-receipt-verification` явно оставляет только hash/header сверку;
такой capture сохраняет `requested: false, verified: false` для receipts.

Свежая проверка blocks 59199202–59199220: 19 payload, 69 transactions,
74 logs — все 19 прошли полную сверку. Сжатый capture и SHA-256 summary:
[verified-receipts-2026-10-22.json](docs/validation/verified-receipts-2026-10-22.json).


## Native ETH в v4

Currency `0x0000000000000000000000000000000000000000` обозначает ETH, не ERC-20.
Поиск включает native v4 по умолчанию. `--exclude-native-v4` сохраняет прежний
ERC-20-only universe, в том числе при повторении старых архивных сканов.
В полном captured snapshot native поддержка добавляет 186 активных
zero-hook/static-fee пулов к прежним 121 ERC-20 пулам.

```bash
python -m incy_arb scan-cycles \
  --inventory data/market-2026-10-22/inventory.json \
  --v4-discovery data/market-2026-10-22/v4-discovery.json \
  --v4-state data/market-2026-10-22/v4-state.json \
  --start-token 0x0000000000000000000000000000000000000000 \
  --amount 100000000000000 --max-steps 2 --output data/native-scan.json
```

Баланс native PoolManager читается через `eth_getBalance`; `flash-capacity`
также принимает zero currency и показывает ETH с 18 decimals. Начальный v4
swap создаёт дельту без физического займа, включая native currency. Native
промежуточные суммы остаются внутри PoolManager между v4-шагами; V2/V3 шаги
работают только с ERC-20. Финальная native прибыль уходит непосредственно на
beneficiary. Executor не получает ETH, wrap/unwrap шаги не добавлялись.
Полный hypothetical вызов по-прежнему не требует деплоя или подписи.


## Локальный подбор объёма v4

`local-v4-scan` загружает ограниченное окно CL ticks на одном sealed block,
строит native/ERC-20 v4 циклы и подбирает raw input логарифмической сеткой
с уточнением локальных максимумов. Независимые локальные leg quotes кэшируются
с ограничением памяти. Неполные fills и выход за загруженный диапазон отвергаются.
Это ограниченный поиск; отсутствие кандидатов не доказывает отсутствие арбитража.

```bash
python -m incy_arb local-v4-scan \
  --v4-discovery data/market-2026-10-22/v4-discovery.json \
  --v4-state data/market-2026-10-22/v4-state.json \
  --min-amount 1000000000 --max-amount 100000000000000000 \
  --max-steps 3 --validate-top 3 --output data/local-v4-scan.json
```

По умолчанию стартовая currency — native ETH, nominal fee cap — 10000 pips
(1%), размер — в raw units стартовой currency. `--currency ADDRESS` можно
повторять для явного ограничения графа; стартовую currency тоже нужно включить.
`--block-number` закрепляет исторический блок. Snapshot содержит все локальные
данные для replay. Верхние `--validate-top` кандидаты проходят полную InkArb
симуляцию и консервативный fee budget. Только совпавшая EVM quote с положительным
результатом после бюджета учитывается в `simulated_positive_after_budget`.

На block 59200457 подбор нашёл ETH→USDt0→ETH с input 2464973735522 wei:
полная EVM симуляция подтвердила gross +2508382745 wei точно, но net после
бюджета комиссии составил −1188693246 wei. На block 59201263 поиск по 44 пулам
и 128 циклам занял 0,095 секунды CPU после загрузки snapshots: 8 gross-кандидатов,
3 успешные полные симуляции, 0 положительных результатов после fee budget.
Артефакты: [summary](docs/validation/local-v4-sized-59201263.json),
[первый EVM результат](docs/validation/native-positive-before-fees-59200457.json).
Время CPU не включает RPC, загрузку ticks и EVM проверку.


## Replay v4 pricing state

```bash
python -m incy_arb v4-replay \
  --scan docs/validation/local-v4-sized-59201263.json.gz --blocks 20 \
  --output data/v4-replay.json
```

Команда читает полный набор canonical receipts для 1–128 последовательных
sealed blocks после scan. Проверяются parent hashes, transaction set/order,
receipt metadata и глобальный порядок logs. Swap обновляет price/tick/liquidity;
ModifyLiquidity — active liquidity и tick net/gross; ProtocolFeeUpdated —
направленные fees. Нулевая LP delta не создаёт ticks; net=0 с gross>0 сохраняется.
Блок применяется атомарно. Пропуск блока, reorg или некорректное событие прерывают
replay. Окно bitmap остаётся исходным; quote за его границами отвергается.

Итог каждого пула независимо сравнивается со StateView на конечном блоке,
включая исходное окно ticks. `differences` означает несовпадение, а не успех.
JSON сохраняет исходные snapshots, полные blocks/receipts и обе конечные модели.
Поддерживаются только static zero-hook v4. Это проверка sealed state;
обработка preconfirmation latency и поиск между subblocks ещё не подключены.

Capture [v4-replay-59201002](docs/validation/v4-replay-59201002.json) подтверждает
2 Swap; [v4-replay-59201183](docs/validation/v4-replay-59201183.json) — 1 Swap и
40 сборов LP fees с нулевой liquidity delta;
[v4-replay-59100910](docs/validation/v4-replay-59100910.json) — реальное изменение
ликвидности. Во всех случаях конечная pricing model точно совпала со StateView.

Расширенный size scan с fee cap 999999 pips на block 59201725 охватил 140 пулов
и 862 цикла за 0,603 секунды CPU: 17 gross-кандидатов, 5 точных успешных EVM
симуляций, 0 положительных после верхнего fee budget. Snapshot:
[local-v4-allfees-59201725](docs/validation/local-v4-allfees-59201725.json).


## Оценка L1 fee со сжатием calldata

`simulate-cycle` и проверка кандидатов сохраняют два расчёта комиссии:
основной `total_fee_budget_wei` использует `getL1FeeUpperBound`, дополнительный
`content_based_estimate` — `getL1Fee(bytes)` для точного unsigned type-2 envelope.
Oracle сам добавляет 68 bytes signature allowance в обоих методах. Подпись не
создаётся. Дополнительный расчёт использует тот же padded gas limit, max fee
и operator fee; serialized envelope сохраняется для проверки destination,
nonce и calldata. Ошибка дополнительного метода не стирает основной budget.

`net_profit_assessment.status` и счётчик `simulated_positive_after_budget`
по-прежнему опираются на верхнюю оценку. Вложенный `content_based_estimate`
показывает более узкую оценку отдельно. Гипотетический адрес executor может
сжиматься лучше реального; nonce, calldata и цены включения тоже влияют на fee.

На block 59200457 при gross +2508382745 wei: L1 upper 3622335691 wei против
content estimate 2050105091 wei. Net с исходным simulation address составляет
+383537354 wei по content estimate и −1188693246 wei по upper budget.
Три тестовых адреса и nonce 0/2^32 дают +177082831…+288250651 wei, всего
0,177…0,288 gwei расчётного остатка. Это крайне малая историческая величина;
реальный доход, доступность включения и окупаемость deployment не подтверждены.
Артефакты: [EVM и оба fee расчёта](docs/validation/native-content-fees-59200457.json),
[чувствительность к адресу и nonce](docs/validation/native-fee-sensitivity-59200457.json).


## DyorSwap: runtime-scoped 30 bps

Inventory распознаёт известную DyorSwap pair реализацию по Keccak runtime:
`0xe21efb434c9e1a20cdd4d9df255bcade55c20e175c5d23b27f7fc3290ca96ef1`.
Её invariant использует `balance * 10000 - amountIn * 30`. Code читается
на том же sealed block; только непустые recent pairs с этим hash получают
`fee_bps=30`. Неизвестный runtime остаётся без fee и исключается из research.
Новый inventory проверяет chain ID/hash и сохраняет block hash.

`scan-cycles` и `scan-three-hop` принимают venue `dyorswap_v2`, повторно проверяя
runtime на quote block. `simulate-cycle` проверяет его ещё раз и использует
существующий V2 kind=4. Отдельный contract adapter не добавлялся.
Это research support; token transfer taxes и нестандартные transfers требуют
полной симуляции. Политика торгового allowlist автоматически не расширяется.
Старые inventory без runtime evidence нужно обновить через `inventory`.

На block 59202817 два основных DyorSwap пула имеют одинаковый подтверждённый
runtime. 11 full-route EVM результатов в обоих направлениях и трёх размерах
точно совпали с расчётом; они убыточны. Три отрицательных контроля с 29 bps
отклонились с `Dyor: K`. Недостаточное funding в одном дополнительном размере
не засчитано как подтверждение. Артефакт:
[dyor-fee-parity-59202817](docs/validation/dyor-fee-parity-59202817.json).
Свежий inventory block 59203033: 447 DyorSwap pairs, 19 recent/nonzero;
все 19 имеют распознанный runtime:
[dyor-inventory-59203033](docs/validation/dyor-inventory-59203033.json).

Обычный `scan-cycles` на block 59203114 проверил 22 двухшаговых WETH цикла
при input 10^14 wei, включая 10 с DyorSwap: все 22 получили quote,
положительных gross нет. Capture: `docs/validation/dyor-inclusive-twohop-59203114.json[.gz]`.


## Replay по границам subblocks

```bash
python -m incy_arb observe --duration 120 --output data/capture.jsonl
python -m incy_arb subblock-v4-replay \
  --capture data/capture.jsonl --v4-discovery data/market-2026-10-22/v4-discovery.json \
  --output data/subblock-v4-replay.json
```

Команда выбирает pre-existing static zero-hook v4 пулы с pricing events в
capture и загружает initial state на блоке перед первым payload. Capture должен
содержать 1–256 полных последовательных блоков; порядок завершения observer
tasks сортируется, а пропуски/дубликаты запрещены. Raw payload каждый раз
пересобирается, transaction hashes/header и receipt logs заново проверяются
по canonical RPC. Сохранённые `verified` флаги не заменяют эту проверку.

Логи внутри каждой границы применяются в порядке raw transactions, затем
receipt logs. Итоговая модель каждого блока продвигается атомарно. Prefix
pricing records не получают sealed block tag: частичный префикс не является
состоянием всего блока. Конечный price/tick/liquidity/fees/bitmap независимо
сравнивается со StateView в исходном tick window. Hooks, dynamic fee, неизвестные
keys и инициализированные во время capture пулы перечисляются в исключениях.

Реальный capture blocks 59203421–59203460: 40 blocks, 240 subblocks,
2 Swap поддерживаемого пула, точное совпадение конечного состояния:
[subblock-v4-replay-59203420](docs/validation/subblock-v4-replay-59203420.json).
Предыдущее окно 118 полностью verified blocks не содержало supported pricing
переходов: события 17 v4 pool IDs относились к hooks/dynamic fee. Оно сохранено
отдельно: [subblock-coverage-59203230](docs/validation/subblock-coverage-59203230.json).

Это post-hoc реконструкция после полной canonical проверки; она не измеряет
время получения preconfirmation и не подтверждает исполнение сделки на префиксе.
Команда проверяет state replay, поиск торговых кандидатов между границами ещё
не подключён. Captures с gaps требуют нового непрерывного окна.


## Paper search на границах subblocks

```bash
python -m incy_arb paper-subblock-search \
  --baseline docs/validation/subblock-search-baseline-59203420.json.gz \
  --replay docs/validation/subblock-v4-replay-59203420.json.gz \
  --output data/paper-subblock-search.json
```

Это offline gross-only расчёт по сохранённым evidence. Baseline должен иметь
тот же start block/hash и покрывать все tracked replay pools. Initial и final
pricing state сверяются с replay evidence, payload/receipts перепроверяются
локально; сетевой проверки в этой команде нет. Файлы следует получать через
проверяющие команды `local-v4-scan` и `subblock-v4-replay`.

Первый поиск считает весь граф; затем пересчитываются только циклы с изменёнными
pricing pools. Leg cache создаётся заново для каждого состояния. Сохраняются
новые/исчезнувшие кандидаты, обновлённые размеры и timeline каждой границы.
Нулевые изменения pricing не вызывают нового поиска. Prefix-модели не
используются как sealed state для EVM-симуляции.

На сохранённом окне: 44 пула, 128 циклов, 240 границ; два изменения потребовали
пересчёта 22 циклов каждое. Было 8 gross-кандидатов, стало 9; локальный проход
занял 0,167 секунды CPU. Пик 10809806094 wei пришёлся на последнюю границу
block 59203443. Поэтому для него отдельно выполнена полная EVM-симуляция
именно sealed block: gross совпал точно, gas estimate 109143, верхний полный
fee budget 2558558811 wei, расчётный net **+8251247283 wei ETH**.
Это всего 0,000000008251247283 ETH в историческом гипотетическом вызове;
живое включение, deployment costs и реальный доход не подтверждены.

```bash
python -m incy_arb simulate-cycle \
  --scan docs/validation/paper-peak-scan-59203443.json \
  --output data/paper-peak-evm.json
```

Артефакты: [paper summary](docs/validation/subblock-paper-search-59203420.json),
[независимая EVM проверка пика](docs/validation/paper-peak-evm-59203443.json).
Остальные prefix-кандидаты остаются без EVM/fee подтверждения: эта одна проверка
не распространяется на состояния внутри незавершённых блоков.


## Локальные arrival и verification timings

Новые `observe` captures сохраняют `message_timings` отдельно от raw messages:
UTC receive time и monotonic receive time фиксируются сразу после `ws.recv`,
до JSON parse и canonical RPC checks. Поле `timing` разделяет промежутки между
arrivals, span payload, ожидание semaphore, verification work и время от
последнего arrival до окончания проверки. Timing не меняет valid/verified flags.

```bash
python -m incy_arb observe --duration 20 --output data/timed.jsonl
python -m incy_arb capture-timing --capture data/timed.jsonl \
  --output data/timing-summary.json
```

Старым captures arrival times не дорисовываются. Monotonic clock используется
для интервалов, UTC — только для отдельного сравнения с header timestamp.
Отрицательная разница с header допустима: сюда входят clock skew и расписание
block timestamps. Это не измерение one-way network latency или гарантированного
времени sequencer emission. Summary показывает median/p95 и число samples.

Live sample: 15/15 полностью verified payloads, median gap 0,1955 s,
median payload arrival span 0,8054 s. Canonical verification завершалась через
median 2,6054 s после последнего arrival, maximum 27,1120 s. Verification work
median 2,0128 s, maximum 26,3993 s; в него входят RPC, retries, rate limiting
и локальная сверка. Это небольшой sample публичного endpoint в текущей среде.
Артефакт: [observer-timing-2026-10-22](docs/validation/observer-timing-2026-10-22.json).

Для поиска коротких окон полная проверка после sealed block даёт существенную
задержку. Текущий paper replay остаётся post-hoc; отдельный provisional receive
path с последующей canonical сверкой ещё предстоит подключить.
