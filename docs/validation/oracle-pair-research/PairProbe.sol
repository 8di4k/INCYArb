// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
interface Token {
    function balanceOf(address) external view returns(uint256);
    function approve(address,uint256) external returns(bool);
    function transfer(address,uint256) external returns(bool);
}
interface Bico {
    function swap(address,address,uint256,uint256,address,uint256) external returns(uint256);
}
contract PairProbe {
    error LegFailed(uint256 stage,bytes data);
    address constant USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant WETH=0x4200000000000000000000000000000000000006;
    address constant ELF=0xf0f0F0F0FB0d738452EfD03A28e8be14C76d5f73;
    address constant BICO=0x0000009E5F98cDB4dd1E1d2E15B220217c88B3b8;
    function probe(uint256 amount) external returns(uint256 beforeStable,uint256 middle,uint256 afterStable) {
        beforeStable=Token(USDC).balanceOf(address(this));
        require(beforeStable==amount,"initial balance mismatch");
        uint256 beforeWeth=Token(WETH).balanceOf(address(this));
        require(Token(USDC).approve(ELF,amount));
        (bool ok,bytes memory data)=ELF.call(abi.encodeWithSelector(bytes4(0x598edcad),USDC,WETH,amount,0,address(this),0));
        if(!ok) revert LegFailed(1,data);
        middle=Token(WETH).balanceOf(address(this))-beforeWeth;
        require(middle>0,"empty first leg");
        require(Token(WETH).transfer(BICO,middle));
        (ok,data)=BICO.call(abi.encodeCall(Bico.swap,(WETH,USDC,middle,0,address(this),0)));
        if(!ok) revert LegFailed(2,data);
        afterStable=Token(USDC).balanceOf(address(this));
    }
}
