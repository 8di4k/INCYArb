# xChange на Ink: найден через исполненную сделку

22 октября 2026 UTC. Продолжение
[исследования реальных исполнителей](Competitor-reverse-engineering-Ink.md).

**Установлен конкретный AtomicSwap deployment и исполненный RFQ на Ink.**
Это подтверждение технической стороны xChange, а не найденный публичный
аналог Caliber и не доказанный прибыльный круг. Подключение клиента остаётся
отложенным по решению владельца.

## Сделка и адреса

[Транзакция `0x1bb1…4fa8`](https://explorer.inkonchain.com/tx/0x1bb1515bd93b0141a103a7686db4a882e495d20f9872ebe825df505a43c24fa8),
блок **59203341**, hash
`0xb46d352dd66dfbcf694d4fcaffd851fb4146e96d609f8979e353b88cf9219de7`.
Она не содержит стандартных AMM Swap events и была найдена дополнительным
фильтром token flows. Root вызов — CoW Settlement; это покупка, а не
подтверждённый замкнутый арбитраж.

| Назначение | Адрес |
| --- | --- |
| CoW Settlement | `0x9008d19f58aabd9ed0d60971565aa8510560ab41` |
| Integration contract, payer и получатель RFQ | `0x9fc428fde25c5c062c2c9cce0be18a1fbec45a42` |
| AtomicSwap proxy | `0x788d911ae7c95121a89a0f0306db65d87422e1de` |
| Implementation из фактического DELEGATECALL | `0x3122466e4cba040b3bf8113b663ade10c5229b03` |
| Кошелёк эмитента из официального public system-wallets API | `0x5f7a4c11bde4f218f0025ef444c369d838ffa2ad` |

Вызов proxy с selector **`0xf28e37bd`** декодирован по опубликованному
ABI implementation как `executeSwap(SwapMessage,bytes,Permit)`:

- incoming: integration contract → кошелёк эмитента,
  **10000000 raw USDC**, то есть **10 USDC**;
- outgoing: кошелёк эмитента → integration contract,
  **26724605611632686 raw TSLAx**, то есть **0,026724605611632686 TSLAx**;
- permit пустой: использован allowance-путь;
- quote ID `0x60d18e233fa31548b9cec7082c5f8518f0ce7a2705271909b759f4808ae9d376`.

Integration contract затем передаёт TSLAx в settlement. Конечный пользователь
получает меньше, чем RFQ выдаёт integration contract; эти величины нельзя
подменять друг другом при расчёте цены и расходов.

## Доступ и подтверждение

В [опубликованном коде implementation](https://explorer.inkonchain.com/address/0x3122466e4cba040b3bf8113b663ade10c5229b03?tab=contract)
`executeSwap` проверяет pause, expiration, usedQuotes, EIP-712 signer и вызывает
`_verifyAllowlist` для from/to обеих передач. Все четыре адресных поля входят
в подписанное сообщение. Скопированная calldata не даёт котировку на другие
адреса; использованный quote повторно не исполняется.

Отдельными read-only RPC-запросами на блоке сделки подтверждены:

- успешный canonical receipt и неизменность hash блока;
- байткод proxy (1167 байт) и implementation (12392 байта) совпадает с
  байткодом explorer; hashes сохранены в архиве;
- `allowedAddresses(integration contract) == true` и
  `allowedAddresses(issuer wallet) == true`;
- `usedQuotes(quoteId) == true` на конец блока.

DELEGATECALL в public explorer trace связывает proxy с implementation в этой
транзакции. Трасса не была самостоятельно пересчитана EVM; implementation-slot
отдельно не читался. Исходники и ABI получены от explorer, независимая
компиляция здесь не выполнялась.

## Что это даёт переносу

Сняты две прежние неопределённости: есть действующий deployment и пример
синхронного обмена с контрактом как плательщиком/получателем. Остаются допуск
нашего исполнителя, доступ к RFQ, spread/лимиты, wrap/unwrap и EVM-проверка
полного flash-funded круга с net после расходов.

[Архив доказательств](validation/competitor-traces/observed-xchange.json.gz)
содержит trace, explorer metadata, ABI/source ответов, декодированное сообщение,
официальный список system wallets и canonical RPC receipt/code/allowlist reads.
Приватные котировки не запрашивались, подписи и транзакции не создавались,
чужой integration contract для обхода допуска не использовался.
