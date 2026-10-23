// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
import "../flash-funded-oracle/FlashPairProbe.sol";

/// @notice Diagnostic eth_call harness, not a production trading executor.
contract FlashPairSearch {
    error LegFailed(uint256 stage, bytes data);
    error Shortfall(uint256 balance, uint256 debt);
    error InsufficientProfit(uint256 actual, uint256 minimum);
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant BTC = 0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf;
    address constant ELF = 0xf0f0F0F0FB0d738452EfD03A28e8be14C76d5f73;
    address constant BICO = 0x0000009E5F98cDB4dd1E1d2E15B220217c88B3b8;
    address constant EXECUTOR = 0x0000006f9bBA066e4efda2e0CAbE4FF8cf9030aC;
    FlashManager constant MANAGER = FlashManager(0x498581fF718922c3f8e6A244956aF099B2652b2b);
    struct Plan { address debt; address middle; uint256 amount; bool elfFirst; address maker; }

    function probe(Plan calldata plan, uint256 minimumWeth) external returns (
        uint256 profitWeth, uint256 profitDebt, uint256 managerBefore, uint256 managerAfter
    ) {
        require(supported(plan.debt) && supported(plan.middle) && plan.debt != plan.middle, "assets");
        require(plan.amount > 0 && plan.amount < 2**127, "amount");
        require(FlashToken(USDC).balanceOf(address(this)) == 0, "initial USDC");
        require(FlashToken(WETH).balanceOf(address(this)) == 0, "initial WETH");
        require(FlashToken(BTC).balanceOf(address(this)) == 0, "initial BTC");
        managerBefore = FlashToken(plan.debt).balanceOf(address(MANAGER));
        profitDebt = abi.decode(MANAGER.unlock(abi.encode(plan)), (uint256));
        managerAfter = FlashToken(plan.debt).balanceOf(address(MANAGER));
        require(managerAfter == managerBefore, "manager principal mismatch");
        if (plan.debt != WETH) {
            require(FlashToken(plan.debt).approve(ELF, type(uint256).max), "conversion approval");
            elf(plan.debt, WETH, profitDebt, 3);
        }
        profitWeth = FlashToken(WETH).balanceOf(address(this));
        require(FlashToken(USDC).balanceOf(address(this)) == 0, "residual USDC");
        require(FlashToken(BTC).balanceOf(address(this)) == 0, "residual BTC");
        if (profitWeth < minimumWeth) revert InsufficientProfit(profitWeth, minimumWeth);
        uint256 beforePayout = FlashToken(WETH).balanceOf(msg.sender);
        require(FlashToken(WETH).transfer(msg.sender, profitWeth), "payout");
        require(FlashToken(WETH).balanceOf(msg.sender) == beforePayout + profitWeth, "payout mismatch");
    }
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(MANAGER), "manager only");
        Plan memory p = abi.decode(data, (Plan));
        MANAGER.take(p.debt, address(this), p.amount);
        require(FlashToken(p.debt).balanceOf(address(this)) == p.amount, "take mismatch");
        if (p.elfFirst) {
            require(FlashToken(p.debt).approve(ELF, type(uint256).max), "approval");
            elf(p.debt, p.middle, p.amount, 1);
            bico(p.middle, p.debt, FlashToken(p.middle).balanceOf(address(this)), p.maker, 2);
        } else {
            bico(p.debt, p.middle, p.amount, p.maker, 1);
            require(FlashToken(p.middle).approve(ELF, type(uint256).max), "approval");
            elf(p.middle, p.debt, FlashToken(p.middle).balanceOf(address(this)), 2);
        }
        require(FlashToken(p.middle).balanceOf(address(this)) == 0, "residual middle");
        uint256 balance = FlashToken(p.debt).balanceOf(address(this));
        if (balance < p.amount) revert Shortfall(balance, p.amount);
        MANAGER.sync(p.debt);
        require(FlashToken(p.debt).transfer(address(MANAGER), p.amount), "repay");
        require(MANAGER.settle() == p.amount, "settle mismatch");
        return abi.encode(balance - p.amount);
    }
    function supported(address token) private pure returns (bool) { return token == WETH || token == USDC || token == BTC; }
    function elf(address tokenIn, address tokenOut, uint256 amount, uint256 stage) private {
        (bool ok, bytes memory result) = ELF.call(abi.encodeWithSelector(
            bytes4(0x598edcad), tokenIn, tokenOut, amount, 0, address(this), 0
        ));
        if (!ok) revert LegFailed(stage, result);
    }
    function bico(address tokenIn, address tokenOut, uint256 amount, address maker, uint256 stage) private {
        require(amount > 0, "empty leg");
        address target = maker == address(0) ? BICO : EXECUTOR;
        require(FlashToken(tokenIn).transfer(target, amount), "transfer");
        bytes memory payload = maker == address(0)
            ? abi.encodeCall(FlashBico.swap, (tokenIn, tokenOut, amount, 0, address(this), 0))
            : abi.encodeCall(FlashBicoExecutor.fill, (maker, tokenIn, tokenOut, amount, address(this)));
        (bool ok, bytes memory result) = target.call(payload);
        if (!ok) revert LegFailed(stage, result);
    }
}
