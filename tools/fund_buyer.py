"""Fund the BUYER test wallet with $U by swapping a little BNB on mainnet.

    python tools/fund_buyer.py quote          # read-only
    python tools/fund_buyer.py swap 0.002     # send it

Separate from the seller's grid_signing.py on purpose: this signs with the
BUYER keystore (.studio/wallets-buyer), never the agent's. Nothing here is
reachable from the agent process or from an LLM tool.

Route: wrap BNB -> WBNB, then V3 exactInputSingle WBNB->U through the fee-500
pool (1.35M U / 3.3k WBNB when measured, so a 0.002 BNB swap is noise).
"""
import json
import os
import sys
import time
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "agent"))
from grid_signing import V3_ROUTER_ABI  # noqa: E402  (same shape, verified by selector)

U = Web3.to_checksum_address("0xcE24439F2D9C6a2289F741120FE202248B666666")
FEE = 500
BUYER_KEYSTORE = ".studio/wallets-buyer"
RPC = "https://bsc-dataseed.binance.org"

WBNB_ABI = [
    {"name": "deposit", "type": "function", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
]
QUOTER_ABI = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "address", "name": "tokenIn"}, {"type": "address", "name": "tokenOut"},
         {"type": "uint256", "name": "amountIn"}, {"type": "uint24", "name": "fee"},
         {"type": "uint160", "name": "sqrtPriceLimitX96"}]}],
     "outputs": [{"type": "uint256", "name": "amountOut"}, {"type": "uint160"},
                 {"type": "uint32"}, {"type": "uint256"}]},
]


def _book():
    p = Path(__file__).resolve().parent.parent / "config" / "bsc-contracts.json"
    return json.load(open(p))["networks"]["bsc-mainnet"]


def _w3():
    return Web3(Web3.HTTPProvider(RPC))


def _buyer():
    from bnbagent.wallets import EVMWalletProvider
    return EVMWalletProvider(
        password=os.environ["WALLET_PASSWORD"],
        address="0x7545e5c647880Bf16558FDfCdA892487DfFFE522",
        wallets_dir=BUYER_KEYSTORE,
    )


def quote(amount_bnb: float = 0.002):
    w3, b = _w3(), _book()
    q = w3.eth.contract(address=Web3.to_checksum_address(b["pancakeswap_v3"]["quoter_v2"]), abi=QUOTER_ABI)
    amt = int(amount_bnb * 1e18)
    out = q.functions.quoteExactInputSingle(
        (Web3.to_checksum_address(b["tokens"]["wbnb"]), U, amt, FEE, 0)
    ).call()[0]
    print(f"{amount_bnb} BNB -> {out/1e18:.6f} U   (1 BNB ≈ {out/amt*1e0:.2f} U)")
    return amt, out


def swap(amount_bnb: float = 0.002, slippage_pct: float = 1.0):
    w3, b = _w3(), _book()
    amt, expected = quote(amount_bnb)
    min_out = int(expected * (1 - slippage_pct / 100))
    wallet = _buyer()
    me = Web3.to_checksum_address(wallet.address)
    wbnb = Web3.to_checksum_address(b["tokens"]["wbnb"])
    router = Web3.to_checksum_address(b["pancakeswap_v3"]["swap_router"])
    print(f"buyer {me}  min_out {min_out/1e18:.6f} U")

    def send(fn, value=0):
        tx = fn.build_transaction({
            "from": me, "value": value, "nonce": w3.eth.get_transaction_count(me),
            "gas": 400_000, "gasPrice": w3.eth.gas_price, "chainId": 56,
        })
        signed = wallet.sign_transaction(tx)
        raw = signed["rawTransaction"]  # SDK returns a dict, not a SignedTransaction
        h = w3.eth.send_raw_transaction(raw)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=300)
        print(f"  {'ok ' if r.status else 'FAIL'} {h.hex()}")
        if not r.status:
            raise SystemExit("transaction reverted")
        return r

    c = w3.eth.contract(address=wbnb, abi=WBNB_ABI)
    print("wrap:")
    send(c.functions.deposit(), value=amt)
    if c.functions.allowance(me, router).call() < amt:
        print("approve:")
        send(c.functions.approve(router, amt))
    print("swap:")
    r = w3.eth.contract(address=router, abi=V3_ROUTER_ABI)
    send(r.functions.exactInputSingle(
        (wbnb, U, FEE, me, int(time.time()) + 600, amt, min_out, 0)
    ))
    erc = w3.eth.contract(address=U, abi=WBNB_ABI)
    print(f"buyer U balance now: {erc.functions.balanceOf(me).call()/1e18:.6f}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "quote"
    arg = float(sys.argv[2]) if len(sys.argv) > 2 else 0.002
    {"quote": quote, "swap": swap}[cmd](arg)
