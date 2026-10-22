# Ink: полный исторический v4 inventory и исследовательские сканы

Все файлы — публичные read-only данные, без ключей, подписания новых
транзакций или деплоя. `manifest.json` содержит SHA-256 сжатых и исходных
данных, блоки, границы наблюдения и счётчики.

- Explorer crawl: 93 страницы, 4603 Initialize/pool IDs, без ошибок decoding;
  captured history complete, старейший Initialize на блоке 6391727,
  новейший захваченный — 59187626. Это не подписка на новые пулы после начала crawl.
- StateView block 59196963: 4603 readable, 1152 с положительной текущей
  liquidity, 121 с ERC-20 currencies, нулевыми hooks и статической fee.
- AMM inventory block 59196936: исследовательский reserve-age threshold 86400
  секунд, чтобы не исключать редко торгуемые stable-пары. При котировании
  читается фактическое состояние выбранного sealed block.

| Scan | Block | Cycles | Complete quotes | Positive gross |
| --- | ---: | ---: | ---: | ---: |
| Two-step, четыре стартовых токена | 59197217 | 82 | 75 | 0 |
| WETH, 2–4 шага | 59197361 | 428 | 340 | 0 |
| USDt0/USDC/USDC.e, 2–4 шага | 59197505 | 1398 | 1243 | 0 |

Широкие WETH и stablecoin сканы вместе охватывают 1826 циклов и 1583 полные
котировки. Two-step scan пересекается с ними, поэтому его нельзя прибавлять
как дополнительные уникальные маршруты. Объём — 10^14 raw WETH или 10^6 raw
соответствующего stablecoin. Проверка этих объёмов на этих блоках не доказывает
отсутствие арбитража на всех размерах, в другие моменты или на других площадках.
Непрокотированные маршруты не признаются убыточными.

`subblocks.jsonl.gz` содержит 18 завершённых payload из 20-секундного capture:
18/18 прошли sealed-сверку, invalid=0. Исторический повтор 12 двухшаговых
WETH-котировок на блоке 59197217 точно совпал с исходными результатами.

## Восстановление и повтор

Из корня репозитория:

```bash
mkdir -p data/market-2026-10-22
for capture in docs/validation/market-2026-10-22/*.gz; do
  gzip -dc "$capture" > "data/market-2026-10-22/$(basename "$capture" .gz)"
done
python -m incy_arb scan-cycles \
  --inventory data/market-2026-10-22/inventory.json \
  --v4-discovery data/market-2026-10-22/v4-discovery.json \
  --v4-state data/market-2026-10-22/v4-state.json \
  --start-token 0x4200000000000000000000000000000000000006 \
  --amount 100000000000000 --max-steps 4 --block-number 59197361 \
  --output data/market-2026-10-22/weth-replay.json
```

Для точного повторения нужен RPC с историческим state. Без `--block-number`
сканер котирует на новом sealed block. Архивный universe остаётся снимком,
а не автоматически обновляемым торговым allowlist. DyorSwap сохраняется в
inventory, но не включается в котирование, пока его fee не подтверждена.
