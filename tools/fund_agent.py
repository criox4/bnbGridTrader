"""Move test capital from the buyer wallet to the AGENT (seller) wallet.

    python tools/fund_agent.py 0.008 4.0

The grid trades the agent's OWN capital, and `studio.toml` binds that identity
to ERC-8004 ids 269233/1838 — so the capital moves to the agent rather than the
agent being repointed at whichever wallet happens to hold funds.

Signs with the buyer keystore only. Nothing here is reachable from the agent.
"""
import os
import sys
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "agent"))
import chain  # noqa: E402

BUYER = "0x7545e5c647880Bf16558FDfCdA892487DfFFE522"
SELLER = "0xFAf0ffd121947B9EE3920Fa0CfbF9EEEB0AcBF7f"
BUYER_KEYSTORE = ".studio/wallets-buyer"

ERC20 = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
]


def main(bnb: float, usdt: float):
    from bnbagent.wallets import EVMWalletProvider

    # BSC is POA: its extraData exceeds what web3's validation middleware allows,
    # and build_transaction() fetches a block. The agent's own _send builds tx
    # dicts by hand with a legacy gasPrice, which is why it never hit this.
    from web3.middleware import ExtraDataToPOAMiddleware

    w3 = chain._w3("bsc-mainnet")
    if "poa" not in w3.middleware_onion:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, "poa", layer=0)
    a = chain.addresses("bsc-mainnet")
    wallet = EVMWalletProvider(
        password=os.environ["WALLET_PASSWORD"], address=BUYER, wallets_dir=BUYER_KEYSTORE,
    )
    me = Web3.to_checksum_address(BUYER)
    to = Web3.to_checksum_address(SELLER)

    def send(tx):
        tx.setdefault("chainId", 56)
        tx["nonce"] = w3.eth.get_transaction_count(me)
        # build_transaction() fills EIP-1559 fields; mixing those with a legacy
        # gasPrice fails RLP serialisation ("Unknown kwargs: ['gasPrice']").
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)
        tx["gasPrice"] = w3.eth.gas_price
        signed = wallet.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed["rawTransaction"])
        r = w3.eth.wait_for_transaction_receipt(h, timeout=300)
        print(f"  {'ok  ' if r.status else 'FAIL'} {h.hex()}")
        if not r.status:
            raise SystemExit("reverted")

    if usdt > 0:
        usdt_c = w3.eth.contract(address=Web3.to_checksum_address(a["usdt"]), abi=ERC20)
        amt = int(usdt * 10**18)
        print(f"USDT {usdt} -> agent:")
        send(usdt_c.functions.transfer(to, amt).build_transaction(
            {"from": me, "gas": 120_000}))
    if bnb > 0:
        print(f"BNB {bnb} -> agent:")
        send({"from": me, "to": to, "value": int(bnb * 10**18), "gas": 21_000})

    print("agent now:", {k: v for k, v in chain.get_balances(SELLER, "bsc-mainnet").items()
                         if k in ("bnb", "usdt")})


if __name__ == "__main__":
    main(float(sys.argv[1]), float(sys.argv[2]))
