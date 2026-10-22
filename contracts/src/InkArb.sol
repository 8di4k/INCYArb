// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.28;

import {IERC20, IPoolManager, IV3Pool, IV2Pair, PoolKey, SwapParams} from "./Interfaces.sol";

/// @dev Чтение transient-хранилища PoolManager (Uniswap v4 Exttload): долг/кредит адреса по валюте.
interface IExttload {
    function exttload(bytes32 slot) external view returns (bytes32);
}

/// @title InkArb — read-only simulation executor derived from 8di4k/Arbitr XArb2.
/// @notice Packed route: token(20), amountIn(16), minProfit(16), maxBlock(4).
/// Steps: V3=1|pool(20)|tokenOut(20), V4=2|tokenOut(20)|fee(3)|spacing(3)|hooks(20),
/// V2=4|pair(20)|tokenOut(20)|feeBps(2). No adapter or native-currency steps.
/// Used only through eth_call state overrides; no deployment or signer is provided.
contract InkArb {
    uint8 internal constant V3 = 1;
    uint8 internal constant V4 = 2;
    uint8 internal constant V2 = 4;

    uint256 internal constant HEADER = 56;

    uint160 internal constant MIN_SQRT_PRICE = 4295128739;
    uint160 internal constant MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342;

    IPoolManager public immutable poolManager;

    // Раскладка хранилища как у XArb (слоты 0–2): бот подменяет их при симуляции до развёртывания.
    address public owner;
    address public beneficiary;
    bool public paused;
    mapping(address => bool) public isExecutor;

    bool transient inRoute;
    address transient expectedPool; // от кого ждём колбэк прямо сейчас
    address transient payToken; // чем платим в колбэке
    uint256 transient payMax; // больше этого в колбэке не платим

    event Executed(address indexed token, uint256 amountIn, uint256 profit);
    event Paused(bool paused, address by);
    event ExecutorSet(address indexed who, bool allowed);
    event BeneficiarySet(address indexed beneficiary);
    event OwnerSet(address indexed owner);

    error NotOwner();
    error NotExecutor();
    error IsPaused();
    error Expired();
    error Reentry();
    error Unauthorized();
    error BadCallback();
    error OverPay(uint256 asked, uint256 max);
    error CallbackMissing();
    error UnknownKind(uint8 kind);
    error BadRoute();
    error NativeNotSupported();
    error ProfitTooLow(uint256 profit, uint256 minProfit);
    error NegativeProfit(int256 delta);
    error TransferFailed(address token);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(IPoolManager _poolManager, address _owner, address _beneficiary) {
        poolManager = _poolManager;
        owner = _owner;
        beneficiary = _beneficiary;
        emit OwnerSet(_owner);
        emit BeneficiarySet(_beneficiary);
    }

    // ───────────────────────────── вход ─────────────────────────────

    /// @notice Маршрут — упакованные байты сразу после селектора (формат в описании контракта).
    /// @return profit прибыль в стартовом токене, уже отправленная на beneficiary
    function execute() external returns (uint256 profit) {
        if (!isExecutor[msg.sender]) revert NotExecutor();
        if (paused) revert IsPaused();
        if (msg.data.length <= 4 + HEADER) revert BadRoute();
        address token;
        uint256 amountIn;
        uint256 maxBlock;
        assembly ("memory-safe") {
            token := shr(96, calldataload(4))
            amountIn := shr(128, calldataload(24))
            maxBlock := shr(224, calldataload(56))
        }
        if (block.number > maxBlock) revert Expired();
        if (inRoute) revert Reentry();
        inRoute = true;
        profit = abi.decode(poolManager.unlock(msg.data[4:]), (uint256));
        inRoute = false;
        emit Executed(token, amountIn, profit);
    }

    /// @dev PoolManager вызывает это из unlock(). Принимаем только от него и только во время своего execute.
    function unlockCallback(bytes calldata raw) external returns (bytes memory) {
        if (msg.sender != address(poolManager) || !inRoute) revert Unauthorized();
        address token = _addr(raw, 0);
        if (token == address(0)) revert NativeNotSupported();
        uint256 amount = _uint(raw, 20, 128);

        address tokenIn = token;
        bool placed; // вход шага уже лежит на его адаптере/паре (туда его отправил предыдущий шаг)
        uint256 n = raw.length;
        uint256 p = HEADER;
        while (p < n) {
            uint8 kind = uint8(_uint(raw, p, 8));
            uint256 q = p + _stepLength(kind);
            if (q > n) revert BadRoute();

            // Куда отправить выход: паре следующего шага напрямую, иначе в PoolManager как кредит.
            address dest = address(poolManager);
            if (q < n) {
                uint8 next = uint8(_uint(raw, q, 8));
                if (next == V2) dest = _addr(raw, q + 1);
            }
            address tokenOut = _addr(raw, kind == V4 ? p + 1 : p + 21);
            if (tokenOut == address(0)) revert NativeNotSupported();

            if (kind == V3) amount = _v3(_addr(raw, p + 1), tokenIn, tokenOut, amount, dest);
            else if (kind == V4) amount = _v4(raw, p, tokenIn, tokenOut, amount, dest);
            else amount = _v2(raw, p, tokenIn, tokenOut, amount, dest, placed);

            tokenIn = tokenOut;
            placed = dest != address(poolManager);
            p = q;
        }
        if (tokenIn != token) revert BadRoute();

        // Кредит стартового токена = вернувшееся − взятое. Остальные токены PoolManager проверит сам.
        uint256 minProfit = _uint(raw, 36, 128);
        bytes32 slot;
        assembly ("memory-safe") {
            mstore(0, address())
            mstore(32, token)
            slot := keccak256(0, 64)
        }
        int256 delta = int256(uint256(IExttload(address(poolManager)).exttload(slot)));
        uint256 profit = delta > 0 ? uint256(delta) : 0;
        if (delta < 0) revert NegativeProfit(delta);
        if (profit < minProfit) revert ProfitTooLow(profit, minProfit);
        if (profit != 0) poolManager.take(token, beneficiary, profit);
        return abi.encode(profit);
    }

    // ───────────────────────────── шаги ─────────────────────────────

    /// @dev Вход — кредит в PoolManager: пул забирает оплату в колбэке через take. Выход — сразу на dest.
    function _v3(address pool, address tokenIn, address tokenOut, uint256 amount, address dest)
        internal
        returns (uint256 out)
    {
        bool zeroForOne = tokenIn < tokenOut;
        bool credit = dest == address(poolManager);
        expectedPool = pool;
        payToken = tokenIn;
        payMax = amount;
        if (credit) poolManager.sync(tokenOut);
        (int256 a0, int256 a1) = IV3Pool(pool).swap(
            dest, zeroForOne, int256(amount), zeroForOne ? MIN_SQRT_PRICE + 1 : MAX_SQRT_PRICE - 1, ""
        );
        if (expectedPool != address(0)) revert CallbackMissing();
        out = credit ? poolManager.settle() : uint256(-(zeroForOne ? a1 : a0));
    }

    /// @dev Кредит на кредит внутри PoolManager; переводов нет, кроме take, если следующий шаг — пара.
    function _v4(bytes calldata raw, uint256 p, address tokenIn, address tokenOut, uint256 amount, address dest)
        internal
        returns (uint256 out)
    {
        bool zeroForOne = tokenIn < tokenOut;
        PoolKey memory key = PoolKey(
            zeroForOne ? tokenIn : tokenOut,
            zeroForOne ? tokenOut : tokenIn,
            uint24(_uint(raw, p + 21, 24)),
            int24(uint24(_uint(raw, p + 24, 24))),
            _addr(raw, p + 27)
        );
        int256 delta = poolManager.swap(
            key, SwapParams(zeroForOne, -int256(amount), zeroForOne ? MIN_SQRT_PRICE + 1 : MAX_SQRT_PRICE - 1), ""
        );
        out = uint256(int256(zeroForOne ? int128(delta) : int128(delta >> 128)));
        if (dest != address(poolManager)) poolManager.take(tokenOut, dest, out);
    }

    function _v2(
        bytes calldata raw,
        uint256 p,
        address tokenIn,
        address tokenOut,
        uint256 amount,
        address dest,
        bool placed
    ) internal returns (uint256 out) {
        address pair = _addr(raw, p + 1);
        bool zeroForOne = tokenIn < tokenOut;
        if (!placed) poolManager.take(tokenIn, pair, amount);
        out = _v2Out(pair, tokenIn, zeroForOne, _uint(raw, p + 41, 16));
        bool credit = dest == address(poolManager);
        if (credit) poolManager.sync(tokenOut);
        (uint256 o0, uint256 o1) = zeroForOne ? (uint256(0), out) : (out, uint256(0));
        IV2Pair(pair).swap(o0, o1, dest, "");
        if (credit) out = poolManager.settle();
    }

    /// @dev Выход пары v2 на то, что на ней лежит сверх резерва (вход уже на паре).
    function _v2Out(address pair, address tokenIn, bool zeroForOne, uint256 feeBps) internal view returns (uint256) {
        (uint112 r0, uint112 r1,) = IV2Pair(pair).getReserves();
        (uint256 rIn, uint256 rOut) = zeroForOne ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
        uint256 inAfterFee = (IERC20(tokenIn).balanceOf(pair) - rIn) * (10_000 - feeBps);
        return inAfterFee * rOut / (rIn * 10_000 + inAfterFee);
    }

    function _stepLength(uint8 kind) internal pure returns (uint256) {
        if (kind == V3) return 41;
        if (kind == V4) return 47;
        if (kind == V2) return 43;
        revert UnknownKind(kind);
    }

    // ───────────────────────────── колбэки пулов v3-типа ─────────────────────────────

    function uniswapV3SwapCallback(int256 d0, int256 d1, bytes calldata) external {
        _payPool(d0, d1);
    }

    function pancakeV3SwapCallback(int256 d0, int256 d1, bytes calldata) external {
        _payPool(d0, d1);
    }

    function algebraSwapCallback(int256 d0, int256 d1, bytes calldata) external {
        _payPool(d0, d1);
    }

    function swapV3SwapCallback(int256 d0, int256 d1, bytes calldata) external {
        _payPool(d0, d1);
    }

    function dfsswapV3SwapCallback(int256 d0, int256 d1, bytes calldata) external {
        _payPool(d0, d1);
    }

    /// @dev Платим только пулу, который сами только что вызвали, только токеном входа, не больше суммы входа и один
    ///      раз. Платит PoolManager из нашего кредита (take): своих токенов у контракта нет.
    function _payPool(int256 d0, int256 d1) internal {
        if (msg.sender != expectedPool || msg.sender == address(0)) revert BadCallback();
        expectedPool = address(0);
        uint256 pay = uint256(d0 > 0 ? d0 : d1);
        uint256 max = payMax;
        if (pay > max) revert OverPay(pay, max);
        poolManager.take(payToken, msg.sender, pay);
    }

    // ───────────────────────────── управление ─────────────────────────────

    /// @notice Стоп-кран: остановить может владелец или любой исполнитель, снять — только владелец.
    function pause() external {
        if (msg.sender != owner && !isExecutor[msg.sender]) revert Unauthorized();
        paused = true;
        emit Paused(true, msg.sender);
    }

    function unpause() external onlyOwner {
        paused = false;
        emit Paused(false, msg.sender);
    }

    function setExecutor(address who, bool allowed) external onlyOwner {
        isExecutor[who] = allowed;
        emit ExecutorSet(who, allowed);
    }

    function setBeneficiary(address to) external onlyOwner {
        beneficiary = to;
        emit BeneficiarySet(to);
    }

    function setOwner(address to) external onlyOwner {
        owner = to;
        emit OwnerSet(to);
    }

    /// @notice Вывести то, что оказалось на контракте не через круг (ошибочный перевод и т.п.).
    function rescue(address token, address to, uint256 amount) external onlyOwner {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TransferFailed(token);
    }

    // ───────────────────────────── чтение маршрута ─────────────────────────────

    function _addr(bytes calldata raw, uint256 i) internal pure returns (address a) {
        assembly ("memory-safe") {
            a := shr(96, calldataload(add(raw.offset, i)))
        }
    }

    /// @dev Беззнаковое число из `bits` бит (кратно 8), начиная с байта i.
    function _uint(bytes calldata raw, uint256 i, uint256 bits) internal pure returns (uint256 v) {
        assembly ("memory-safe") {
            v := shr(sub(256, bits), calldataload(add(raw.offset, i)))
        }
    }
}
