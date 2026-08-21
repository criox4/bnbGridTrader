"""Buyer-side harness: drive the ERC-8183 lifecycle against the deployed seller.

    python tools/buyer_smoke.py negotiate
    python tools/buyer_smoke.py run          # negotiate -> create -> fund -> notify
    python tools/buyer_smoke.py status | fetch | settle

TWO SEPARATE WALLETS. This signs with the BUYER keystore
(.studio/wallets-buyer); the seller signs with .studio/wallets and its identity
in studio.toml is never touched. That separation is the point — a single-wallet
run cannot prove client != provider.

The evaluator is the ROUTER (the SDK's create_job wires it that way), so
settlement is the OptimisticPolicy's: silence past the dispute window, then a
permissionless router.settle(). Nothing here can shortcut that.

EXPIRED_AT NEEDS A REAL BUFFER. Mainnet's dispute window is 7 days. A job whose
expired_at is only minutes past submittedAt + disputeWindow is settle-able for
only those minutes before claimRefund can take it to EXPIRED instead. Mainnet
job 56608 shipped with a 29-minute window; this uses DISPUTE_BUFFER_DAYS.
"""
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

NETWORK = os.environ.get("BNB_NETWORK", "bsc-mainnet")
BUYER_KEYSTORE = ".studio/wallets-buyer"
BUYER = "0x7545e5c647880Bf16558FDfCdA892487DfFFE522"
SELLER = "0xFAf0ffd121947B9EE3920Fa0CfbF9EEEB0AcBF7f"
AGENT_URL = os.environ.get("SELLER_URL", "https://bnb-grid.172-104-171-139.nip.io")
STATE = Path(__file__).parent / "buyer-job.json"
TASK = "Compute a 9-level BNB/USDT grid plan for 200 USDT capital"
DISPUTE_BUFFER_DAYS = 2


def buyer_wallet():
    from bnbagent.wallets import EVMWalletProvider
    return EVMWalletProvider(
        password=os.environ["WALLET_PASSWORD"], address=BUYER, wallets_dir=BUYER_KEYSTORE,
    )


def client():
    from bnbagent.erc8183 import ERC8183Client
    return ERC8183Client(wallet_provider=buyer_wallet(), network=NETWORK)


def _a2a(payload: dict) -> dict:
    """Send one A2A message/send with the skill envelope as a DataPart.

    The seller has no free-form skill: prose without a DataPart is rejected by
    design, so the envelope shape matters.
    """
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "role": "user", "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "data", "data": payload}],
        }},
    }
    req = urllib.request.Request(
        AGENT_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def _reply(resp: dict) -> dict:
    """Pull the seller's data part out of the JSON-RPC envelope."""
    result = resp.get("result") or {}
    for part in (result.get("parts") or []):
        if part.get("kind") == "data":
            return part.get("data") or {}
    for msg in (result.get("artifacts") or []):
        for part in (msg.get("parts") or []):
            if part.get("kind") == "data":
                return part.get("data") or {}
    return resp


def negotiate():
    q = _reply(_a2a({"skill": "negotiate", "task_description": TASK,
                     "terms": {"deliverables": "grid plan JSON",
                               "quality_standards": "levels, spacing and per-rung size"}}))
    print(json.dumps(q, indent=2)[:900])
    return q


def run():
    c = client()
    dec = c.token_decimals()
    q = negotiate()
    resp = q.get("response") or {}
    if not resp.get("accepted"):
        raise SystemExit(f"seller rejected the quote: {resp.get('reason')}")
    price = int((resp.get("terms") or {}).get("price") or 0)
    if not price:
        raise SystemExit(f"no price in quote: {q}")
    print(f"\nquoted price: {price} ({price/10**dec} U)")

    bal = c.token_balance(BUYER)
    print(f"buyer U balance: {bal/10**dec}")
    if bal < price:
        raise SystemExit("buyer cannot cover the quote — run tools/fund_buyer.py swap")

    dispute = int(c.policy.dispute_window())
    expired_at = int(time.time()) + dispute + DISPUTE_BUFFER_DAYS * 86400
    print(f"dispute_window {dispute}s; expired_at = now + window + "
          f"{DISPUTE_BUFFER_DAYS}d -> settle window is {DISPUTE_BUFFER_DAYS} days wide")

    # The description is NOT free text: verify.py requires a JobDescription
    # carrying the quote WE just negotiated, and recovers provider_sig from it to
    # confirm the seller signed these exact terms. A plain string is rejected
    # permanently with "no signed quote anchored in job description", and the
    # description cannot be changed after createJob — so build it from the
    # negotiation result verbatim.
    from bnbagent.erc8183.negotiation import build_job_description

    description = build_job_description(q)
    r = c.create_job(provider=SELLER, expired_at=expired_at, description=description)
    job_id = int(r["jobId"])
    print(f"  createJob  -> job {job_id}  (evaluator = router)")
    c.register_job(job_id)
    print("  registerJob-> policy bound")
    c.set_budget(job_id, price)
    print(f"  setBudget  -> {price/10**dec} U")
    c.fund(job_id, price)
    print(f"  fund       -> escrowed; status {c.get_job_status(job_id).name}")
    STATE.write_text(json.dumps({"job_id": job_id, "network": NETWORK,
                                 "expired_at": expired_at, "price": price}))

    if "--no-notify" in sys.argv:
        # Deliberately silent: proves the Service Layer sweep finds a funded job
        # that the buyer never announced. Without the sweep this job would sit
        # FUNDED until its deadline.
        print("  notify     -> SKIPPED (--no-notify): the sweep must find this one")
    else:
        ack = _reply(_a2a({"skill": "notify_funded", "job_id": job_id}))
        print(f"  notify     -> {json.dumps(ack)[:300]}")
    print(f"\njob {job_id} funded. Poll: python tools/buyer_smoke.py status")
    return job_id


def _job_id():
    return json.loads(STATE.read_text())["job_id"]


def status():
    c = client()
    jid = _job_id()
    j = c.get_job(jid)
    print(f"job {jid}: {j.status.name}")
    print(f"  client   {j.client}")
    print(f"  provider {j.provider}")
    print(f"  evaluator{j.evaluator}")
    print(f"  separated: {j.client.lower() != j.provider.lower()}")
    return j


def fetch():
    c = client()
    jid = _job_id()
    url = c.get_deliverable_url(jid)
    print("deliverable_url:", url)
    if url and url.startswith("http"):
        with urllib.request.urlopen(url, timeout=60) as r:
            print("fetched:", r.status, r.read()[:400])
    return url


def settle():
    c = client()
    jid = _job_id()
    r = c.settle(jid)
    print("settle tx:", r.get("tx_hash") or r.get("transactionHash"))
    print("status now:", c.get_job_status(jid).name)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"negotiate": negotiate, "run": run, "status": status,
     "fetch": fetch, "settle": settle}[cmd]()
