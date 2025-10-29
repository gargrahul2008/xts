# file: xts_place_order_demo.py
import os
from datetime import datetime

# if you prefer .env, uncomment:
# from dotenv import load_dotenv; load_dotenv()

from Connect import XTSConnect  # from the official repo

API_KEY    = '98b7e43c7ca45f7ae27624' #os.getenv("XTS_API_KEY",    "YOUR_API_KEY")
API_SECRET = 'Wsae140#tk' #os.getenv("XTS_API_SECRET", "YOUR_API_SECRET")
SOURCE     = 'WEBAPI' #os.getenv("XTS_SOURCE",     "WEBAPI")  # must match config.ini
ROOT_URL   = 'http://14.99.241.29:4000' #os.getenv("XTS_ROOT",       "https://developers.symphonyfintech.in")

# --- helper: find exchangeInstrumentID by symbol from the daily master (NSE cash) ---
def find_nsecm_instrument_id(xt: XTSConnect, symbol: str) -> int:
    """
    Download master and find exchangeInstrumentID for a cash symbol like 'RELIANCE-EQ'.
    The master is a big '|' delimited string; for NSECM the field 2 is the ID.
    """
    cfg = xt.get_config()
    # get master for NSECM
    master = xt.get_master(exchangeSegmentList=[xt.EXCHANGE_NSECM])
    raw = master.get("result", "")  # large '|' separated lines
    for line in raw.splitlines():
        # format is: NSECM|<ID>|8|<...>|<TradingSymbol>|...
        parts = line.split("|")
        # trading symbol usually at index 4 (per SDK docs/examples)
        if len(parts) > 5 and parts[4] == symbol:
            return int(parts[1])
    raise ValueError(f"Symbol {symbol} not found in master")

def main():
    # Instantiate client with your creds (README shows this shape)
    xt = XTSConnect(API_KEY, API_SECRET, SOURCE)

    # --- Login (trading) ---
    login_resp = xt.interactive_login()
    if login_resp.get("type") != "success":
        raise RuntimeError(f"Interactive login failed: {login_resp}")

    print("Interactive login OK at", datetime.now())

    # --- Resolve instrument ID (if you already know it, skip this step) ---
    # Example cash symbol; change to what you need, e.g., 'INFY-EQ', 'TCS-EQ'
    symbol = os.getenv("XTS_SYMBOL", "RELIANCE-EQ")
    try:
        exchange_instrument_id = find_nsecm_instrument_id(xt, symbol)
    except Exception as e:
        raise SystemExit(f"Cannot resolve symbol -> ID: {e}")

    print(f"{symbol} exchangeInstrumentID =", exchange_instrument_id)

    # --- Place a tiny market BUY order (MIS intraday) ---
    # Params and constants mirror README's example
    order_resp = xt.place_order(
        exchangeSegment=xt.EXCHANGE_NSECM,
        exchangeInstrumentID=exchange_instrument_id,
        productType=xt.PRODUCT_MIS,               # MIS intraday
        orderType=xt.ORDER_TYPE_MARKET,           # Market order
        orderSide=xt.TRANSACTION_TYPE_BUY,        # BUY
        timeInForce=xt.VALIDITY_DAY,              # DAY
        disclosedQuantity=0,
        orderQuantity=int(os.getenv("XTS_QTY", "1")),
        limitPrice=0,
        stopPrice=0,
        orderUniqueIdentifier=os.getenv("XTS_TAG", "demo-order-1"),
    )

    print("Place order response:", order_resp)

    # --- Optional sanity: fetch your order book ---
    ob = xt.get_order_book()
    print("Order book entries:", len(ob.get("result", [])))

if __name__ == "__main__":
    main()

