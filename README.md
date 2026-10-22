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
результат sealed-сверки и причину любой неполноты. Его можно использовать как
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
30 bps; DyorSwap остаётся inventory-only, пока его fork-specific fee не
подтверждён. v4 намеренно не включён в этот вывод: у
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

Этот баланс не является разрешённым размером сделки: оптимизатор должен взять
минимум из market-depth cap и PoolManager balance с запасом, повторно проверить
pending state и исключить fee-on-transfer/нестандартные токены.

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
с последних событий.

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
статическую fee, а также две ERC-20 currencies (не native `address(0)`);
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
