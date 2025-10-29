# xts_dual_exchange_demo.py
"""
Create two XTS sessions (NSE & BSE) with different root URLs.
Resolve symbol in NSE master first; if not found, fallback to BSE.
Place a MARKET order (MIS) on the chosen exchange.
"""
import os, sys, shutil, configparser, importlib.util
from datetime import datetime

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_SDK = os.path.dirname(__file__)

def require(var: str) -> str:
    v = os.getenv(var, "").strip()
    if not v:
        raise SystemExit(f"Missing required env var: {var}. Edit your .env first.")
    return v


def get_symbols_to_trade(path: str | Path) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Safely handle missing keys
    orders = data.get("orders", [])
    return [(o["symbol"], o['qty'], o['side'], o['limit']) for o in orders]


def load_sdk_with_root(sdk_src_dir: str, module_name: str, root_url: str):
    """
    Load the official SDK from a *copy* of the repo folder with config.ini patched to root_url.
    This ensures two independent module namespaces (NSE, BSE) with different defaults.
    """
    if not os.path.isdir(sdk_src_dir):
        raise SystemExit("SDK repo folder xts-pythonclient-api-sdk not found. Run the run_dual script first.")

    sdk_copy = os.path.join(os.path.dirname(__file__), f"{module_name}_sdkcopy")
    # Rebuild if missing or if root marker changed
    marker = os.path.join(sdk_copy, ".root_url")
    need_copy = True
    if os.path.isdir(sdk_copy):
        try:
            with open(marker, "r", encoding="utf-8") as f:
                if f.read().strip() == root_url:
                    need_copy = False
        except Exception:
            pass

    if need_copy:
        if os.path.isdir(sdk_copy):
            shutil.rmtree(sdk_copy)
        shutil.copytree(sdk_src_dir, sdk_copy)
        # patch config.ini
        conf_path = os.path.join(sdk_copy, "config.ini")
        cfg = configparser.ConfigParser()
        cfg.read(conf_path)
        if "root_url" not in cfg:
            cfg["root_url"] = {}
        cfg["root_url"]["root"] = root_url
        # default configs required by SDK
        if "user" not in cfg:
            cfg["user"] = {}
        cfg["user"]["source"] = os.getenv("XTS_SOURCE", "WEBAPI")
        if "SSL" not in cfg:
            cfg["SSL"] = {}
        cfg["SSL"]["disable_ssl"] = "True"
        with open(conf_path, "w", encoding="utf-8") as f:
            cfg.write(f)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(root_url)

    # Dynamically import Connect.py from the copied SDK
    connect_py = os.path.join(sdk_copy, "Connect.py")
    spec = importlib.util.spec_from_file_location(module_name, connect_py)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

def try_pass_root(connect_cls, api_key, api_secret, source, root_url):
    """
    Some SDK variants accept root=... in the constructor.
    Prefer that. If not, just call the 3-arg version.
    """
    try:
        return connect_cls(api_key, api_secret, source, root=root_url)
    except TypeError:
        return connect_cls(api_key, api_secret, source)

def find_instrument_id_from_master(xt, exchange_segment, symbol: str) -> int:
    """Parse the '|' delimited master to return exchangeInstrumentID for exact symbol match."""
    resp = xt.get_master(exchangeSegmentList=[exchange_segment])
    raw = resp.get("result") if isinstance(resp, dict) else resp
    if not raw:
        raise SystemExit("Master download returned empty result.")
    for line in str(raw).splitlines():
        parts = line.split("|")
        if len(parts) >= 5 and parts[3] == symbol:
            try:
                return int(parts[1])
            except ValueError:
                pass
    return -1

def main():
    # --- Read env ---
    source = os.getenv("XTS_SOURCE", "WEBAPI")

    nse_api_key    = require("XTS_NSE_API_KEY")
    nse_api_secret = require("XTS_NSE_API_SECRET")
    nse_root       = require("XTS_NSE_ROOT")

    bse_api_key    = require("XTS_BSE_API_KEY")
    bse_api_secret = require("XTS_BSE_API_SECRET")
    bse_root       = require("XTS_BSE_ROOT")



    symbol = os.getenv("XTS_SYMBOL", "RELIANCE-EQ").strip()
    qty    = int(os.getenv("XTS_QTY", "1"))
    side   = os.getenv("XTS_SIDE", "BUY").upper()
    tag    = os.getenv("XTS_TAG", f"dual-{datetime.now().strftime('%Y%m%d%H%M%S')}")

    if side not in ("BUY", "SELL"):
        raise SystemExit("XTS_SIDE must be BUY or SELL")

    # --- Load two SDK modules with different roots ---

    nse_mod = load_sdk_with_root(BASE_SDK, "xts_nse", nse_root)
    bse_mod = load_sdk_with_root(BASE_SDK, "xts_bse", bse_root)

    XTSN = nse_mod.XTSConnect
    XTSB = bse_mod.XTSConnect

    # --- Create clients (try to pass root if supported) ---
    xt_nse = try_pass_root(XTSN, nse_api_key, nse_api_secret, source, nse_root)
    xt_bse = try_pass_root(XTSB, bse_api_key, bse_api_secret, source, bse_root)
    print(nse_api_key, nse_api_secret, nse_root, bse_api_key, bse_root)
    # import pdb; pdb.set_trace()

    # --- Interactive login both ---
    print(f"[{datetime.now().strftime('%H:%M:%S')}] NSE interactive login...")
    try:
        lr_n = xt_nse.interactive_login()
        if not isinstance(lr_n, dict) or lr_n.get("type") != "success":
            print(f'NSE interactive login failed: {lr_n}')
        #raise SystemExit(f"NSE interactive login failed: {lr_n}")
    except:
        print(f'NSE interactive login failed')
        lr_n = None

    print(f"[{datetime.now().strftime('%H:%M:%S')}] BSE interactive login...")
    lr_b = xt_bse.interactive_login()
    if not isinstance(lr_b, dict) or lr_b.get("type") != "success":
        raise SystemExit(f"BSE interactive login failed: {lr_b}")

    # --- Resolve instrument: NSE first ---
    chosen = None
    if isinstance(lr_n, dict):
        print(f"Resolving symbol '{symbol}'... NSE first, then BSE")
        eid = find_instrument_id_from_master(xt_nse, getattr(xt_nse, "EXCHANGE_NSECM"), symbol)
        if eid != -1:
            chosen = ("NSE", xt_nse, getattr(xt_nse, "EXCHANGE_NSECM"), eid)
    if not chosen:
        eid_b = find_instrument_id_from_master(xt_bse, getattr(xt_bse, "EXCHANGE_BSECM"), symbol)
        if eid_b != -1:
            chosen = ("BSE", xt_bse, getattr(xt_bse, "EXCHANGE_BSECM"), eid_b)

    if not chosen:
        raise SystemExit(f"Symbol '{symbol}' not found in NSE or BSE masters.")

    exch_name, xt, exch_segment, exchange_instrument_id = chosen
    print(f"Chosen exchange: {exch_name}  ({symbol} -> {exchange_instrument_id})")

    # --- Map side ---
    side_const = xt.TRANSACTION_TYPE_BUY if side == "BUY" else xt.TRANSACTION_TYPE_SELL

    # --- Place order ---
    print(f"Placing {side} {qty} @ MARKET on {exch_name} (MIS, DAY)...")

    order_resp = xt.place_order(
        exchangeSegment=exch_segment,
        exchangeInstrumentID=exchange_instrument_id,
        productType=xt.PRODUCT_MIS,
        orderType=xt.ORDER_TYPE_MARKET,
        orderSide=side_const,
        timeInForce=xt.VALIDITY_DAY,
        disclosedQuantity=0,
        orderQuantity=qty,
        limitPrice=0,
        stopPrice=0,
        orderUniqueIdentifier=tag,
        apiOrderSource=source,
        isAMO=True
    )
    print("Order response:", order_resp)
    import pdb; pdb.set_trace()

    # --- Order book sanity ---
    try:
        ob = xt.get_order_book()
        size = len(ob.get("result", [])) if isinstance(ob, dict) else 0
        print(f"{exch_name} order book entries:", size)
    except Exception as e:
        print(f"get_order_book failed: {e}")


if __name__ == "__main__":
    main()

