#!/usr/bin/env python3
"""
Schedule & place XTS orders from orders.json with dual-exchange (NSE/BSE) resolution.

- Loads two XTS SDKs with different root URLs (NSE & BSE) from a *copy* of the SDK directory.
- Logs in interactively to both sessions at start.
- Pre-downloads masters and resolves each symbol once (NSE preferred, fallback to BSE).
- Schedules placements using "time" in orders.json minus early_guard_ms.
- Places exactly at schedule time (best-effort) and writes a CSV log of results.

Requires env (.env) like:
  XTS_SOURCE=WEBAPI
  XTS_NSE_API_KEY=...
  XTS_NSE_API_SECRET=...
  XTS_NSE_ROOT=https://nse-root-url
  XTS_BSE_API_KEY=...
  XTS_BSE_API_SECRET=...
  XTS_BSE_ROOT=https://bse-root-url

Optional:
  XTS_ORDERS_JSON=./orders.json
  XTS_TAG_PREFIX=mytag

Notes:
- 'isAMO' is kept True (same as your original). Change IS_AMO_DEFAULT below if needed.
- Times in orders.json are interpreted in Asia/Kolkata for *today*.
"""

import os
import sys
import json
import csv
import time
import shutil
import configparser
import importlib.util
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Config ---
TIMEZONE = ZoneInfo("Asia/Kolkata")  # Schedule in IST

# --- .env (optional) ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
SDK_REPO_DIR = str(BASE_DIR)  # points to folder that contains the official SDK repo
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# ------------- Helpers -------------

def require(var: str) -> str:
    v = os.getenv(var, "").strip()
    if not v:
        raise SystemExit(f"Missing required env var: {var}. Edit your .env first.")
    return v

def load_sdk_with_root(sdk_src_dir: str, module_name: str, root_url: str):
    """
    Load the official SDK from a *copy* of the repo folder with config.ini patched to root_url.
    This ensures two independent module namespaces (NSE, BSE) with different defaults.
    """
    if not os.path.isdir(sdk_src_dir):
        raise SystemExit("SDK repo folder xts-pythonclient-api-sdk not found. Place script beside the SDK or set SDK_REPO_DIR.")

    sdk_copy = os.path.join(os.path.dirname(__file__), f"{module_name}_sdkcopy")
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

        conf_path = os.path.join(sdk_copy, "config.ini")
        cfg = configparser.ConfigParser()
        cfg.read(conf_path)
        if "root_url" not in cfg:
            cfg["root_url"] = {}
        cfg["root_url"]["root"] = root_url
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

    connect_py = os.path.join(sdk_copy, "Connect.py")
    spec = importlib.util.spec_from_file_location(module_name, connect_py)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

def try_pass_root(connect_cls, api_key, api_secret, source, root_url):
    try:
        return connect_cls(api_key, api_secret, source, root=root_url)
    except TypeError:
        return connect_cls(api_key, api_secret, source)

def fetch_master_map(xt, exchange_segment) -> dict[str, int]:
    """
    Download master for `exchange_segment` and return {symbol: exchangeInstrumentID}.
    Assumes '|' delimited with at least: [0]=..., [1]=id, [2]=..., [3]=symbol
    """
    resp = xt.get_master(exchangeSegmentList=[exchange_segment])
    raw = resp.get("result") if isinstance(resp, dict) else resp
    if not raw:
        raise SystemExit("Master download returned empty result.")
    mapping: dict[str, int] = {}
    for line in str(raw).splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            symbol = parts[3]
            try:
                eid = int(parts[1])
                mapping[symbol] = eid
            except Exception:
                continue
    return mapping

def map_consts(xt, *, side: str, order_type: str, product: str, tif: str):
    side = side.upper()
    order_type = order_type.upper()
    product = product.upper()
    tif = tif.upper()

    side_const = xt.TRANSACTION_TYPE_BUY if side == "BUY" else xt.TRANSACTION_TYPE_SELL
    if order_type == "MARKET":
        order_type_const = xt.ORDER_TYPE_MARKET
    elif order_type == "LIMIT":
        order_type_const = xt.ORDER_TYPE_LIMIT
    else:
        raise ValueError(f"Unsupported order type: {order_type}")

    # Product mappings (SDK typically exposes PRODUCT_MIS, PRODUCT_NRML etc.)
    if hasattr(xt, "PRODUCT_" + product):
        product_const = getattr(xt, "PRODUCT_" + product)
    else:
        raise ValueError(f"Unsupported product: {product}")

    # Time-in-force (assume DAY is available)
    if tif == "DAY":
        tif_const = xt.VALIDITY_DAY
    else:
        # add more if needed
        raise ValueError(f"Unsupported TIF: {tif}")

    return side_const, order_type_const, product_const, tif_const

def parse_orders_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    early_guard_ms = int(data.get("early_guard_ms", 0))
    orders = data.get("orders", [])
    return early_guard_ms, orders

def to_dt_today_ist(hhmmss: str) -> datetime:
    h, m, s = [int(x) for x in hhmmss.split(":")]
    today = date.today()
    return datetime(today.year, today.month, today.day, h, m, s, tzinfo=TIMEZONE)

def csv_logger():
    log_path = LOGS_DIR / f"order_log_{datetime.now(TIMEZONE).strftime('%Y%m%d')}.csv"
    new_file = not log_path.exists()
    f = open(log_path, "a", newline="", encoding="utf-8")
    writer = csv.writer(f)
    if new_file:
        writer.writerow([
            "ts", "planned_ts", "symbol", "qty", "side", "type", "limit",
            "product", "tif", "exchange", "exchangeInstrumentID", "status", "message", "order_tag"
        ])
        f.flush()
    return f, writer, log_path

# ------------- Main flow -------------

def main():
    source = os.getenv("XTS_SOURCE", "WEBAPI")

    nse_api_key    = require("XTS_NSE_API_KEY")
    nse_api_secret = require("XTS_NSE_API_SECRET")
    nse_root       = require("XTS_NSE_ROOT")

    bse_api_key    = require("XTS_BSE_API_KEY")
    bse_api_secret = require("XTS_BSE_API_SECRET")
    bse_root       = require("XTS_BSE_ROOT")

    tag_prefix     = os.getenv("XTS_TAG_PREFIX", "sched")
    orders_json    = Path(os.getenv("XTS_ORDERS_JSON", "orders.json")).resolve()

    # --- Load two SDK modules with different roots ---
    # Expect the official SDK repo folder is alongside this script, named exactly as provided by vendor.
    # If your SDK lives elsewhere, point SDK_REPO_DIR env to it or change SDK_REPO_DIR above.
    sdk_src_dir = str(BASE_DIR / "xts-pythonclient-api-sdk")
    if not os.path.isdir(sdk_src_dir):
        # Fallback: allow using BASE_DIR itself if the SDK files are right here
        sdk_src_dir = str(BASE_DIR)

    nse_mod = load_sdk_with_root(sdk_src_dir, "xts_nse", nse_root)
    bse_mod = load_sdk_with_root(sdk_src_dir, "xts_bse", bse_root)

    XTSN = nse_mod.XTSConnect
    XTSB = bse_mod.XTSConnect

    # --- Create clients ---
    xt_nse = try_pass_root(XTSN, nse_api_key, nse_api_secret, source, nse_root)
    xt_bse = try_pass_root(XTSB, bse_api_key, bse_api_secret, source, bse_root)

    # --- Interactive logins ---
    print(f"[{datetime.now(TIMEZONE).strftime('%H:%M:%S')}] NSE interactive login...")
    lr_n = None
    try:
        lr_n = xt_nse.interactive_login()
        if not isinstance(lr_n, dict) or lr_n.get("type") != "success":
            print(f"NSE interactive login failed: {lr_n}")
            lr_n = None
    except Exception as e:
        print(f"NSE interactive login exception: {e}")
        lr_n = None

    print(f"[{datetime.now(TIMEZONE).strftime('%H:%M:%S')}] BSE interactive login...")
    lr_b = xt_bse.interactive_login()
    if not isinstance(lr_b, dict) or lr_b.get("type") != "success":
        raise SystemExit(f"BSE interactive login failed: {lr_b}")

    # --- Masters / symbol resolution (NSE preferred) ---
    nse_map = {}
    if lr_n:
        try:
            nse_seg = getattr(xt_nse, "EXCHANGE_NSECM")
            nse_map = fetch_master_map(xt_nse, nse_seg)
            print(f"NSE master loaded: {len(nse_map)} symbols")
        except Exception as e:
            print(f"Failed to load NSE master: {e}")
            nse_map = {}

    bse_seg = getattr(xt_bse, "EXCHANGE_BSECM")
    bse_map = fetch_master_map(xt_bse, bse_seg)
    print(f"BSE master loaded: {len(bse_map)} symbols")

    def resolve_symbol(symbol: str):
        """Return (exch_name, xt, exch_segment, exchangeInstrumentID) or None."""
        if symbol in nse_map:
            return ("NSE", xt_nse, getattr(xt_nse, "EXCHANGE_NSECM"), nse_map[symbol])
        if symbol in bse_map:
            return ("BSE", xt_bse, getattr(xt_bse, "EXCHANGE_BSECM"), bse_map[symbol])
        return None

    # --- Load orders ---
    early_guard_ms, orders_raw = parse_orders_json(orders_json)
    print(f"Loaded {len(orders_raw)} orders from {orders_json}")
    guard_delta = timedelta(milliseconds=max(0, early_guard_ms))

    # --- Prepare schedule list (pre-validate & resolve) ---
    sched_items = []
    errors_early = []

    for idx, o in enumerate(orders_raw, start=1):
        try:
            symbol  = str(o["symbol"]).strip()
            qty     = int(o["qty"])
            side    = str(o.get("side", "BUY")).upper()
            otype   = str(o.get("type", "MARKET")).upper()
            limit_p = float(o.get("limit", 0.0) or 0.0)
            product = str(o.get("product", "MIS")).upper()
            tif     = str(o.get("tif", "DAY")).upper()
            tstr    = str(o["time"])  # 'HH:MM:SS'
            ISAMO = str(o.get("ISAMO", True)).capitalize()

            planned = to_dt_today_ist(tstr) - guard_delta
            now = datetime.now(TIMEZONE)
            # If planned time already passed, nudge to immediate (50ms)
            if planned <= now:
                print(f'Trade time passed for {symbol} time {planned}')
                continue
                # planned = now + timedelta(milliseconds=50)

            res = resolve_symbol(symbol)
            if not res:
                errors_early.append((symbol, "Not in NSE or BSE master"))
                continue

            exch_name, xt, exch_seg, exchangeInstrumentID = res
            tag = f"{tag_prefix}-{symbol}-{now.strftime('%H%M%S')}-{idx}"

            sched_items.append({
                "planned": planned,
                "planned_str": planned.strftime("%Y-%m-%d %H:%M:%S.%f %Z"),
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "type": otype,
                "limit": limit_p,
                "product": product,
                "tif": tif,
                "exchange": exch_name,
                "xt": xt,
                "exch_seg": exch_seg,
                "eid": exchangeInstrumentID,
                "tag": tag,
                "ISAMO": ISAMO
            })
        except Exception as e:
            errors_early.append((o.get("symbol", f"#{idx}"), f"Parse/prepare error: {e}"))

    # Sort by planned time
    sched_items.sort(key=lambda x: x["planned"])

    # Print prep summary
    for sym, msg in errors_early:
        print(f"[SKIP] {sym}: {msg}")
    print(f"Scheduled {len(sched_items)} orders; {len(errors_early)} skipped during preparation.")

    # --- CSV logger ---
    log_file, writer, log_path = csv_logger()
    print(f"Logging to: {log_path}")

    # --- Placement loop ---
    for item in sched_items:
        planned = item["planned"]
        ISAMO = item["ISAMO"]
        now = datetime.now(TIMEZONE)
        # Sleep until planned time
        to_sleep = (planned - now).total_seconds()
        xt = item["xt"]
        side_c, type_c, prod_c, tif_c = map_consts(
            xt,
            side=item["side"],
            order_type=item["type"],
            product=item["product"],
            tif=item["tif"]
        )

        # For LIMIT vs MARKET
        limit_px = item["limit"] if item["type"] == "LIMIT" else 0.0
        if to_sleep > 0:
            print(f'Sleeping till {planned} for {item["symbol"]}')
            time.sleep(to_sleep)
        try:
            resp = xt.place_order(
                exchangeSegment=item["exch_seg"],
                exchangeInstrumentID=item["eid"],
                productType=prod_c,
                orderType=type_c,
                orderSide=side_c,
                timeInForce=tif_c,
                disclosedQuantity=0,
                orderQuantity=item["qty"],
                limitPrice=limit_px,
                stopPrice=0,
                orderUniqueIdentifier=item["tag"],
                apiOrderSource=os.getenv("XTS_SOURCE", "WEBAPI"),
                isAMO=ISAMO
            )
            print(f"[{datetime.now(TIMEZONE).strftime('%H:%M:%S.%f')[:-3]}] Placing {item['side']} {item['qty']} {item['symbol']} "
                  f"{item['type']}@{limit_px} on {item['exchange']} (tif={item['tif']}, prod={item['product']})")

            status = "success" if isinstance(resp, dict) and resp.get("type") == "success" else "sent"
            msg = json.dumps(resp, ensure_ascii=False)[:2000]  # cap for CSV
        except Exception as e:
            status = "error"
            msg = f"{type(e).__name__}: {e}"

        writer.writerow([
            datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S.%f %Z"),
            item["planned_str"],
            item["symbol"], item["qty"], item["side"], item["type"], item["limit"],
            item["product"], item["tif"],
            item["exchange"], item["eid"],
            status, msg, item["tag"]
        ])
        log_file.flush()
    import pdb; pdb.set_trace()
    print("All scheduled orders processed.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user. Exiting.")
