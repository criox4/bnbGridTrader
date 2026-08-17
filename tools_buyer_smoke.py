"""Buyer-side harness: drive the full ERC-8183 job loop against our own agent.

    python buyer.py create|status|fetch|complete

The studio's testnet OptimisticPolicy has been DE-WHITELISTED on the
EvaluatorRouter (`policyWhitelist(0x4f4678d4..) == False`, while mainnet's is
still True), so `bag erc8183 buy` reverts at register_job with
PolicyNotWhitelisted(). This harness routes around that by creating the job with
evaluator = us and hook = 0, which needs no router registration: an unrouted job
is completed by its evaluator via commerce.complete() instead of router.settle().

That exercises OUR half of the loop end to end (create -> fund -> notify ->
LLM work -> submit_result -> deliverable fetch -> payment release) but NOT the
studio's policy-based settlement, which is untestable on testnet while the
policy stays de-whitelisted.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bnbagent.erc8183 import ERC8183Client
from bnbagent_studio_core.wallet import get_wallet

NETWORK = os.environ.get("BNB_NETWORK", "bsc-testnet")
ZERO = "0x0000000000000000000000000000000000000000"
STATE = Path(__file__).parent / "buyer-job.json"
PRICE_U = 0.1


def client():
    """Client that SELF-PAYS gas.

    The testnet preset sets use_paymaster=True (MegaFuel). Sponsored writes were
    silently dropped — broadcast returned a hash, then the tx was in neither the
    mempool nor a block and the nonce never advanced. resolve_network() returns a
    NetworkConfig verbatim, so a copy with use_paymaster=False makes _build_paymaster
    return None and the wallet pays its own gas.
    """
    import dataclasses

    from bnbagent.config import resolve_network

    w = get_wallet()
    nc = dataclasses.replace(resolve_network(NETWORK), use_paymaster=False)
    return ERC8183Client(wallet_provider=w, network=nc), w.address


def create():
    c, me = client()
    dec = c.token_decimals()
    amount = int(PRICE_U * 10**dec)
    expired_at = int(time.time()) + 3 * 24 * 3600

    print(f"buyer/provider/evaluator = {me}  network={NETWORK}")
    # evaluator=me so completion needs no whitelisted policy. The kernel rejects
    # a zero hook (HookRequired()), so the router stays as the hook.
    r = c.commerce.create_job(
        provider=me,
        evaluator=me,
        expired_at=expired_at,
        description="Compute a 9-level BNB/USDT grid plan for 200 USDT capital",
        hook=c.router.address,
    )
    job_id = int(r["jobId"])
    print(f"  createJob   -> job {job_id}")

    c.set_budget(job_id, amount)
    print(f"  setBudget   -> {PRICE_U} U ({amount})")

    c.fund(job_id, amount)
    print(f"  fund        -> escrowed {PRICE_U} U")

    job = c.get_job(job_id)
    print(f"  status      -> {job.status.name}")
    STATE.write_text(json.dumps({"job_id": job_id, "network": NETWORK}))
    return job_id


def _job_id():
    return json.loads(STATE.read_text())["job_id"]


def status():
    c, _ = client()
    jid = _job_id()
    job = c.get_job(jid)
    print(f"job {jid}: status={job.status.name} client={job.client}")
    print(f"  provider={job.provider} evaluator={job.evaluator}")
    print(f"  budget={job.budget} expired_at={job.expired_at}")
    return job


def fetch():
    c, _ = client()
    jid = _job_id()
    url = c.get_deliverable_url(jid)
    print(f"deliverable_url: {url}")
    return url


def complete():
    c, me = client()
    jid = _job_id()
    bal_before = c.token_balance(me) if hasattr(c, "token_balance") else None
    r = c.commerce.complete(jid)
    print(f"complete tx: {r.get('tx_hash') or r.get('transactionHash')}")
    print(f"status now: {c.get_job(jid).status.name}")
    if bal_before is not None:
        print(f"balance {bal_before} -> {c.token_balance(me)}")


if __name__ == "__main__":
    {"create": create, "status": status, "fetch": fetch, "complete": complete}[
        sys.argv[1]
    ]()
