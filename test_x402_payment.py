#!/usr/bin/env python3
"""
Test script for x402 payment flow on Teardrop.

Performs:
  1. Get SIWE nonce
  2. Sign SIWE message (EIP-191)
  3. Get JWT via SIWE
  4. Call /agent/run → get 402 Payment Required
  5. Sign x402 payment (EIP-3009 USDC transfer)
  6. Retry /agent/run with signed payment
  7. Parse SSE stream for BILLING_SETTLEMENT event

Usage:
  python test_x402_payment.py <private_key_hex> <base_url>

Example:
  python test_x402_payment.py 0xabc123... https://teardrop.onrender.com
"""

import sys
import json
import requests
import time
import base64
import warnings
from datetime import datetime, timedelta
from siwe import SiweMessage
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct


def get_nonce(base_url: str) -> str:
    """Fetch a SIWE nonce from /auth/siwe/nonce."""
    resp = requests.get(f"{base_url}/auth/siwe/nonce")
    resp.raise_for_status()
    return resp.json()["nonce"]


def create_siwe_message(nonce: str, address: str, domain: str) -> str:
    """Create an EIP-4361 SIWE message in proper text format."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        now = datetime.utcnow()
    expires_at = now + timedelta(hours=1)
    
    message = SiweMessage(
        domain=domain,
        address=address,
        statement="Sign in to Teardrop",
        uri="https://teardrop.onrender.com",
        version="1",
        chain_id=84532,  # Base Sepolia
        nonce=nonce,
        issued_at=now.replace(microsecond=0).isoformat() + "Z",
        expiration_time=expires_at.replace(microsecond=0).isoformat() + "Z",
    )
    # The prepare_message method returns the RFC 4648-formatted message
    return message.prepare_message()


def sign_message(message: str, private_key: str) -> str:
    """Sign a message with EIP-191."""
    account = Account.from_key(private_key)
    msg_hash = encode_defunct(text=message)
    signed = account.sign_message(msg_hash)
    return signed.signature.hex()


def get_jwt_via_siwe(base_url: str, siwe_message: str, signature: str) -> str:
    """Exchange SIWE message + signature for JWT."""
    print(f"\n   [DEBUG] Full SIWE message:\n{siwe_message}\n")
    print(f"   [DEBUG] Signature: {signature[:50]}...\n")
    
    resp = requests.post(
        f"{base_url}/token",
        json={
            "siwe_message": siwe_message,
            "siwe_signature": signature,
        },
    )
    if resp.status_code != 200:
        print(f"✗ SIWE exchange failed with {resp.status_code}")
        print(f"Response: {resp.text}")
        sys.exit(1)
    return resp.json()["access_token"]


def call_agent_run_no_payment(base_url: str, jwt: str) -> dict:
    """Call /agent/run without payment → expect 402."""
    resp = requests.post(
        f"{base_url}/agent/run",
        json={"message": "What is 2+2?", "thread_id": "test-1"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    
    if resp.status_code == 402:
        print("✓ Got 402 Payment Required (expected)")
        return {
            "status": 402,
            "headers": dict(resp.headers),
            "body": resp.json() if resp.text else {},
        }
    else:
        print(f"✗ Expected 402, got {resp.status_code}")
        print(resp.text)
        sys.exit(1)


def sign_x402_payment(payment_required_body: dict, private_key: str) -> str:
    """Sign x402 payment using EIP-3009 (USDC transferWithAuthorization).

    Uses x402ClientSync to create a properly signed PaymentPayload,
    then base64-encodes it for the X-PAYMENT request header.
    """
    from x402 import parse_payment_required, x402ClientSync
    from x402.mechanisms.evm.exact import ExactEvmScheme

    account = Account.from_key(private_key)

    # Parse the full 402 response body into the SDK's PaymentRequired object
    payment_required = parse_payment_required(payment_required_body)

    req = payment_required.accepts[0]
    print(f"   Signing payment: {req.amount} atomic USDC → {req.pay_to}")
    print(f"   Network: {req.network}, Asset: {req.asset}")
    print(f"   extra: {req.extra}")

    # ExactEvmScheme auto-wraps a LocalAccount into the required EthAccountSigner
    scheme = ExactEvmScheme(signer=account)

    # Register scheme for whichever network the server advertises
    network = payment_required.accepts[0].network
    client = x402ClientSync()
    client.register(network, scheme)
    payload = client.create_payment_payload(payment_required)

    print(f"   PaymentPayload created (x402_version={payload.x402_version})")

    # Print decoded payload for debugging
    payload_json = payload.model_dump_json()
    try:
        payload_dict = json.loads(payload_json)
        inner = payload_dict.get("payload", {})
        auth = inner.get("authorization", {})
        print(f"   Authorization: from={auth.get('from')} to={auth.get('to')} value={auth.get('value')}")
        print(f"   validAfter={auth.get('validAfter')} validBefore={auth.get('validBefore')}")
        print(f"   nonce={str(auth.get('nonce', ''))[:20]}...")
    except Exception:
        pass

    # Serialize with camelCase aliases (BaseX402Model: serialize_by_alias=True)
    # then base64-encode for the X-PAYMENT header
    return base64.b64encode(payload_json.encode()).decode()


def call_agent_run_with_payment(
    base_url: str,
    jwt: str,
    payment_signature: str,
) -> None:
    """Call /agent/run with signed payment → stream SSE events."""
    headers = {
        "Authorization": f"Bearer {jwt}",
        "X-PAYMENT": payment_signature,
    }
    
    resp = requests.post(
        f"{base_url}/agent/run",
        json={"message": "What is 2+2?", "thread_id": "test-1"},
        headers=headers,
        stream=True,
    )
    
    if resp.status_code != 200:
        print(f"✗ Expected 200, got {resp.status_code}")
        print(resp.text)
        sys.exit(1)
    
    print("✓ Got 200 OK (stream started)")
    print("\n--- SSE Stream Events ---")
    
    settlement_tx = None
    for line in resp.iter_lines():
        if not line:
            continue
        
        line_str = line.decode("utf-8") if isinstance(line, bytes) else line
        
        # Parse SSE event format: "event: TYPE\ndata: JSON"
        if line_str.startswith("event:"):
            event_type = line_str.split(":", 1)[1].strip()
        elif line_str.startswith("data:"):
            event_data_str = line_str.split(":", 1)[1].strip()
            try:
                event_data = json.loads(event_data_str)
                
                if event_type == "BILLING_SETTLEMENT":
                    settlement_tx = event_data.get("tx_hash", "")
                    print(f"\n🎉 BILLING_SETTLEMENT event:")
                    print(f"   tx_hash: {settlement_tx}")
                    print(f"   amount_usdc: {event_data.get('amount_usdc')}")
                    print(f"   network: {event_data.get('network')}")
                
                elif event_type in ["TEXT_MESSAGE_CONTENT", "USAGE_SUMMARY"]:
                    print(f"[{event_type}] {event_data}")
                elif event_type in ["RUN_STARTED", "RUN_FINISHED", "DONE"]:
                    print(f"[{event_type}] ✓")
            except json.JSONDecodeError:
                pass
    
    if settlement_tx:
        print(f"\n✓ Settlement successful!")
        print(f"  View on Sepolia Etherscan: https://sepolia.basescan.org/tx/{settlement_tx}")
    else:
        print("\n⚠ No settlement event captured")


def main():
    if len(sys.argv) < 3:
        print("Usage: python test_x402_payment.py <private_key> <base_url>")
        print("\nExample:")
        print("  python test_x402_payment.py 0xabc123... https://teardrop.onrender.com")
        sys.exit(1)
    
    private_key = sys.argv[1]
    base_url = sys.argv[2].rstrip("/")
    
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    # Derive account from private key
    account = Account.from_key(private_key)
    address = account.address
    domain = base_url.replace("https://", "").replace("http://", "")
    
    print(f"🔑 Address: {address}")
    print(f"🌐 Domain: {domain}")
    print(f"🔗 Base URL: {base_url}")
    print()
    
    # Step 1: Get nonce
    print("→ Step 1: Getting SIWE nonce...")
    nonce = get_nonce(base_url)
    print(f"✓ Nonce: {nonce[:16]}...")
    
    # Step 2: Sign SIWE message
    print("\n→ Step 2: Creating and signing SIWE message...")
    siwe_msg = create_siwe_message(nonce, address, domain)
    print(f"   SIWE message: {siwe_msg[:100]}...")
    siwe_sig = sign_message(siwe_msg, private_key)
    print(f"✓ Signed SIWE message")
    
    # Step 3: Get JWT
    print("\n→ Step 3: Exchanging SIWE for JWT...")
    jwt = get_jwt_via_siwe(base_url, siwe_msg, siwe_sig)
    print(f"✓ JWT: {jwt[:50]}...")
    
    # Step 4: Call /agent/run without payment
    print("\n→ Step 4: Calling /agent/run (no payment)...")
    payment_required = call_agent_run_no_payment(base_url, jwt)
    # x402 v2 uses 'accepts' key
    payment_requirements = payment_required["body"].get("accepts", [])
    if not payment_requirements:
        print("⚠ No payment requirements in 402 response")
        print(f"   Full response: {payment_required['body']}")
        sys.exit(1)
    print(f"   Payment: {payment_requirements[0].get('amount')} atomic USDC on {payment_requirements[0].get('network')}")
    print(f"   Asset: {payment_requirements[0].get('asset')}")
    print(f"   pay_to: {payment_requirements[0].get('payTo')}")
    print(f"   extra: {payment_requirements[0].get('extra')}")
    
    # Step 5: Sign x402 payment
    print("\n→ Step 5: Signing x402 payment...")
    try:
        payment_sig = sign_x402_payment(payment_required["body"], private_key)
        print(f"✓ Signed payment: {payment_sig[:50]}...")
    except Exception as e:
        print(f"✗ Error signing payment: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # Step 6: Call /agent/run with payment
    print("\n→ Step 6: Calling /agent/run with signed payment...")
    call_agent_run_with_payment(base_url, jwt, payment_sig)


if __name__ == "__main__":
    main()
