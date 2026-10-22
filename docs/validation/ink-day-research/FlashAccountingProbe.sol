// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
interface PM {
    function unlock(bytes calldata) external returns(bytes memory);
    function take(address,address,uint256) external;
    function sync(address) external;
    function settle() external payable returns(uint256);
}
interface ERC20Probe {
    function balanceOf(address) external view returns(uint256);
    function transfer(address,uint256) external returns(bool);
}
contract FlashAccountingProbe {
    PM constant MANAGER=PM(0x360E68faCcca8cA495c1B759Fd9EEe466db9FB32);
    function run(address token,uint256 amount) external returns(uint256 beforeBalance,uint256 afterBalance) {
        beforeBalance=ERC20Probe(token).balanceOf(address(this));
        MANAGER.unlock(abi.encode(token,amount,beforeBalance));
        afterBalance=ERC20Probe(token).balanceOf(address(this));
        require(afterBalance==beforeBalance,"nonzero cost");
    }
    function unlockCallback(bytes calldata data) external returns(bytes memory) {
        require(msg.sender==address(MANAGER),"manager only");
        (address token,uint256 amount,uint256 baseline)=abi.decode(data,(address,uint256,uint256));
        MANAGER.take(token,address(this),amount);
        require(ERC20Probe(token).balanceOf(address(this))==baseline+amount,"take mismatch");
        MANAGER.sync(token);
        require(ERC20Probe(token).transfer(address(MANAGER),amount),"repay failed");
        require(MANAGER.settle()==amount,"settle mismatch");
        return "";
    }
}
