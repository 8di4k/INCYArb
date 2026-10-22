// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.28;

import {InkArb} from "../src/InkArb.sol";
import {IPoolManager, PoolKey, SwapParams} from "../src/Interfaces.sol";

interface Vm {
    function prank(address) external;
    function deal(address,uint256) external;
    function expectRevert(bytes4) external;
    function expectRevert(bytes calldata) external;
}

contract Token {
    mapping(address => uint256) public balanceOf;
    function mint(address who, uint256 amount) external { balanceOf[who] += amount; }
    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

// Deterministic accounting mock. Tracks each caller/currency delta and rejects unsettled unlocks.
contract Manager {
    mapping(bytes32 => int256) public delta;
    bytes32[] private touched;
    address private synced;
    uint256 private syncedBalance;
    bool private unlocked;
    function key(address caller, address currency) public pure returns (bytes32) {
        return keccak256(abi.encode(caller, currency));
    }
    function account(address token, int256 change) internal {
        bytes32 slot = key(msg.sender, token);
        touched.push(slot);
        delta[slot] += change;
    }
    function unlock(bytes calldata data) external returns (bytes memory result) {
        require(!unlocked, "locked");
        unlocked = true;
        result = InkArb(msg.sender).unlockCallback(data);
        for (uint256 i; i < touched.length; ++i) require(delta[touched[i]] == 0, "unsettled");
        delete touched;
        unlocked = false;
    }
    function take(address token, address to, uint256 amount) external {
        require(unlocked, "locked");
        account(token, -int256(amount));
        if (token==address(0)) {
            (bool ok,)=payable(to).call{value:amount}("");
            require(ok,"native transfer");
        } else Token(token).transfer(to, amount);
    }
    function sync(address token) external {
        synced = token;
        syncedBalance = Token(token).balanceOf(address(this));
    }
    function settle() external payable returns (uint256 amount) {
        amount = Token(synced).balanceOf(address(this)) - syncedBalance;
        account(synced, int256(amount));
        synced = address(0);
    }
    function exttload(bytes32 slot) external view returns (bytes32) { return bytes32(uint256(delta[slot])); }
    function swap(PoolKey calldata pool, SwapParams calldata params, bytes calldata) external returns (int256) {
        uint256 amount = uint256(-params.amountSpecified);
        address input = params.zeroForOne ? pool.currency0 : pool.currency1;
        address output = params.zeroForOne ? pool.currency1 : pool.currency0;
        account(input, -int256(amount));
        account(output, int256(2 * amount));
        int128 a0 = params.zeroForOne ? -int128(int256(amount)) : int128(int256(2 * amount));
        int128 a1 = params.zeroForOne ? int128(int256(2 * amount)) : -int128(int256(amount));
        return (int256(a0) << 128) | int256(uint256(uint128(a1)));
    }
}

contract Pair {
    Token public token0;
    Token public token1;
    uint112 public r0;
    uint112 public r1;
    constructor(Token a, Token b, uint112 x, uint112 y) { token0=a; token1=b; r0=x; r1=y; }
    function getReserves() external view returns (uint112, uint112, uint32) { return (r0,r1,0); }
    function swap(uint256 o0, uint256 o1, address to, bytes calldata) external {
        require(o0 < r0 && o1 < r1, "liquidity");
        if (o0 != 0) token0.transfer(to,o0);
        if (o1 != 0) token1.transfer(to,o1);
        uint256 b0=token0.balanceOf(address(this)); uint256 b1=token1.balanceOf(address(this));
        uint256 in0=b0 > r0-o0 ? b0-(r0-o0) : 0;
        uint256 in1=b1 > r1-o1 ? b1-(r1-o1) : 0;
        require((b0*10000-in0*30)*(b1*10000-in1*30) >= uint256(r0)*r1*10000**2, "invariant");
        r0=uint112(b0); r1=uint112(b1);
    }
}

// A distinct quote function ensures stable steps cannot accidentally use the volatile 30 bps formula.
contract StablePair is Pair {
    constructor(Token a, Token b, uint112 x, uint112 y) Pair(a,b,x,y) {}
    function getAmountOut(uint256 amount, address input) external view returns (uint256) {
        (uint256 reserveIn, uint256 reserveOut) = input == address(token0) ? (r0,r1) : (r1,r0);
        uint256 afterFee=amount*9300;
        return afterFee*reserveOut/(reserveIn*10000+afterFee);
    }
}

contract EvilPool {
    uint256 public mode;
    function setMode(uint256 next) external { mode=next; }
    function swap(address, bool, int256 amount, uint160, bytes calldata) external returns (int256, int256) {
        if (mode==0) InkArb(msg.sender).uniswapV3SwapCallback(amount+1,0,"");
        if (mode==1) {
            InkArb(msg.sender).uniswapV3SwapCallback(amount,0,"");
            InkArb(msg.sender).uniswapV3SwapCallback(amount,0,"");
        }
        return (amount,-1);
    }
}

contract InkArbTest {
    Vm constant vm=Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    Token a; Token b; Manager manager; InkArb arb; Pair p1; Pair p2;
    address constant BOT=address(0xB07);
    address constant PAYOUT=address(0xBEEF);
    function setUp() public {
        a=new Token(); b=new Token();
        if (address(a)>address(b)) (a,b)=(b,a);
        manager=new Manager();
        arb=new InkArb(IPoolManager(address(manager)),address(this),PAYOUT);
        arb.setExecutor(BOT,true);
        a.mint(address(manager),1000000); b.mint(address(manager),1000000);
        p1=new Pair(a,b,100000,200000); p2=new Pair(a,b,100000,100000);
        a.mint(address(p1),100000); b.mint(address(p1),200000);
        a.mint(address(p2),100000); b.mint(address(p2),100000);
    }
    function header(uint128 amount, uint128 minimum) internal view returns (bytes memory) {
        return abi.encodePacked(InkArb.execute.selector,address(a),amount,minimum,uint32(block.number));
    }
    function v2(Pair pool, Token output) internal pure returns (bytes memory) {
        return abi.encodePacked(uint8(4),address(pool),address(output),uint16(30));
    }
    function run(bytes memory data) internal returns (uint256) {
        vm.prank(BOT);
        (bool ok,bytes memory returned)=address(arb).call(data);
        if (!ok) assembly ("memory-safe") { revert(add(returned,32),mload(returned)) }
        return abi.decode(returned,(uint256));
    }
    function testProfitableV2CyclePaysAndSettles() public {
        uint256 profit=run(bytes.concat(header(1000,1),v2(p1,b),v2(p2,a)));
        require(profit > 900 && a.balanceOf(PAYOUT)==profit, "payout");
        require(a.balanceOf(address(arb))==0 && b.balanceOf(address(arb))==0,"executor balances");
        require(manager.delta(manager.key(address(arb),address(a)))==0,"a delta");
        require(manager.delta(manager.key(address(arb),address(b)))==0,"b delta");
    }
    function testLosingCycleRevertsAndRollsBack() public {
        vm.expectRevert(abi.encodeWithSelector(InkArb.NegativeProfit.selector,int256(-511)));
        run(bytes.concat(header(1000,0),v2(p2,b),v2(p1,a)));
        require(a.balanceOf(address(manager))==1000000,"rollback");
        require(a.balanceOf(PAYOUT)==0,"no payout");
    }
    function testMinimumProfitIsEnforced() public {
        vm.expectRevert(abi.encodeWithSelector(InkArb.ProfitTooLow.selector,930,10000));
        run(bytes.concat(header(1000,10000),v2(p1,b),v2(p2,a)));
    }
    function testV4FirstNeedsNoInitialStartBalance() public {
        vm.prank(address(manager)); a.transfer(address(this),1000000);
        require(a.balanceOf(address(manager))==0,"initial balance");
        bytes memory first=abi.encodePacked(uint8(2),address(b),uint24(3000),int24(60),address(0));
        uint256 profit=run(bytes.concat(header(1000,1),first,v2(p2,a)));
        require(profit > 900 && a.balanceOf(PAYOUT)==profit,"delta-funded payout");
    }
    function stable(Pair pool, Token output) internal pure returns (bytes memory) {
        return abi.encodePacked(uint8(5),address(pool),address(output));
    }
    function testStableFirstUsesPoolQuoteAndPaysExactProfit() public {
        StablePair pool=new StablePair(a,b,100000,200000);
        a.mint(address(pool),100000); b.mint(address(pool),200000);
        uint256 profit=run(bytes.concat(header(1000,1),stable(pool,b),v2(p2,a)));
        require(profit > 750 && profit < 850,"must use distinct pool quote");
        require(a.balanceOf(PAYOUT)==profit,"payout");
        require(manager.delta(manager.key(address(arb),address(b)))==0,"settle");
    }
    function testStableSecondReceivesPreviousOutputDirectly() public {
        StablePair pool=new StablePair(a,b,100000,100000);
        a.mint(address(pool),100000); b.mint(address(pool),100000);
        uint256 profit=run(bytes.concat(header(1000,1),v2(p1,b),stable(pool,a)));
        require(profit > 750 && profit < 850,"must quote actual placed input");
        require(a.balanceOf(PAYOUT)==profit,"payout");
        require(a.balanceOf(address(arb))==0 && b.balanceOf(address(arb))==0,"no executor balance");
    }
    function v4(address output, uint24 fee) internal pure returns (bytes memory) {
        return abi.encodePacked(uint8(2),output,fee,int24(60),address(0));
    }
    function testNativeStartPaysNativeProfitAndSettlesEveryCurrency() public {
        vm.deal(address(manager),1000000);
        bytes memory head=abi.encodePacked(InkArb.execute.selector,address(0),uint128(1000),uint128(1),uint32(block.number));
        uint256 profit=run(bytes.concat(head,v4(address(a),3000),v4(address(0),500)));
        require(profit==3000 && PAYOUT.balance==profit,"native payout");
        require(address(arb).balance==0 && address(manager).balance==1000000-profit,"no native custody");
        require(manager.delta(manager.key(address(arb),address(0)))==0,"native settled");
    }
    function testNativeIntermediateNeverNeedsPhysicalNativeTransfer() public {
        require(address(manager).balance==0,"no physical ETH");
        uint256 profit=run(bytes.concat(header(1000,1),v4(address(0),3000),v4(address(b),500),v2(p2,a)));
        require(profit>2500 && a.balanceOf(PAYOUT)==profit,"ERC20 payout");
        require(address(manager).balance==0 && address(arb).balance==0,"native never transferred");
        require(manager.delta(manager.key(address(arb),address(0)))==0,"native intermediate settled");
    }
    function testNativeExternalStepIsRejectedBeforeTransfer() public {
        vm.expectRevert(InkArb.NativeNotSupported.selector);
        run(bytes.concat(header(1000,0),v4(address(0),3000),v2(p2,a)));
    }
    function testRejectsUnauthorizedExecute() public {
        bytes memory data=bytes.concat(header(1000,0),v2(p1,b),v2(p2,a));
        vm.expectRevert(InkArb.NotExecutor.selector);
        (bool ok,)=address(arb).call(data); require(ok,"unreachable");
    }
    function testCallbackNeedsExpectedPool() public {
        vm.expectRevert(InkArb.BadCallback.selector);
        arb.uniswapV3SwapCallback(1,0,"");
    }
    function testUnlockCallbackNeedsActiveManager() public {
        vm.expectRevert(InkArb.Unauthorized.selector);
        arb.unlockCallback("");
    }
    function testPauseAndExpiredRoute() public {
        arb.pause(); vm.expectRevert(InkArb.IsPaused.selector);
        run(bytes.concat(header(1000,0),v2(p1,b),v2(p2,a)));
        arb.unpause();
        bytes memory data=abi.encodePacked(InkArb.execute.selector,address(a),uint128(1000),uint128(0),uint32(0),v2(p1,b),v2(p2,a));
        vm.expectRevert(InkArb.Expired.selector); run(data);
    }
    function testNoAdapterKind() public {
        vm.expectRevert(abi.encodeWithSelector(InkArb.UnknownKind.selector,uint8(3)));
        run(bytes.concat(header(1000,0),hex"03"));
    }
    function testEmptyRouteRejected() public {
        vm.expectRevert(InkArb.BadRoute.selector); run(header(1000,0));
    }
    function testV3OverpayAndDoubleCallbackAndMissingCallback() public {
        EvilPool pool=new EvilPool();
        bytes memory data=bytes.concat(header(1000,0),abi.encodePacked(uint8(1),address(pool),address(b)),v2(p2,a));
        vm.expectRevert(abi.encodeWithSelector(InkArb.OverPay.selector,1001,1000)); run(data);
        pool.setMode(1); vm.expectRevert(InkArb.BadCallback.selector); run(data);
        pool.setMode(2); vm.expectRevert(InkArb.CallbackMissing.selector); run(data);
    }
}
