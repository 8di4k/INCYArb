// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

interface FlashToken {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
}
interface FlashManager {
    function unlock(bytes calldata) external returns (bytes memory);
    function take(address, address, uint256) external;
    function sync(address) external;
    function settle() external payable returns (uint256);
}
interface FlashBico {
    function swap(address, address, uint256, uint256, address, uint256) external returns (uint256);
}
interface FlashBicoExecutor {
    function fill(address, address, address, uint256, address) external returns (uint256);
}

/// @notice Historical research harness. Only its code and an EOA gas balance may be overridden.
contract FlashPairProbe {
    error LegFailed(uint256 round, uint256 stage, bytes data);
    error Shortfall(uint256 balance, uint256 debt);
    error InsufficientProfit(uint256 actual, uint256 minimum);
    address constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address constant ELF = 0xf0f0F0F0FB0d738452EfD03A28e8be14C76d5f73;
    address constant BICO = 0x0000009E5F98cDB4dd1E1d2E15B220217c88B3b8;
    address constant EXECUTOR = 0x0000006f9bBA066e4efda2e0CAbE4FF8cf9030aC;
    FlashManager constant MANAGER = FlashManager(0x498581fF718922c3f8e6A244956aF099B2652b2b);

    struct Plan { uint256 amount; uint256 rounds; bool wethDebt; address maker; }

    function probe(Plan calldata plan, uint256 minimumProfit) external returns (
        uint256 profit, uint256 managerBefore, uint256 managerAfter, int256[] memory changes
    ) {
        require(plan.amount > 0 && plan.amount < 2**127 && plan.rounds > 0 && plan.rounds <= 8, "bounds");
        require(FlashToken(USDC).balanceOf(address(this)) == 0, "initial USDC");
        require(FlashToken(WETH).balanceOf(address(this)) == 0, "initial WETH");
        address asset = plan.wethDebt ? WETH : USDC;
        managerBefore = FlashToken(asset).balanceOf(address(MANAGER));
        changes = abi.decode(MANAGER.unlock(abi.encode(plan)), (int256[]));
        managerAfter = FlashToken(asset).balanceOf(address(MANAGER));
        require(managerAfter == managerBefore, "manager principal mismatch");
        require(FlashToken(plan.wethDebt ? USDC : WETH).balanceOf(address(this)) == 0, "residual intermediate");
        profit = FlashToken(asset).balanceOf(address(this));
        if (profit < minimumProfit) revert InsufficientProfit(profit, minimumProfit);
        uint256 recipientBefore = FlashToken(asset).balanceOf(msg.sender);
        require(FlashToken(asset).transfer(msg.sender, profit), "payout");
        require(FlashToken(asset).balanceOf(msg.sender) == recipientBefore + profit, "payout mismatch");
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(MANAGER), "manager only");
        Plan memory plan = abi.decode(data, (Plan));
        address asset = plan.wethDebt ? WETH : USDC;
        MANAGER.take(asset, address(this), plan.amount);
        require(FlashToken(asset).balanceOf(address(this)) == plan.amount, "take mismatch");
        require(FlashToken(USDC).approve(ELF, type(uint256).max), "approve");
        int256[] memory changes = new int256[](plan.rounds);
        for (uint256 i; i < plan.rounds; ++i) {
            uint256 beforeBalance = FlashToken(asset).balanceOf(address(this));
            if (plan.wethDebt) {
                sell(plan.amount, plan.maker, i);
                buy(FlashToken(USDC).balanceOf(address(this)), i);
            } else {
                buy(plan.amount, i);
                sell(FlashToken(WETH).balanceOf(address(this)), plan.maker, i);
            }
            changes[i] = int256(FlashToken(asset).balanceOf(address(this))) - int256(beforeBalance);
        }
        uint256 balance = FlashToken(asset).balanceOf(address(this));
        if (balance < plan.amount) revert Shortfall(balance, plan.amount);
        MANAGER.sync(asset);
        require(FlashToken(asset).transfer(address(MANAGER), plan.amount), "repay");
        require(MANAGER.settle() == plan.amount, "settlement");
        return abi.encode(changes);
    }

    function buy(uint256 amount, uint256 round) private {
        (bool ok, bytes memory data) = ELF.call(abi.encodeWithSelector(
            bytes4(0x598edcad), USDC, WETH, amount, 0, address(this), 0
        ));
        if (!ok) revert LegFailed(round, 1, data);
    }

    function sell(uint256 amount, address maker, uint256 round) private {
        require(amount > 0, "empty WETH");
        address target = maker == address(0) ? BICO : EXECUTOR;
        require(FlashToken(WETH).transfer(target, amount), "transfer");
        bytes memory payload = maker == address(0)
            ? abi.encodeCall(FlashBico.swap, (WETH, USDC, amount, 0, address(this), 0))
            : abi.encodeCall(FlashBicoExecutor.fill, (maker, WETH, USDC, amount, address(this)));
        (bool ok, bytes memory data) = target.call(payload);
        if (!ok) revert LegFailed(round, 2, data);
    }
}
