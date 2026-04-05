import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

HOST = "https://clob.polymarket.com"


def get_live_client():
    private_key = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ["POLYMARKET_FUNDER_ADDRESS"]
    signature_type = int(os.environ["POLYMARKET_SIGNATURE_TYPE"])

    client = ClobClient(
        HOST,
        key=private_key,
        chain_id=137,
        signature_type=signature_type,
        funder=funder,
    )

    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


def place_market_buy(token_id: str, price: float, size_usd: float):
    client = get_live_client()

    size = round(size_usd / max(price, 0.01), 4)

    order_args = OrderArgs(
        price=price,
        size=size,
        side="BUY",
        token_id=token_id,
    )

    signed = client.create_order(order_args)
    return client.post_order(signed, OrderType.GTC)
