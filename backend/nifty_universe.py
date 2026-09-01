import json
import logging
from datetime import date
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "BHARTIARTL",
    "INFY", "ITC", "HINDUNILVR", "LT", "SBIN",
    "BAJFINANCE", "KOTAKBANK", "AXISBANK", "M&M", "MARUTI",
    "ASIANPAINT", "SUNPHARMA", "TITAN", "ULTRACEMCO", "TATASTEEL",
    "NTPC", "TATAMOTORS", "POWERGRID", "BAJAJFINSV", "NESTLEIND",
    "ADANIENT", "HCLTECH", "ONGC", "WIPRO", "JSWSTEEL",
    "ADANIPORTS", "HDFCLIFE", "LTIM", "SBILIFE", "COALINDIA",
    "TATACONSUM", "GRASIM", "BRITANNIA", "BAJAJ-AUTO", "EICHERMOT",
    "APOLLOHOSP", "DRREDDY", "HINDALCO", "CIPLA", "INDUSINDBK",
    "DIVISLAB", "TECHM", "HEROMOTOCO", "UPL", "BPCL"
]

_cached_nifty50 = None
_cached_on = None


def _fetch_nifty50_from_nse():
    """Fetch current NIFTY 50 constituents from NSE's public index endpoint."""
    user_agent = "Mozilla/5.0 (compatible; KiteAgenticTrading/1.0)"
    homepage_request = Request(
        "https://www.nseindia.com/",
        headers={"User-Agent": user_agent, "Accept": "text/html"},
    )

    try:
        with urlopen(homepage_request, timeout=10) as homepage:
            cookie_headers = homepage.headers.get_all("Set-Cookie", [])
    except HTTPError as error:
        # NSE can reject the HTML bootstrap while still issuing usable cookies.
        if error.code != 403:
            raise
        cookie_headers = error.headers.get_all("Set-Cookie", [])

    cookies = [value.split(";", 1)[0] for value in cookie_headers]

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/market-data/constituents",
    }
    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    index = quote("NIFTY 50")
    endpoints = (
        f"https://www.nseindia.com/api/equity-stock-indices?index={index}",
        f"https://www.nseindia.com/api/equity-stockIndices?index={index}",
    )
    response_data = None
    for endpoint in endpoints:
        try:
            with urlopen(Request(endpoint, headers=headers), timeout=10) as response:
                response_data = json.load(response)
            break
        except HTTPError as error:
            if error.code != 404:
                raise

    if not isinstance(response_data, dict):
        raise ValueError("NSE returned an unexpected NIFTY 50 response")

    rows = response_data.get("data", response_data.get("records", []))
    symbols = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if (
            isinstance(symbol, str)
            and symbol
            and symbol.upper() != "NIFTY 50"
            and symbol not in symbols
        ):
            symbols.append(symbol)

    if len(symbols) != 50:
        raise ValueError(f"NSE returned {len(symbols)} NIFTY 50 constituents")
    return symbols


def get_nifty50_universe():
    """Return today's live NIFTY 50 list, falling back to the bundled list."""
    global _cached_nifty50, _cached_on

    today = date.today()
    if _cached_nifty50 is not None and _cached_on == today:
        return list(_cached_nifty50)

    try:
        _cached_nifty50 = _fetch_nifty50_from_nse()
        _cached_on = today
        logging.info("Loaded current NIFTY 50 constituents from NSE")
    except Exception as error:
        logging.warning("Using bundled NIFTY 50 list: %s", error)
        _cached_nifty50 = list(NIFTY_50)
        _cached_on = today

    return list(_cached_nifty50)
