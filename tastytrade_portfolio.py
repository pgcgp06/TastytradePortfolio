r"""Tastytrade portfolio fetcher.

Authenticates to the Tastytrade API with OAuth2, pulls positions and buying
power for your account, and generates dashboard.html (the Options Portfolio
Manager) with your live data embedded. Run it interactively:

    py tastytrade_portfolio.py

Tastytrade discontinued username/password session tokens on Dec 1 2025;
personal API access now uses an OAuth2 refresh token. One-time setup: on
tastytrade.com, open Manage > My Profile > API, create a personal OAuth
application (copy its client secret, shown only once) and then create a grant
to get a refresh token. See https://developer.tastytrade.com/oauth/.

First run asks for that client secret and refresh token; both are stored in
%LOCALAPPDATA%\TastytradePortfolio\oauth.json so later runs are non-interactive.
The refresh token doesn't expire (revoke it on the site anytime); each run
exchanges it for a short-lived (~15 min) access token.

Standard library only - no packages to install.
"""

import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
import webbrowser
from datetime import date
from pathlib import Path

BASE = "https://api.tastyworks.com"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TastytradePortfolio"
CONFIG_FILE = APP_DIR / "oauth.json"
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIR / "dashboard-template.html"
OUTPUT_HTML = SCRIPT_DIR / "dashboard.html"
OUTPUT_JSON = SCRIPT_DIR / "portfolio.json"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "tastytrade-portfolio-dashboard/1.0",
}


def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    for k, v in HEADERS.items():
        req.add_header(k, v)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def login():
    """Exchange a stored OAuth2 refresh token for a short-lived access token.

    On first run, prompts for the client secret and refresh token from a personal
    OAuth application/grant (tastytrade.com > Manage > My Profile > API) and saves
    them so later runs are non-interactive.
    """
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            cfg = {}

    client_secret = cfg.get("client_secret")
    refresh_token = cfg.get("refresh_token")
    if not (client_secret and refresh_token):
        print("First-time OAuth2 setup.")
        print("On tastytrade.com: Manage > My Profile > API > OAuth Applications,")
        print("create a personal application and a grant, then paste the values")
        print("below (docs: https://developer.tastytrade.com/oauth/).")
        client_secret = client_secret or getpass.getpass("  Client secret: ").strip()
        refresh_token = refresh_token or getpass.getpass("  Refresh token: ").strip()
        save_oauth(client_secret, refresh_token)

    resp = api("POST", "/oauth/token", {
        "grant_type": "refresh_token",
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })
    token = resp.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in /oauth/token response: {resp}")
    print("Logged in via OAuth2 (access token valid ~15 min).")
    return token


def save_oauth(client_secret, refresh_token):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }))


def pick_account(token):
    resp = api("GET", "/customers/me/accounts", token=token)
    accounts = [it["account"] for it in resp["data"]["items"]
                if not it["account"].get("closed-at")]
    if not accounts:
        sys.exit("No open accounts found.")
    if len(accounts) == 1:
        acct = accounts[0]["account-number"]
        print(f"Account: {acct}")
        return acct
    print("Accounts:")
    for i, a in enumerate(accounts, 1):
        print(f"  {i}. {a['account-number']}  {a.get('nickname') or a.get('account-type-name', '')}")
    while True:
        choice = input(f"Pick account [1-{len(accounts)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(accounts):
            return accounts[int(choice) - 1]["account-number"]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_occ(symbol):
    """Decode an OCC option symbol like 'AAPL  260711P00180000'."""
    parts = symbol.split()
    if len(parts) < 2:
        return None
    tail = parts[-1]
    if len(tail) != 15:
        return None
    yymmdd, cp, strike8 = tail[:6], tail[6], tail[7:]
    if cp not in "CP" or not (yymmdd + strike8).isdigit():
        return None
    return {
        "symbol": parts[0],
        "expiration": f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
        "type": "call" if cp == "C" else "put",
        "strike": int(strike8) / 1000.0,
    }


# Fallback dollar multiplier per point, by futures root, when the API doesn't
# return one on the position. Covers the common micro/mini and full-size
# products; anything unknown falls back to 1 (see build_positions).
FUTURE_MULT = {
    "/MCL": 100, "/CL": 1000, "/QM": 500, "/MNG": 1000, "/NG": 10000,
    "/MES": 5, "/ES": 50, "/MNQ": 2, "/NQ": 20, "/M2K": 5, "/RTY": 50,
    "/MYM": 0.5, "/YM": 5, "/MGC": 10, "/GC": 100, "/SIL": 1000, "/SI": 5000,
    "/HG": 25000, "/ZC": 50, "/ZS": 50, "/ZW": 50, "/ZM": 100, "/ZL": 600,
    "/ZB": 1000, "/ZN": 1000, "/ZF": 1000, "/ZT": 2000, "/UB": 1000,
    "/6E": 125000, "/6J": 12500000, "/6B": 62500, "/6A": 100000,
    "/6C": 100000, "/6S": 125000, "/6M": 500000,
}


def future_root(underlying):
    """'/MCLQ6' -> '/MCL' (strip the trailing month+year code)."""
    if not underlying or not underlying.startswith("/"):
        return underlying
    return re.sub(r"[FGHJKMNQUVXZ]\d{1,2}$", "", underlying)


def parse_future_option(symbol):
    """Decode a future-option symbol tail like './MCLQ6MCOQ6 260716P70'.

    Returns expiration/type/strike from the trailing '<YYMMDD><C|P><strike>'
    token; the underlying comes from the position's underlying-symbol field.
    """
    tail = symbol.split()[-1]
    m = re.match(r"^(\d{6})([CP])(\d+(?:\.\d+)?)$", tail)
    if not m:
        return None
    yymmdd, cp, strike = m.group(1), m.group(2), m.group(3)
    return {
        "expiration": f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
        "type": "call" if cp == "C" else "put",
        "strike": float(strike),
    }


def fetch_marks(token, symbols):
    """Best-effort current prices for underlyings (equities and futures)."""
    if not symbols:
        return {}
    equities = sorted(s for s in symbols if not s.startswith("/"))
    futures = sorted(s for s in symbols if s.startswith("/"))
    params = []
    if equities:
        params.append("equity=" + urllib.request.quote(",".join(equities), safe=','))
    if futures:
        params.append("future=" + urllib.request.quote(",".join(futures), safe=','))
    if not params:
        return {}
    try:
        resp = api("GET", "/market-data/by-type?" + "&".join(params), token=token)
        marks = {}
        for it in resp["data"]["items"]:
            mark = num(it.get("mark")) or num(it.get("last")) or num(it.get("close"))
            if mark:
                marks[it["symbol"]] = mark
        return marks
    except (RuntimeError, KeyError) as e:
        print(f"  (couldn't fetch underlying quotes - fill prices in the dashboard: {e})")
        return {}


def build_positions(token, acct):
    resp = api("GET", f"/accounts/{acct}/positions", token=token)
    items = resp["data"]["items"]
    positions, skipped, underlyings = [], [], set()

    for it in items:
        underlyings.add(it.get("underlying-symbol") or it["symbol"])
    marks = fetch_marks(token, underlyings)

    for it in items:
        itype = it.get("instrument-type", "")
        qty = num(it.get("quantity")) or 0
        if it.get("quantity-direction") == "Short":
            qty = -abs(qty)
        avg_open = num(it.get("average-open-price"))
        close = num(it.get("close-price"))
        und_sym = it.get("underlying-symbol") or it["symbol"]
        und_price = marks.get(und_sym) or (close if itype == "Equity" else None) or 0

        if itype == "Equity":
            positions.append({
                "symbol": it["symbol"], "type": "stock", "qty": qty,
                "costBasis": avg_open or 0, "underlyingPrice": und_price,
            })
        elif itype == "Equity Option":
            occ = parse_occ(it["symbol"])
            if not occ:
                skipped.append(it["symbol"])
                continue
            side = "short" if qty < 0 else "long"
            pos = {
                "symbol": occ["symbol"], "type": occ["type"], "side": side,
                "strike": occ["strike"], "expiration": occ["expiration"],
                "qty": abs(qty), "currentPrice": close,
                "underlyingPrice": marks.get(und_sym) or 0,
            }
            pos["credit" if side == "short" else "cost"] = avg_open
            positions.append(pos)
        elif itype == "Future Option":
            fo = parse_future_option(it["symbol"])
            if not fo:
                skipped.append(it["symbol"])
                continue
            side = "short" if qty < 0 else "long"
            mult = num(it.get("multiplier")) or FUTURE_MULT.get(future_root(und_sym))
            pos = {
                "symbol": und_sym, "type": fo["type"], "side": side,
                "strike": fo["strike"], "expiration": fo["expiration"],
                "qty": abs(qty), "currentPrice": close,
                "underlyingPrice": marks.get(und_sym) or 0, "future": True,
            }
            if mult:
                pos["multiplier"] = mult
            pos["credit" if side == "short" else "cost"] = avg_open
            positions.append(pos)
        elif itype == "Future":
            pos = {
                "symbol": und_sym, "type": "stock", "qty": qty,
                "costBasis": avg_open or 0,
                "underlyingPrice": marks.get(und_sym) or close or 0, "future": True,
            }
            mult = num(it.get("multiplier")) or FUTURE_MULT.get(future_root(und_sym))
            if mult:
                pos["multiplier"] = mult
            positions.append(pos)
        else:
            skipped.append(f"{it['symbol']} ({itype})")

    if skipped:
        print("  Skipped (not supported by the analyzer): " + ", ".join(skipped))
    return positions


def fetch_balances(token, acct):
    """Return (net_liq, option_bp, stock_bp) from the account balances.

    option_bp = derivative buying power (available for options);
    stock_bp  = equity buying power (available for stock, ~2x net liq on Reg-T).
    Total capital deployed is net_liq - option_bp (the broker's BP usage).
    """
    resp = api("GET", f"/accounts/{acct}/balances", token=token)
    d = resp["data"]
    net_liq = num(d.get("net-liquidating-value"))
    option_bp = num(d.get("derivative-buying-power"))
    stock_bp = num(d.get("equity-buying-power"))
    print(f"  Net liq: {net_liq}   Option BP: {option_bp}   Stock BP: {stock_bp}")
    return net_liq, option_bp, stock_bp


def chunks(seq, n=90):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_market_metrics(token, symbols):
    """Best-effort IVR / liquidity / beta / earnings per symbol (batched)."""
    metrics = {}
    for batch in chunks(list(symbols)):
        try:
            q = ",".join(sorted(batch))
            resp = api("GET", f"/market-metrics?symbols={urllib.request.quote(q, safe=',')}", token=token)
        except RuntimeError as e:
            print(f"  (couldn't fetch market metrics - screen shows '?': {e})")
            continue
        for it in resp.get("data", {}).get("items", []):
            sym = it.get("symbol")
            if not sym:
                continue
            ivr = num(it.get("implied-volatility-index-rank")) or num(it.get("tos-implied-volatility-index-rank"))
            liq = (num(it.get("liquidity-rating")) or num(it.get("liquidity-rank"))
                   or num(it.get("liquidity-value")))
            earn_days = None
            edate = (it.get("earnings") or {}).get("expected-report-date")
            if edate:
                try:
                    earn_days = (date.fromisoformat(edate[:10]) - date.today()).days
                except ValueError:
                    earn_days = None
            metrics[sym] = {
                # IVR comes back as a 0-1 fraction; the dashboard also normalizes.
                "ivr": round(ivr * 100, 1) if ivr is not None and ivr <= 1.5 else ivr,
                "liquidity": int(liq) if liq is not None else None,
                "beta": num(it.get("beta")),
                "earningsDays": earn_days,
            }
    return metrics


def fetch_quotes(token, symbols):
    """Best-effort price + 52-week high/low per symbol (batched)."""
    quotes = {}
    for batch in chunks(list(symbols)):
        try:
            q = ",".join(sorted(batch))
            resp = api("GET", f"/market-data/by-type?equity={urllib.request.quote(q, safe=',')}", token=token)
        except RuntimeError as e:
            print(f"  (couldn't fetch watchlist quotes: {e})")
            continue
        for it in resp.get("data", {}).get("items", []):
            sym = it.get("symbol")
            if not sym:
                continue
            quotes[sym] = {
                "price": num(it.get("mark")) or num(it.get("last")) or num(it.get("close")),
                "yearHigh": num(it.get("year-high-price")),
                "yearLow": num(it.get("year-low-price")),
            }
    return quotes


def build_watchlist(token):
    """Pull the user's Tastytrade watchlists and enrich each equity symbol with
    the metrics the dashboard screens on (price, IVR, liquidity, beta, earnings,
    52-week range). Returns a list of dicts ready for the dashboard."""
    try:
        resp = api("GET", "/watchlists", token=token)
    except RuntimeError as e:
        print(f"  (couldn't fetch watchlists: {e})")
        return []
    first_list, order = {}, []
    for wl in resp.get("data", {}).get("items", []):
        name = wl.get("name") or "Watchlist"
        for entry in wl.get("watchlist-entries", []) or []:
            sym = entry.get("symbol")
            itype = (entry.get("instrument-type") or "").lower()
            if not sym or sym.startswith("/") or " " in sym:
                continue  # skip futures and option symbols
            if itype and "equity" not in itype and "etf" not in itype:
                continue
            if sym not in first_list:
                first_list[sym] = name
                order.append(sym)
    if not order:
        print("  No equity symbols found in your watchlists.")
        return []
    print(f"  {len(order)} watchlist symbols - fetching metrics and quotes...")
    metrics = fetch_market_metrics(token, order)
    quotes = fetch_quotes(token, order)
    watchlist = []
    for sym in order:
        m = metrics.get(sym, {})
        qv = quotes.get(sym, {})
        watchlist.append({
            "symbol": sym, "list": first_list[sym],
            "price": qv.get("price"), "ivr": m.get("ivr"),
            "liquidity": m.get("liquidity"), "beta": m.get("beta"),
            "earningsDays": m.get("earningsDays"),
            "yearHigh": qv.get("yearHigh"), "yearLow": qv.get("yearLow"),
        })
    return watchlist


def main():
    print("Tastytrade Portfolio Dashboard")
    print("-" * 40)
    token = login()
    acct = pick_account(token)
    print("Fetching positions...")
    positions = build_positions(token, acct)
    print(f"  {len(positions)} positions mapped.")
    net_liq, option_bp, stock_bp = fetch_balances(token, acct)

    # The API supplies positions + buying power; you upload your watchlist CSV in
    # the dashboard. That raw export has no 52-week columns, so pre-fetch the
    # ranges for your account's watchlist symbols here and embed them as a lookup
    # — the dashboard fills them into the uploaded rows (near-high/near-low rule).
    print("Fetching 52-week ranges for your watchlist symbols...")
    api_wl = build_watchlist(token)
    ranges = {w["symbol"]: [w["yearLow"], w["yearHigh"]]
              for w in api_wl if w.get("yearLow") is not None and w.get("yearHigh") is not None}
    print(f"  52-week ranges for {len(ranges)} symbols.")

    vix = input("/VX level (Enter to set later in the dashboard): ").strip()
    payload = {
        "data": {"positions": positions, "watchlist": []},
        "netLiq": net_liq,
        "optionBp": option_bp,
        "stockBp": stock_bp,
        "vix": float(vix) if vix else None,
        "asOf": date.today().isoformat(),
        "ranges": ranges,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2))

    html = TEMPLATE.read_text(encoding="utf-8")
    marker_start, marker_end = "/*__DATA__*/", "/*__END__*/"
    a = html.index(marker_start)
    b = html.index(marker_end) + len(marker_end)
    html = html[:a] + json.dumps(payload) + html[b:]
    OUTPUT_HTML.write_text(html, encoding="utf-8")

    print(f"\nDashboard written to {OUTPUT_HTML}")
    print("Next: in the dashboard, click 'Upload file' and drop your watchlist CSV")
    print("to screen it for the top trade candidates against your positions.")
    webbrowser.open(OUTPUT_HTML.as_uri())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except RuntimeError as e:
        sys.exit(f"\nError: {e}")
