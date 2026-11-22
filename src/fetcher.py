"""台股官方公開籌碼與技術指標爬蟲引擎。

整合台灣證交所（TWSE）、台灣期交所（TAIFEX）與 Yahoo Finance 日 K 公開資料，
計算大戶期現貨籌碼、散戶小台/微台多空比、融資變化與推算維持率、台積電/金融均線結構與大盤日線 KD(9,3,3)。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LATEST_DATA_PATH = DATA_DIR / "latest_market_data.json"
TDCC_HISTORY_PATH = DATA_DIR / "tdcc_weekly_history.json"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 12


def _log_progress(msg: str) -> None:
    """即時印出同步進度（強制 flush 以免終端機輸出緩衝延遲）。"""
    print(f"[Sync] {msg}", flush=True)


def _parse_number(raw: Any) -> float | None:
    """解析含千分位逗號的數字字串，失敗回傳 None。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", "").replace("+", "")
    if not text or text in ("-", "--", "N/A", "nan"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _compute_ma(values: list[float], period: int) -> float | None:
    """計算移動平均線。"""
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def _compute_kd(candles: list[dict[str, Any]], period: int = 9) -> dict[str, float]:
    """計算標準台股日線 KD(9,3,3) 指標。"""
    if len(candles) < period:
        return {"k": 50.0, "d": 50.0, "prev_k": 50.0, "prev_d": 50.0}

    k_val = 50.0
    d_val = 50.0
    prev_k = 50.0
    prev_d = 50.0

    for idx in range(period - 1, len(candles)):
        window = candles[idx - period + 1 : idx + 1]
        high_n = max(c["high"] for c in window)
        low_n = min(c["low"] for c in window)
        close_now = candles[idx]["close"]
        if high_n == low_n:
            rsv = 50.0
        else:
            rsv = ((close_now - low_n) / (high_n - low_n)) * 100.0
        prev_k, prev_d = k_val, d_val
        k_val = (2.0 / 3.0) * k_val + (1.0 / 3.0) * rsv
        d_val = (2.0 / 3.0) * d_val + (1.0 / 3.0) * k_val

    return {
        "k": round(k_val, 2),
        "d": round(d_val, 2),
        "prev_k": round(prev_k, 2),
        "prev_d": round(prev_d, 2),
    }


def fetch_yahoo_candles(
    session: requests.Session, symbol: str, range_str: str = "6mo"
) -> list[dict[str, Any]]:
    """從 Yahoo Finance v8 Chart API 抓取日線 OHLCV（自動轉台灣時區日期）。"""
    encoded_sym = symbol.replace("^", "%5E")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_sym}"
        f"?range={range_str}&interval=1d"
    )
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    results = payload.get("chart", {}).get("result") or []
    if not results:
        return []

    res = results[0]
    timestamps = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    tz_taipei = dt.timezone(dt.timedelta(hours=8))
    candles: list[dict[str, Any]] = []
    seen_dates: set[str] = set()

    for i, ts in enumerate(timestamps):
        c = closes[i] if i < len(closes) else None
        h = highs[i] if i < len(highs) else None
        l_val = lows[i] if i < len(lows) else None
        o = opens[i] if i < len(opens) else None
        v = volumes[i] if i < len(volumes) else 0
        if c is None or h is None or l_val is None or o is None:
            continue
        date_str = dt.datetime.fromtimestamp(ts, tz=tz_taipei).strftime("%Y-%m-%d")
        if date_str in seen_dates:
            # Yahoo 偶爾在盤中與盤後重複最後一日，取最新一筆覆蓋
            candles = [x for x in candles if x["date"] != date_str]
        seen_dates.add(date_str)
        candles.append(
            {
                "date": date_str,
                "open": float(o),
                "high": float(h),
                "low": float(l_val),
                "close": float(c),
                "volume": float(v or 0),
            }
        )
    candles.sort(key=lambda x: x["date"])
    return candles


def fetch_twse_fmtqik(
    session: requests.Session, yyyymmdd: str
) -> list[dict[str, Any]]:
    """抓取台灣證交所 FMTQIK（當月大盤每日成交金額與指數）。"""
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={yyyymmdd}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("stat") != "OK" or not data.get("data"):
        return []

    rows: list[dict[str, Any]] = []
    for row in data["data"]:
        # 格式: ['115/11/20', '成交股數', '成交金額', '成交筆數', '發行量加權股價指數', '漲跌點數']
        roc_parts = str(row[0]).strip().split("/")
        if len(roc_parts) != 3:
            continue
        year = int(roc_parts[0]) + 1911
        iso_date = f"{year:04d}-{int(roc_parts[1]):02d}-{int(roc_parts[2]):02d}"
        amount = _parse_number(row[2]) or 0.0
        index_val = _parse_number(row[4]) or 0.0
        change_val = _parse_number(row[5]) or 0.0
        rows.append(
            {
                "date": iso_date,
                "amount_billion": round(amount / 1e8, 2),
                "close": index_val,
                "change": change_val,
            }
        )
    return rows


def fetch_twse_bfi82u_day(
    session: requests.Session, iso_date: str
) -> dict[str, float] | None:
    """抓取證交所單日三大法人買賣超金額（單位：億元）。"""
    yyyymmdd = iso_date.replace("-", "")
    url = f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&dayDate={yyyymmdd}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK" or not payload.get("data"):
        return None

    foreign_net = 0.0
    trust_net = 0.0
    dealer_net = 0.0
    for row in payload["data"]:
        name = str(row[0])
        diff = _parse_number(row[3]) or 0.0
        if "外資及陸資(不含外資自營商)" in name or "外資自營商" in name:
            foreign_net += diff
        elif "投信" in name:
            trust_net += diff
        elif "自營商" in name:
            dealer_net += diff

    return {
        "date": iso_date,
        "foreign_billion": round(foreign_net / 1e8, 2),
        "trust_billion": round(trust_net / 1e8, 2),
        "dealer_billion": round(dealer_net / 1e8, 2),
    }


def fetch_twse_margin_day(
    session: requests.Session, iso_date: str
) -> dict[str, Any] | None:
    """抓取證交所單日大盤融資融券餘額（融資金額換算為億元）。"""
    yyyymmdd = iso_date.replace("-", "")
    url = (
        f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
        f"?response=json&date={yyyymmdd}&selectType=MS"
    )
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK" or not payload.get("tables"):
        return None

    table0 = payload["tables"][0]
    for row in table0.get("data", []):
        if "融資金額" in str(row[0]):
            # 單位為仟元 -> 除以 100,000 為億元
            prev_k = _parse_number(row[4])
            curr_k = _parse_number(row[5])
            if prev_k is None or curr_k is None:
                return None
            return {
                "date": iso_date,
                "balance_billion": round(curr_k / 100000.0, 2),
                "prev_balance_billion": round(prev_k / 100000.0, 2),
                "change_1d_billion": round((curr_k - prev_k) / 100000.0, 2),
            }
    return None


def fetch_taifex_institutional_day(
    session: requests.Session, iso_date: str
) -> dict[str, Any] | None:
    """抓取期交所單日三大法人期貨未平倉淨額（台指期、小台、微台）。"""
    slash_date = iso_date.replace("-", "/")
    url = (
        "https://www.taifex.com.tw/cht/3/futContractsDateDown"
        f"?queryStartDate={slash_date}&queryEndDate={slash_date}"
    )
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    text = resp.content.decode("cp950", errors="replace").strip()
    if not text or "日期" not in text or "查無資料" in text:
        return None

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 4:
        return None

    tx_foreign_net: int | None = None
    mtx_inst_net = 0
    mtx_found = False
    tmf_inst_net = 0
    tmf_found = False

    for row in rows[1:]:
        if len(row) < 14:
            continue
        contract_name = row[1].strip()
        identity = row[2].strip()
        oi_net = _parse_number(row[13])
        if oi_net is None:
            continue
        oi_int = int(round(oi_net))

        if contract_name == "臺股期貨" and "外資" in identity:
            tx_foreign_net = oi_int
        elif contract_name == "小型臺指期貨":
            mtx_inst_net += oi_int
            mtx_found = True
        elif contract_name == "微型臺指期貨":
            tmf_inst_net += oi_int
            tmf_found = True

    if tx_foreign_net is None:
        return None

    return {
        "date": iso_date,
        "tx_foreign_net": tx_foreign_net,
        "mtx_inst_net": mtx_inst_net if mtx_found else 0,
        "tmf_inst_net": tmf_inst_net if tmf_found else 0,
    }


def fetch_taifex_total_oi(session: requests.Session) -> dict[str, Any]:
    """從期交所 OpenAPI 抓取最新交易日全市場小台（MTX）與微台（TMF）總未平倉量。"""
    url = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        return {}

    latest_raw_date = str(rows[0].get("Date", "")).strip()
    iso_date = ""
    if len(latest_raw_date) == 8 and latest_raw_date.isdigit():
        iso_date = f"{latest_raw_date[:4]}-{latest_raw_date[4:6]}-{latest_raw_date[6:8]}"

    mtx_oi = 0
    tmf_oi = 0
    tx_oi = 0
    for row in rows:
        if row.get("TradingSession") != "一般":
            continue
        oi_raw = str(row.get("OpenInterest", "")).strip()
        if not oi_raw.isdigit():
            continue
        oi_val = int(oi_raw)
        contract = row.get("Contract")
        if contract == "MTX":
            mtx_oi += oi_val
        elif contract == "TMF":
            tmf_oi += oi_val
        elif contract == "TX":
            tx_oi += oi_val

    return {
        "date": iso_date,
        "mtx_total_oi": mtx_oi,
        "tmf_total_oi": tmf_oi,
        "tx_total_oi": tx_oi,
    }


def _classify_tsmc(candles: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """評估台積電 (2330.TW) 均線與量價位階，映射至選項值 (0/3/7/14)。"""
    closes = [c["close"] for c in candles]
    last = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else last
    close_now = last["close"]
    ma5 = _compute_ma(closes, 5) or close_now
    ma10 = _compute_ma(closes, 10) or close_now
    ma20 = _compute_ma(closes, 20) or close_now
    ma60 = _compute_ma(closes, 60) or close_now
    chg_pct = ((close_now - prev["close"]) / prev["close"]) * 100.0 if prev["close"] else 0.0

    # 判定邏輯
    if close_now < ma20 * 0.985 and close_now < ma60:
        status = "14"
    elif close_now < ma20 and close_now >= ma5 and close_now >= ma10 and chg_pct > 0:
        status = "3"
    elif close_now < ma10 or (close_now < ma20 and close_now >= ma60):
        status = "7" if close_now >= ma20 * 0.98 else "14"
    else:
        status = "0"

    return status, {
        "close": round(close_now, 1),
        "change_pct": round(chg_pct, 2),
        "ma5": round(ma5, 1),
        "ma10": round(ma10, 1),
        "ma20": round(ma20, 1),
        "ma60": round(ma60, 1),
    }


def _classify_finance(candles: list[dict[str, Any]], twii_5d_pct: float) -> tuple[str, dict[str, Any]]:
    """評估金融股指數防線狀態，映射至選項值 (0/5/11)。"""
    closes = [c["close"] for c in candles]
    last = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else last
    ref5 = candles[-6] if len(candles) >= 6 else candles[0]
    close_now = last["close"]
    ma20 = _compute_ma(closes, 20) or close_now
    ma60 = _compute_ma(closes, 60) or close_now
    chg_1d_pct = ((close_now - prev["close"]) / prev["close"]) * 100.0 if prev["close"] else 0.0
    chg_5d_pct = ((close_now - ref5["close"]) / ref5["close"]) * 100.0 if ref5["close"] else 0.0

    if close_now < ma20 * 0.975 and (close_now < ma60 or chg_1d_pct <= -2.0 or chg_5d_pct <= -3.5):
        status = "11"
    elif close_now >= ma20 * 0.995 and chg_5d_pct >= twii_5d_pct + 1.0:
        status = "0"
    elif close_now >= ma20 and chg_5d_pct >= 0:
        status = "0"
    else:
        status = "5"

    return status, {
        "close": round(close_now, 2),
        "change_1d_pct": round(chg_1d_pct, 2),
        "change_5d_pct": round(chg_5d_pct, 2),
        "ma20": round(ma20, 2),
    }


def _classify_kd(kd_info: dict[str, float]) -> str:
    """根據真實 K/D 值與交叉方向，映射至選項值 (0/3/8/12)。"""
    k, d = kd_info["k"], kd_info["d"]
    pk, pd = kd_info["prev_k"], kd_info["prev_d"]
    if k < 28 and k > d and pk <= pd:
        return "3"
    if k < 25:
        return "12" if k <= d else "3"
    if k >= 50 and k >= d:
        return "0"
    return "8"


def _estimate_margin_maintenance_ratio(
    twii_candles: list[dict[str, Any]], margin_change_1d_b: float
) -> float:
    """以大盤近 40 日成本線對比收盤位階並納入斷頭修正，推算整戶融資維持率 (%)。"""
    closes = [c["close"] for c in twii_candles]
    close_now = closes[-1]
    ma40 = _compute_ma(closes, 40) or _compute_ma(closes, len(closes)) or close_now
    # 台灣標準融資自備 4 成、借 6 成，初始維持率 166.7%
    base_ratio = 166.7 * (close_now / ma40) if ma40 > 0 else 160.0
    # 若單日大減超過百億代表追繳強平潮，略微下修反映市場實況
    if margin_change_1d_b <= -150:
        base_ratio -= 4.0
    elif margin_change_1d_b <= -80:
        base_ratio -= 2.0
    return round(max(120.0, min(185.0, base_ratio)), 1)


# 《台股低本益比與成長潛力觀察名單.pdf》11 檔基本面共識基準（2026/11/20 報告）
WATCHLIST_SPECS: list[dict[str, Any]] = [
    {
        "code": "2376",
        "name": "技嘉",
        "sector": "AI 伺服器／板卡",
        "category": "attack",
        "eps2025": 18.20,
        "div2026": 12.00,
        "payoutPct": 65.9,
        "eps2026E": 36.20,
        "eps2027E": 38.63,
        "div2027E": 23.90,
        "low52": 206.5,
        "high52": 402.0,
        "buyZone": [225.0, 250.0],
        "note": "2026 前三季 EPS 達 30.52 元、毛利率近 12%，2027E 本益比壓縮至 7 倍以下，前瞻殖利率逾 9%。",
    },
    {
        "code": "2357",
        "name": "華碩",
        "sector": "品牌電腦／AI",
        "category": "attack",
        "eps2025": 59.99,
        "div2026": 42.00,
        "payoutPct": 70.0,
        "eps2026E": 78.02,
        "eps2027E": 75.68,
        "div2027E": 54.60,
        "low52": 490.0,
        "high52": 1030.0,
        "buyZone": [690.0, 745.0],
        "note": "主機板與 AI PC 領導廠並擴展 AI 伺服器，跌破 800 元關卡後今明兩年本益比落於 10 倍以下。",
    },
    {
        "code": "2881",
        "name": "富邦金",
        "sector": "金融控股",
        "category": "defense",
        "eps2025": 8.36,
        "div2026": 4.25,
        "payoutPct": 50.8,
        "eps2026E": 11.04,
        "eps2027E": 10.23,
        "div2027E": 5.60,
        "low52": 83.5,
        "high52": 164.5,
        "buyZone": [122.0, 132.0],
        "note": "金控獲利王，2026 前 10 月純益 1,264.7 億元（EPS 8.75 元）；屬強勢補跌段，待止跌再分批布局。",
    },
    {
        "code": "3231",
        "name": "緯創",
        "sector": "AI 伺服器代工",
        "category": "attack",
        "eps2025": 9.04,
        "div2026": 5.50,
        "payoutPct": 60.8,
        "eps2026E": 18.97,
        "eps2027E": 29.52,
        "div2027E": 11.50,
        "low52": 122.5,
        "high52": 206.0,
        "buyZone": [135.0, 148.0],
        "note": "2026 Q3 單季營收破 1.14 兆、純益年增 148%，2027E 本益比僅約 5 倍，為低估值高成長代表。",
    },
    {
        "code": "2382",
        "name": "廣達",
        "sector": "AI 伺服器代工",
        "category": "attack",
        "eps2025": 19.45,
        "div2026": 15.60,
        "payoutPct": 80.2,
        "eps2026E": 26.44,
        "eps2027E": 31.62,
        "div2027E": 21.20,
        "low52": 247.5,
        "high52": 438.0,
        "buyZone": [240.0, 258.0],
        "note": "AI 伺服器代工龍頭，Q3 營收年增翻倍；股價逼近 52 週低點 247.5 元支撐，前瞻殖利率逾 8.4%。",
    },
    {
        "code": "2385",
        "name": "群光",
        "sector": "電子零組件",
        "category": "defense",
        "eps2025": 9.05,
        "div2026": 7.20,
        "payoutPct": 79.6,
        "eps2026E": 7.96,
        "eps2027E": 7.11,
        "div2027E": 6.30,
        "low52": 84.0,
        "high52": 149.5,
        "buyZone": [82.0, 86.5],
        "note": "受惠 AI PC 換機與高瓦數電源升級；大盤急殺時單日抗跌，距 52 週低點僅 1%，高配息保護強。",
    },
    {
        "code": "2882",
        "name": "國泰金",
        "sector": "金融控股",
        "category": "defense",
        "eps2025": 7.08,
        "div2026": 3.50,
        "payoutPct": 49.4,
        "eps2026E": 9.02,
        "eps2027E": 8.73,
        "div2027E": 4.50,
        "low52": 63.6,
        "high52": 121.5,
        "buyZone": [88.0, 94.0],
        "note": "2026 前 10 月累計獲利破 1,100 億元（EPS 7.56 元），銀行壽險雙引擎穩健；補跌後待止穩接回。",
    },
    {
        "code": "6669",
        "name": "緯穎",
        "sector": "雲端資料中心",
        "category": "attack",
        "eps2025": 275.06,
        "div2026": 144.39,
        "payoutPct": 52.5,
        "eps2026E": 135.76,
        "eps2027E": 201.37,
        "div2027E": 71.30,
        "low52": 1103.0,
        "high52": 2875.0,
        "buyZone": [1180.0, 1300.0],
        "note": "CSP 與自研 ASIC 伺服器純度極高，殺盤修正後 2027E 本益比降至 6.6 倍，股性活潑宜分批低接。",
    },
    {
        "code": "3034",
        "name": "聯詠",
        "sector": "IC 設計",
        "category": "defense",
        "eps2025": 26.87,
        "div2026": 23.00,
        "payoutPct": 85.6,
        "eps2026E": 32.26,
        "eps2027E": 35.45,
        "div2027E": 27.60,
        "low52": 352.0,
        "high52": 608.0,
        "buyZone": [380.0, 408.0],
        "note": "顯示驅動 IC 龍頭，Q3 營收年增 26%、毛利率守穩 40.89%，配息率達 85.6% 現金流穩定。",
    },
    {
        "code": "2317",
        "name": "鴻海",
        "sector": "AI 伺服器／代工",
        "category": "attack",
        "eps2025": 13.61,
        "div2026": 7.20,
        "payoutPct": 52.9,
        "eps2026E": 19.41,
        "eps2027E": 25.00,
        "div2027E": 10.30,
        "low52": 187.5,
        "high52": 314.0,
        "buyZone": [192.0, 206.0],
        "note": "2026 Q3 營收破 3 兆、純益 775.24 億元創高；掌握 AI 整機組裝高市占，靠近 200 元具下檔支撐。",
    },
    {
        "code": "2330",
        "name": "台積電",
        "sector": "晶圓代工龍頭",
        "category": "attack",
        "eps2025": 66.25,
        "div2026": 22.00,
        "payoutPct": 33.2,
        "eps2026E": 109.21,
        "eps2027E": 145.69,
        "div2027E": 36.00,
        "low52": 1375.0,
        "high52": 2705.0,
        "buyZone": [1880.0, 2010.0],
        "note": "先進製程與封裝絕對龍頭，前三季 EPS 78.75 元；回檔測試 2,000 元大關，2027E PE 13.7 倍 PEG < 1。",
    },
    {
        "code": "5871",
        "name": "中租-KY",
        "sector": "租賃金融／綠能",
        "category": "defense",
        "eps2025": 11.24,
        "div2026": 5.80,
        "payoutPct": 51.6,
        "eps2026E": 11.60,
        "eps2027E": 12.00,
        "div2027E": 6.00,
        "low52": 93.8,
        "high52": 122.1,
        "buyZone": [88.0, 95.0],
        "note": "2026 前三季 EPS 8.73 元（年增 6%）、總資產破兆，惟合併延滯率仍處 5.2% 高檔磨底；本益比壓縮至約 8 倍、貼近 52 週低點 93.8 元，宜耐心小額分批。",
    },
]


def _load_tdcc_history() -> dict[str, dict[str, Any]]:
    """讀取本機集保中心每週歷史快照。"""
    if not TDCC_HISTORY_PATH.exists():
        return {}
    try:
        loaded = json.loads(TDCC_HISTORY_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _attach_tdcc_deltas(
    history: dict[str, dict[str, Any]],
    tdcc_date: str,
    grouped: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """為集保快照計算與前一週快照的增減欄位（回傳淺拷貝避免污染存檔資料）。"""
    result = {code: dict(rec) for code, rec in grouped.items()}
    prev_dates = sorted([d for d in history.keys() if d < tdcc_date])
    if prev_dates:
        prev_snapshot = history[prev_dates[-1]]
        for code, rec in result.items():
            prev_rec = prev_snapshot.get(code)
            if prev_rec:
                rec["big1000Delta1w"] = round(
                    rec["big1000Pct"] - prev_rec.get("big1000Pct", 0.0), 2
                )
                rec["big400Delta1w"] = round(
                    rec["big400Pct"] - prev_rec.get("big400Pct", 0.0), 2
                )
                rec["retailHoldersDelta1w"] = rec["retailHolders"] - prev_rec.get(
                    "retailHolders", 0
                )
                rec["totalHoldersDelta1w"] = rec["totalHolders"] - prev_rec.get(
                    "totalHolders", 0
                )
    return result


def fetch_tdcc_distribution(
    session: requests.Session,
    target_codes: set[str],
    latest_market_date: str | None = None,
    force_refresh: bool = False,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """從集保中心 (TDCC) OpenData 下載最新一週股權分散表，優先使用本週快取並於失敗時回退。"""
    history = _load_tdcc_history()
    date_keys = sorted(k for k in history.keys() if k.isdigit() and len(k) == 8)
    latest_cached_date = date_keys[-1] if date_keys else ""
    cached_snapshot = history.get(latest_cached_date, {}) if latest_cached_date else {}

    if not force_refresh and latest_cached_date and target_codes.issubset(cached_snapshot.keys()):
        try:
            cached_dt = dt.datetime.strptime(latest_cached_date, "%Y%m%d").date()
            ref_dt = dt.date.today()
            if latest_market_date:
                try:
                    ref_dt = dt.datetime.strptime(latest_market_date, "%Y-%m-%d").date()
                except ValueError:
                    pass
            now_dt = dt.datetime.now()
            days_old = (now_dt.date() - cached_dt).days
            is_fresh = (
                cached_dt >= ref_dt
                or days_old < 7
                or (days_old == 7 and now_dt.weekday() == 4 and now_dt.hour < 19)
            )
            if is_fresh:
                _log_progress(
                    f"集保 TDCC 命中本週快取 ({latest_cached_date}，免重複下載 2.4MB)"
                )
                return latest_cached_date, _attach_tdcc_deltas(
                    history, latest_cached_date, cached_snapshot
                )
        except ValueError:
            pass

    url = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"
    _log_progress("正在下載集保中心 TDCC 每週股權分散表 (約 2.4MB，需時 20~35 秒)...")
    try:
        t0 = time.monotonic()
        chunks: list[bytes] = []
        with session.get(url, timeout=(10, 20), stream=True) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=65536):
                if time.monotonic() - t0 > 45.0:
                    resp.close()
                    raise TimeoutError("TDCC 下載超過 45 秒上限")
                if chunk:
                    chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8-sig", errors="replace")

        grouped: dict[str, dict[str, Any]] = {}
        tdcc_date = ""
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if len(row) < 6:
                continue
            code = row[1].strip()
            if code not in target_codes:
                continue
            tdcc_date = row[0].strip()
            lvl = row[2].strip()
            holders = int(_parse_number(row[3]) or 0)
            shares = int(_parse_number(row[4]) or 0)
            pct = float(_parse_number(row[5]) or 0.0)

            rec = grouped.setdefault(
                code,
                {
                    "date": tdcc_date,
                    "big1000Holders": 0,
                    "big1000Pct": 0.0,
                    "big400Holders": 0,
                    "big400Pct": 0.0,
                    "retailHolders": 0,
                    "retailPct": 0.0,
                    "totalHolders": 0,
                    "totalShares": 0,
                },
            )
            if lvl == "15":
                rec["big1000Holders"] = holders
                rec["big1000Pct"] = round(pct, 2)
            if lvl in ("12", "13", "14", "15"):
                rec["big400Holders"] += holders
                rec["big400Pct"] = round(rec["big400Pct"] + pct, 2)
            if lvl in ("1", "2", "3"):
                rec["retailHolders"] += holders
                rec["retailPct"] = round(rec["retailPct"] + pct, 2)
            if lvl == "17":
                rec["totalHolders"] = holders
                rec["totalShares"] = shares

        if not tdcc_date or not grouped:
            raise ValueError("TDCC 回應內容缺少目標個股有效資料")

        history[tdcc_date] = grouped
        sorted_dates = sorted(history.keys())
        if len(sorted_dates) > 52:
            for old_d in sorted_dates[:-52]:
                history.pop(old_d, None)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TDCC_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return tdcc_date, _attach_tdcc_deltas(history, tdcc_date, grouped)
    except Exception as exc:
        if latest_cached_date and cached_snapshot:
            _log_progress(
                f"集保 TDCC 下載失敗（{exc}），自動回退使用本機快取 ({latest_cached_date})"
            )
            return latest_cached_date, _attach_tdcc_deltas(
                history, latest_cached_date, cached_snapshot
            )
        _log_progress(f"集保 TDCC 下載失敗且無本機快取（{exc}）")
        return "", {}


def fetch_rsi20_holders_history(
    session: requests.Session, code: str
) -> dict[str, Any] | None:
    """從 rsi.pttstock.tw 抓取個股近 20 週 400 張以上大戶持股比率歷史序列。"""
    url = f"https://rsi.pttstock.tw/stock/{code}/"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    m = re.search(
        r"xAxis:\{type:'category',data:\[(.*?)\].*?series:\[\{type:'line',data:\[(.*?)\]",
        resp.text,
        re.S,
    )
    if not m:
        return None
    dates = [d.strip().strip("'\"") for d in m.group(1).split(",") if d.strip()]
    values: list[float] = []
    for raw_v in m.group(2).split(","):
        num = _parse_number(raw_v)
        if num is not None:
            values.append(round(num, 2))
    if not dates or len(values) < 2:
        return None
    return {"dates": dates, "values": values}


def fetch_fubon_broker_summary(
    session: requests.Session, code: str
) -> dict[str, Any] | None:
    """從富邦嘉實券商分點頁抓取前 15 大主力買賣超與最大買超／賣超分點。"""
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{code}.djhtm"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    html = resp.content.decode("cp950", errors="replace")

    m_buy = re.search(r"合計買超張數.*?<td[^>]*>\s*([\d,]+)\s*</td>", html, re.S | re.I)
    m_sell = re.search(r"合計賣超張數.*?<td[^>]*>\s*([\d,]+)\s*</td>", html, re.S | re.I)
    if not m_buy or not m_sell:
        return None

    main_buy = int(_parse_number(m_buy.group(1)) or 0)
    main_sell = int(_parse_number(m_sell.group(1)) or 0)
    main_net = main_buy - main_sell

    # 解析第一大買超與賣超券商分點名稱及張數
    top_buyer = ""
    top_buyer_lots = 0
    top_seller = ""
    top_seller_lots = 0

    idx_header = html.find("買超券商")
    if idx_header != -1:
        sub = html[idx_header:]
        m_row = re.search(r"<TR>\s*(.*?)\s*</tr>", sub, re.S | re.I)
        if m_row:
            row_html = m_row.group(1)
            brokers = re.findall(r'<TD class="t4t1"[^>]*><a[^>]*>(.*?)</a></TD>', row_html, re.S | re.I)
            nums = re.findall(r'<TD class="t3n1"[^>]*>(.*?)</TD>', row_html, re.S | re.I)
            if len(brokers) >= 1 and len(nums) >= 3:
                top_buyer = brokers[0].strip()
                top_buyer_lots = int(_parse_number(nums[2]) or 0)
            if len(brokers) >= 2 and len(nums) >= 7:
                top_seller = brokers[1].strip()
                top_seller_lots = int(_parse_number(nums[6]) or 0)

    return {
        "mainBuyLots": main_buy,
        "mainSellLots": main_sell,
        "mainNetLots": main_net,
        "topBuyer": top_buyer,
        "topBuyerLots": top_buyer_lots,
        "topSeller": top_seller,
        "topSellerLots": top_seller_lots,
    }


def _evaluate_watchlist_stock(
    spec: dict[str, Any],
    candles: list[dict[str, Any]],
    tdcc_item: dict[str, Any] | None = None,
    rsi20_hist: dict[str, Any] | None = None,
    broker_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """結合個股最新股價、均線、KD 與報告估值區間，評估是否達可買進價位。"""
    closes = [c["close"] for c in candles]
    last = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else last
    close_now = round(last["close"], 2)
    chg_pct = round(
        ((close_now - prev["close"]) / prev["close"]) * 100.0 if prev["close"] else 0.0,
        2,
    )
    ma5 = round(_compute_ma(closes, 5) or close_now, 2)
    ma10 = round(_compute_ma(closes, 10) or close_now, 2)
    ma20 = round(_compute_ma(closes, 20) or close_now, 2)
    kd = _compute_kd(candles, period=9)

    pe_2026e = round(close_now / spec["eps2026E"], 2) if spec["eps2026E"] else 0.0
    pe_2027e = round(close_now / spec["eps2027E"], 2) if spec["eps2027E"] else 0.0
    yield_2027e = round((spec["div2027E"] / close_now) * 100.0, 2) if close_now else 0.0

    low52 = spec["low52"]
    high52 = spec["high52"]
    dist_52_low_pct = round(((close_now - low52) / low52) * 100.0, 1) if low52 else 0.0
    zone_low, zone_high = spec["buyZone"]
    right_confirm_price = round(max(ma5, ma10), 1)

    # 評估是否達可買進價位
    # 1. 右側轉強買進確認：站上 5 日與 10 日均線且日 KD 偏多或金叉向上
    if close_now >= ma5 and close_now >= ma10 and (kd["k"] > kd["d"] or kd["k"] >= 50) and pe_2027e <= 16.5:
        entry_status = "buy_right"
        entry_label = "右側買進確認"
        entry_reason = f"已站回 5/10 日均線（{right_confirm_price} 元），且 2027E 本益比 {pe_2027e} 倍具性價比。"
    # 2. 補跌破線觀望（例如單日暴跌逾 5.8% 且距離 52 週低點仍遠 > 20%）
    elif chg_pct <= -5.8 and dist_52_low_pct >= 20.0 and close_now > zone_high:
        entry_status = "wait"
        entry_label = "補跌修正・暫觀望"
        entry_reason = f"單日補跌 {chg_pct:.2f}% 且距 52 週低點仍遠，勿第一天急接，等止跌或靠攏 {zone_low}~{zone_high} 元。"
    # 3. 落入左側價值買進區間或貼近 52 週低點 7.5% 以內
    elif close_now <= zone_high or dist_52_low_pct <= 7.5:
        entry_status = "buy_left"
        entry_label = "達左側首波試單價"
        if dist_52_low_pct <= 3.0:
            entry_reason = f"逼近 52 週低點 {low52} 元（僅差 {dist_52_low_pct:.1f}%），2027E PE {pe_2027e} 倍、殖利率 {yield_2027e:.1f}%，可左側試單。"
        else:
            entry_reason = f"進入合理買進區間（{zone_low}~{zone_high} 元），前瞻 PE {pe_2027e} 倍，適合小額分批建立第 1 批底倉。"
    # 4. 距買點在 6% 以內，列為接近買點
    elif ((close_now - zone_high) / zone_high) * 100.0 <= 6.5:
        entry_status = "watch_near"
        gap_pct = round(((close_now - zone_high) / zone_high) * 100.0, 1)
        entry_label = "估值偏低・接近買點"
        entry_reason = f"距左側試單上緣 {zone_high} 元僅差 {gap_pct}%（或等站回 5/10 日線 {right_confirm_price} 元右側轉強再接）。"
    else:
        entry_status = "wait"
        entry_label = "價位未到・續等待"
        entry_reason = f"仍高於買進區間 {zone_high} 元且未站穩短均線，建議耐心等待拉回買點或轉強訊號。"

    # 整合籌碼三件套 (集保 TDCC + RSI20 近4週大戶趨勢 + 富邦嘉實前15大主力分點)
    tdcc = tdcc_item or {}
    big1000_pct = tdcc.get("big1000Pct")
    big400_pct = tdcc.get("big400Pct")
    big400_delta_1w = tdcc.get("big400Delta1w")
    big400_delta_4w: float | None = None

    if rsi20_hist and rsi20_hist.get("values"):
        vals = rsi20_hist["values"]
        r_dates = rsi20_hist.get("dates") or []
        curr_400 = big400_pct if big400_pct is not None else vals[-1]
        tdcc_raw_d = str(tdcc.get("date", ""))
        tdcc_mmdd = f"{tdcc_raw_d[4:6]}/{tdcc_raw_d[6:8]}" if len(tdcc_raw_d) == 8 else ""
        same_latest = bool(tdcc_mmdd and r_dates and r_dates[-1] == tdcc_mmdd)
        idx_1w = -2 if same_latest else -1
        idx_4w = -5 if same_latest else -4
        if len(vals) >= abs(idx_1w):
            big400_delta_1w = round(curr_400 - vals[idx_1w], 2)
        if len(vals) >= abs(idx_4w):
            big400_delta_4w = round(curr_400 - vals[idx_4w], 2)
        elif vals:
            big400_delta_4w = round(curr_400 - vals[0], 2)

    main_net_lots = broker_info.get("mainNetLots") if broker_info else None
    chip_signal = "neutral"
    chip_label = "籌碼拉鋸換手"
    d4 = big400_delta_4w if big400_delta_4w is not None else 0.0
    d1 = big400_delta_1w if big400_delta_1w is not None else 0.0
    mn = main_net_lots if main_net_lots is not None else 0

    if (d4 <= -0.30 and mn < 0) or (d4 <= -0.80 and mn <= 0) or (d1 <= -0.20 and mn <= -400):
        chip_signal = "dumping"
        chip_label = "大戶倒貨／主力偏空"
    elif (d4 >= 0.25 and mn > 0) or (d1 >= 0.15 and mn >= 400):
        chip_signal = "accumulating"
        chip_label = "大戶吸籌／主力偏多"
    elif d4 <= -0.25 and mn >= 400:
        chip_signal = "neutral"
        chip_label = "4週大戶減・主力轉買"
    elif d4 >= 0.20 and mn <= -400:
        chip_signal = "neutral"
        chip_label = "4週增・短線主力調節"

    chips_payload = {
        "tdccDate": tdcc.get("date", ""),
        "big1000Holders": tdcc.get("big1000Holders"),
        "big1000Pct": big1000_pct,
        "big1000Delta1w": tdcc.get("big1000Delta1w"),
        "big400Holders": tdcc.get("big400Holders"),
        "big400Pct": big400_pct,
        "big400Delta1w": big400_delta_1w,
        "big400Delta4w": big400_delta_4w,
        "retailHolders": tdcc.get("retailHolders"),
        "retailHoldersDelta1w": tdcc.get("retailHoldersDelta1w"),
        "retailPct": tdcc.get("retailPct"),
        "totalHolders": tdcc.get("totalHolders"),
        "totalHoldersDelta1w": tdcc.get("totalHoldersDelta1w"),
        "broker": broker_info,
        "chipSignal": chip_signal,
        "chipLabel": chip_label,
    }

    return {
        "code": spec["code"],
        "name": spec["name"],
        "sector": spec["sector"],
        "category": spec["category"],
        "close": close_now,
        "changePct": chg_pct,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "kd": kd,
        "pe2026E": pe_2026e,
        "pe2027E": pe_2027e,
        "div2027E": spec["div2027E"],
        "yield2027E": yield_2027e,
        "payoutPct": spec["payoutPct"],
        "low52": low52,
        "high52": high52,
        "dist52LowPct": dist_52_low_pct,
        "buyZoneLow": zone_low,
        "buyZoneHigh": zone_high,
        "rightConfirmPrice": right_confirm_price,
        "entryStatus": entry_status,
        "entryLabel": entry_label,
        "entryReason": entry_reason,
        "note": spec["note"],
        "chips": chips_payload,
    }


def fetch_watchlist_data(
    session: requests.Session | None = None,
    tsmc_candles: list[dict[str, Any]] | None = None,
    latest_market_date: str | None = None,
    force_tdcc: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """抓取觀察名單 12 檔日 K、集保股權分散 (TDCC)、RSI20 大戶週趨勢與富邦主力分點。"""
    del session  # 各背景工作執行緒各自建立獨立 requests.Session，避免跨執行緒共用
    target_codes = {s["code"] for s in WATCHLIST_SPECS}

    def _fetch_tdcc_task() -> tuple[str, dict[str, dict[str, Any]]]:
        try:
            with requests.Session() as ts:
                ts.headers.update(DEFAULT_HEADERS)
                return fetch_tdcc_distribution(
                    ts,
                    target_codes,
                    latest_market_date=latest_market_date,
                    force_refresh=force_tdcc,
                )
        except Exception:
            return "", {}

    def _fetch_stock_raw(
        spec: dict[str, Any],
    ) -> (
        tuple[
            dict[str, Any],
            list[dict[str, Any]],
            dict[str, Any] | None,
            dict[str, Any] | None,
        ]
        | None
    ):
        code = spec["code"]
        candles: list[dict[str, Any]] = []
        rsi20_hist: dict[str, Any] | None = None
        broker_info: dict[str, Any] | None = None

        with requests.Session() as s:
            s.headers.update(DEFAULT_HEADERS)
            if code == "2330" and tsmc_candles and len(tsmc_candles) >= 10:
                candles = tsmc_candles
            else:
                try:
                    candles = fetch_yahoo_candles(s, f"{code}.TW", "6mo")
                except Exception:
                    candles = []

            if len(candles) < 5:
                return None

            try:
                rsi20_hist = fetch_rsi20_holders_history(s, code)
            except Exception:
                rsi20_hist = None

            try:
                broker_info = fetch_fubon_broker_summary(s, code)
            except Exception:
                broker_info = None

        return spec, candles, rsi20_hist, broker_info

    with ThreadPoolExecutor(max_workers=5) as executor:
        tdcc_future = executor.submit(_fetch_tdcc_task)
        stock_futures = [executor.submit(_fetch_stock_raw, spec) for spec in WATCHLIST_SPECS]
        tdcc_date, tdcc_map = tdcc_future.result()
        raw_stocks = [fut.result() for fut in stock_futures]

    items: list[dict[str, Any]] = []
    for raw in raw_stocks:
        if raw is None:
            continue
        spec, candles, rsi20_hist, broker_info = raw
        items.append(
            _evaluate_watchlist_stock(
                spec,
                candles,
                tdcc_item=tdcc_map.get(spec["code"]),
                rsi20_hist=rsi20_hist,
                broker_info=broker_info,
            )
        )
    return tdcc_date, items


def fetch_all_market_data(force_tdcc: bool = False) -> dict[str, Any]:
    """執行完整台股官方數據抓取與儀表板指標計算，並儲存至 data/latest_market_data.json。"""
    t_start = time.monotonic()
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    errors: list[str] = []

    # 1. Yahoo Finance 日 K（大盤、台積電、金融指數）
    _log_progress("1/5 抓取 Yahoo Finance 日 K（加權指數、台積電、金融指數）...")
    twii_candles = fetch_yahoo_candles(session, "^TWII", "6mo")
    if len(twii_candles) < 10:
        raise RuntimeError("無法取得足夠的大盤日 K 線數據 (^TWII)")

    tsmc_candles = fetch_yahoo_candles(session, "2330.TW", "6mo")
    try:
        fin_candles = fetch_yahoo_candles(session, "^TFNI", "6mo")
        if len(fin_candles) < 10:
            fin_candles = fetch_yahoo_candles(session, "0055.TW", "6mo")
    except Exception:
        fin_candles = fetch_yahoo_candles(session, "0055.TW", "6mo")

    latest_twii = twii_candles[-1]
    prev_twii = twii_candles[-2]
    ref5_twii = twii_candles[-6] if len(twii_candles) >= 6 else twii_candles[0]
    twii_close = round(latest_twii["close"], 2)
    twii_change = round(latest_twii["close"] - prev_twii["close"], 2)
    twii_change_pct = round((twii_change / prev_twii["close"]) * 100.0, 2)
    twii_5d_pct = round(
        ((latest_twii["close"] - ref5_twii["close"]) / ref5_twii["close"]) * 100.0, 2
    )

    kd_info = _compute_kd(twii_candles, period=9)
    kd_status = _classify_kd(kd_info)

    tsmc_status, tsmc_info = (
        _classify_tsmc(tsmc_candles) if len(tsmc_candles) >= 5 else ("7", {})
    )
    finance_status, finance_info = (
        _classify_finance(fin_candles, twii_5d_pct)
        if len(fin_candles) >= 5
        else ("5", {})
    )

    # 候選最近交易日清單（由新到舊）
    recent_dates = [c["date"] for c in reversed(twii_candles[-12:])]
    latest_date = recent_dates[0]

    # 2. TWSE FMTQIK 大盤成交金額（計算量能是否爆量或窒息量）
    _log_progress("2/5 抓取證交所大盤成交量、近 5 日三大法人與融資餘額...")
    fmt_rows: list[dict[str, Any]] = []
    try:
        curr_month_str = latest_date.replace("-", "")
        fmt_rows = fetch_twse_fmtqik(session, curr_month_str)
        if len(fmt_rows) < 20:
            dt_prev_month = dt.datetime.strptime(latest_date, "%Y-%m-%d").replace(day=1) - dt.timedelta(days=1)
            prev_month_rows = fetch_twse_fmtqik(session, dt_prev_month.strftime("%Y%m%d"))
            fmt_rows = prev_month_rows + fmt_rows
    except Exception as exc:
        errors.append(f"TWSE 成交量抓取異常: {exc}")

    volume_billion = 0.0
    avg_vol_20d = 0.0
    if fmt_rows:
        fmt_rows.sort(key=lambda x: x["date"])
        volume_billion = fmt_rows[-1]["amount_billion"]
        vol_slice = [r["amount_billion"] for r in fmt_rows[-20:]]
        avg_vol_20d = round(sum(vol_slice) / len(vol_slice), 2) if vol_slice else volume_billion

    # 判斷恐慌爆量急殺 vs 量縮窒息量
    has_recent_crash = any(
        ((twii_candles[i]["close"] - twii_candles[i - 1]["close"]) / twii_candles[i - 1]["close"]) * 100.0 <= -2.5
        for i in range(len(twii_candles) - 3, len(twii_candles))
        if i >= 1
    )
    vol_ratio = round(volume_billion / avg_vol_20d, 2) if avg_vol_20d > 0 else 1.0
    chk_panic_vol = bool(
        (twii_change_pct <= -2.2 and vol_ratio >= 1.15)
        or twii_change_pct <= -3.2
        or (has_recent_crash and vol_ratio >= 1.1)
    )
    # 窒息量且近 3 日不破前低
    min_prev_low = min(c["low"] for c in twii_candles[-10:-1]) if len(twii_candles) > 10 else 0
    chk_dry_vol = bool(
        avg_vol_20d > 0
        and vol_ratio <= 0.72
        and latest_twii["low"] >= min_prev_low
        and twii_change_pct > -1.0
    )

    # 3. TWSE BFI82U 最近 5 個有效交易日外資與投信現貨買賣超
    bfi_records: list[dict[str, float]] = []
    for d in recent_dates:
        if len(bfi_records) >= 5:
            break
        try:
            rec = fetch_twse_bfi82u_day(session, d)
            if rec:
                bfi_records.append(rec)
        except Exception:
            continue

    spot_net_5d = int(round(sum(r["foreign_billion"] for r in bfi_records))) if bfi_records else 0
    spot_net_1d = bfi_records[0]["foreign_billion"] if bfi_records else 0.0
    trust_net_5d = round(sum(r["trust_billion"] for r in bfi_records), 1) if bfi_records else 0.0
    data_date = bfi_records[0]["date"] if bfi_records else latest_date

    # 4. TWSE MI_MARGN 融資餘額變化
    margin_records: list[dict[str, Any]] = []
    for d in recent_dates:
        if len(margin_records) >= 5:
            break
        try:
            m_rec = fetch_twse_margin_day(session, d)
            if m_rec:
                margin_records.append(m_rec)
        except Exception:
            continue

    margin_balance_b = margin_records[0]["balance_billion"] if margin_records else 0.0
    margin_change_1d_b = margin_records[0]["change_1d_billion"] if margin_records else 0.0
    margin_change_5d_b = (
        round(sum(r["change_1d_billion"] for r in margin_records), 2)
        if margin_records
        else 0.0
    )
    margin_maint_ratio = _estimate_margin_maintenance_ratio(twii_candles, margin_change_1d_b)

    if margin_change_1d_b <= -80.0 or margin_change_5d_b <= -150.0:
        margin_status = "4"
    elif twii_5d_pct <= -1.0 and margin_change_5d_b >= 20.0:
        margin_status = "6"
    else:
        margin_status = "1"

    # 5. TAIFEX 台指期外資淨口數 + 散戶小台/微台多空比
    _log_progress("3/5 抓取期交所三大法人期貨未平倉與小微台散戶多空比...")
    taifex_inst: dict[str, Any] | None = None
    for d in recent_dates[:4]:
        try:
            taifex_inst = fetch_taifex_institutional_day(session, d)
            if taifex_inst:
                break
        except Exception:
            continue

    taifex_oi: dict[str, Any] = {}
    try:
        taifex_oi = fetch_taifex_total_oi(session)
    except Exception as exc:
        errors.append(f"期交所全市場 OI 抓取異常: {exc}")

    futures_net = taifex_inst["tx_foreign_net"] if taifex_inst else -35000
    inst_mtx_net = taifex_inst["mtx_inst_net"] if taifex_inst else 0
    inst_tmf_net = taifex_inst["tmf_inst_net"] if taifex_inst else 0
    mtx_total_oi = taifex_oi.get("mtx_total_oi") or 0
    tmf_total_oi = taifex_oi.get("tmf_total_oi") or 0

    retail_mtx_only_ratio = 0.0
    retail_combined_ratio = 0.0
    if mtx_total_oi > 0:
        retail_mtx_only_ratio = round((-inst_mtx_net / mtx_total_oi) * 100.0, 1)
    if (mtx_total_oi + tmf_total_oi / 5.0) > 0:
        combined_retail_net = (-inst_mtx_net) + (-inst_tmf_net / 5.0)
        combined_total_oi = mtx_total_oi + (tmf_total_oi / 5.0)
        retail_combined_ratio = round((combined_retail_net / combined_total_oi) * 100.0, 1)
    else:
        retail_combined_ratio = retail_mtx_only_ratio

    # 6. 抓取觀察名單 12 檔最新股價、估值、集保大戶與富邦主力買賣超
    _log_progress("4/5 抓取 12 檔觀察名單、集保大戶與主力分點...")
    tdcc_date, watchlist_items = fetch_watchlist_data(
        session=session,
        tsmc_candles=tsmc_candles,
        latest_market_date=data_date,
        force_tdcc=force_tdcc,
    )
    buy_ready_count = sum(1 for x in watchlist_items if x["entryStatus"] in ("buy_left", "buy_right"))
    near_count = sum(1 for x in watchlist_items if x["entryStatus"] == "watch_near")
    wait_count = sum(1 for x in watchlist_items if x["entryStatus"] == "wait")

    # 統計觀察名單 12 檔真實集保 4 週大戶減碼／增碼狀況
    dumping_4w_count = sum(
        1 for x in watchlist_items
        if (x.get("chips", {}).get("big400Delta4w") or 0.0) <= -0.15
    )
    acc_4w_count = sum(
        1 for x in watchlist_items
        if (x.get("chips", {}).get("big400Delta4w") or 0.0) >= 0.15
    )
    dumping_1w_count = sum(
        1 for x in watchlist_items
        if (x.get("chips", {}).get("big400Delta1w") or 0.0) <= -0.05
    )

    # 7. 大戶持股趨勢客觀推估 (結合真實集保大戶 4 週統計 + 外資5日買賣超與台積電結構)
    if (dumping_4w_count >= 7 and spot_net_5d < -300) or (spot_net_5d <= -1000 and tsmc_status == "14"):
        whale_trend = "6"
    elif acc_4w_count >= 7 and spot_net_5d >= 350 and tsmc_status in ("0", "3"):
        whale_trend = "0"
    elif spot_net_5d >= 200 and tsmc_status in ("0", "3"):
        whale_trend = "2"
    elif dumping_4w_count >= 6 or spot_net_5d < -300 or tsmc_status == "7":
        whale_trend = "4"
    else:
        whale_trend = "2"

    # 官股/護盤條件客觀輔助：若投信近 5 日逆勢大買逾 150 億且大盤重挫，視為有內資穩盤訊號（或由使用者勾選）
    chk_gov_support = bool(trust_net_5d >= 150 and twii_5d_pct <= -3.0)

    # 8. 組合人類可讀的專業摘要
    date_label = f"{data_date.replace('-', '/')} 收盤"
    summary_parts = [
        f"加權指數收 {twii_close:,.0f} 點（{twii_change:+.0f} 點 / {twii_change_pct:+.2f}%），"
        f"成交金額 {volume_billion:,.0f} 億元（20日均量比 {vol_ratio:.2f}x），"
        f"外資台指期淨部位 {futures_net:+,} 口、近 5 日現貨 {spot_net_5d:+,} 億元，"
        f"散戶小台＋微台綜合多空比 {retail_combined_ratio:+.1f}%（純小台 {retail_mtx_only_ratio:+.1f}%），"
        f"台積電收 {tsmc_info.get('close', '--')} 元（月線 {tsmc_info.get('ma20', '--')}），"
        f"大盤單日融資 {margin_change_1d_b:+.1f} 億元（維持率推算 {margin_maint_ratio:.1f}%），"
        f"日線 K={kd_info['k']:.1f} / D={kd_info['d']:.1f}。"
    ]

    result: dict[str, Any] = {
        "fetchedAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateLabel": date_label,
        "dataDate": data_date,
        "summary": "".join(summary_parts),
        "futuresNet": max(-120000, min(40000, int(futures_net))),
        "futuresNetRaw": int(futures_net),
        "spotNet5d": max(-4500, min(2500, int(spot_net_5d))),
        "spotNet5dRaw": int(spot_net_5d),
        "spotNet1d": spot_net_1d,
        "trustNet5d": trust_net_5d,
        "retailMtxRatio": max(-40.0, min(60.0, retail_combined_ratio)),
        "retailMtxOnlyRatio": retail_mtx_only_ratio,
        "marginMaintenanceRatio": margin_maint_ratio,
        "marginBalanceBillion": margin_balance_b,
        "marginChange1dBillion": margin_change_1d_b,
        "marginChange5dBillion": margin_change_5d_b,
        "tsmcStatus": tsmc_status,
        "financeStatus": finance_status,
        "marginStatus": margin_status,
        "whaleTrend": whale_trend,
        "kdStatus": kd_status,
        "chkPanicVol": chk_panic_vol,
        "chkGovSupport": chk_gov_support,
        "chkDryVol": chk_dry_vol,
        "metrics": {
            "twiiClose": twii_close,
            "twiiChange": twii_change,
            "twiiChangePct": twii_change_pct,
            "twii5dPct": twii_5d_pct,
            "volumeBillion": volume_billion,
            "avgVolume20dBillion": avg_vol_20d,
            "volumeRatio": vol_ratio,
            "kd": kd_info,
            "tsmc": tsmc_info,
            "finance": finance_info,
            "taifex": {
                "txForeignNet": futures_net,
                "mtxInstNet": inst_mtx_net,
                "mtxTotalOi": mtx_total_oi,
                "tmfInstNet": inst_tmf_net,
                "tmfTotalOi": tmf_total_oi,
                "retailMtxOnlyRatio": retail_mtx_only_ratio,
                "retailCombinedRatio": retail_combined_ratio,
            },
            "whaleStats": {
                "tdccDate": tdcc_date,
                "totalTracked": len(watchlist_items),
                "dumping4wCount": dumping_4w_count,
                "acc4wCount": acc_4w_count,
                "dumping1wCount": dumping_1w_count,
            },
        },
        "watchlist": watchlist_items,
        "watchlistSummary": {
            "total": len(watchlist_items),
            "buyReady": buy_ready_count,
            "watchNear": near_count,
            "wait": wait_count,
        },
        "sources": [
            {"title": "台灣證券交易所 TWSE (三大法人 / 融資融券 / 成交統計)", "uri": "https://www.twse.com.tw/zh/trading/foreign/bfi82u.html"},
            {"title": "台灣期交所 TAIFEX (三大法人期貨未平倉)", "uri": "https://www.taifex.com.tw/cht/3/futContractsDate"},
            {"title": "集保中心 TDCC 股權分散表", "uri": "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"},
            {"title": "RSI20 籌碼趨勢 & 富邦嘉實主力分點", "uri": "https://rsi.pttstock.tw/"},
        ],
        "errors": errors,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_DATA_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _log_progress(f"5/5 同步完成！基準日 {data_date}（耗時 {time.monotonic() - t_start:.1f} 秒）")
    return result


def load_latest_market_data() -> dict[str, Any] | None:
    """讀取本機快取的最新市場數據。"""
    if not LATEST_DATA_PATH.exists():
        return None
    try:
        return json.loads(LATEST_DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


if __name__ == "__main__":
    output = fetch_all_market_data()
    print(json.dumps(output, ensure_ascii=False, indent=2))
