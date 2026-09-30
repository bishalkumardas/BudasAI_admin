from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
import hmac
import logging
from numbers import Real
import secrets as token_secrets
import traceback
from typing import Any
import pandas as pd
import requests
import streamlit as st
from streamlit_cookies_controller import CookieController
import yfinance as yf
from supabase import Client, create_client


logger = logging.getLogger(__name__)

st.set_page_config("BudasAI Admin", "🛡️", layout="wide")
def secret(n):
    v=st.secrets.get(n, "")
    if not v: st.error(f"Missing `{n}` in .streamlit/secrets.toml"); st.stop()
    return str(v)

@st.cache_resource
def db() -> Client:
    return create_client(
        secret("SUPABASE_URL"),
        secret("SUPABASE_SERVICE_ROLE_KEY")
    )

def safe_error_details(error_text, api_key):
    """Remove the API key from errors before they reach logs or the UI."""
    return str(error_text).replace(api_key, "[REDACTED]")

def fetch_daily_news(api_key):
    categories = ("general", "forex", "crypto", "merger")
    records = []

    for category in categories:
        for attempt in range(3):
            try:
                response = requests.get(
                    "https://api.finnhub.io/api/v1/news",
                    params={"category": category, "token": api_key},
                    headers={"Accept": "application/json", "User-Agent": "BudasAI Admin"},
                    timeout=(5, 20),
                )
                response.raise_for_status()
                payload = response.json()
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt == 2:
                    raise
        else:
            raise RuntimeError(f"Finnhub {category} news request failed after retries.")

        if not isinstance(payload, list):
            raise ValueError(f"Finnhub returned an unexpected {category} news response; expected a list.")

        for article in payload:
            if not isinstance(article, dict):
                continue
            headline = article.get("headline")
            article_url = article.get("url")
            if not isinstance(headline, str) or not headline.strip():
                continue
            if not isinstance(article_url, str) or not article_url.strip():
                continue

            published_at = None
            timestamp = article.get("datetime")
            if timestamp is not None:
                try:
                    published_at = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError, OSError):
                    pass

            record = {
                "category": article.get("category") or category,
                "published_at": published_at,
                "headline": headline.strip(),
                "image_url": article.get("image"),
                "related": article.get("related"),
                "source": article.get("source"),
                "summary": article.get("summary"),
                "article_url": article_url.strip(),
            }
            if article.get("id") is not None:
                record["finnhub_id"] = article["id"]
            records.append(record)

    return records

def existing_daily_news_ids():
    page_size = 1000
    offset = 0
    existing_ids = set()

    while True:
        page = (
            db()
            .table("daily_news")
            .select("finnhub_id")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        existing_ids.update(
            str(row["finnhub_id"]).strip()
            for row in page
            if row.get("finnhub_id") is not None
            and str(row["finnhub_id"]).strip()
        )
        if len(page) < page_size:
            break
        offset += page_size

    return existing_ids

def daily_news_delete_options():
    page_size = 1000
    offset = 0
    options = []

    while True:
        page = (
            db()
            .table("daily_news")
            .select("id,source")
            .order("id")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        options.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    return options

def prepare_daily_news(records, existing_ids):
    seen_ids = {str(article_id).strip() for article_id in existing_ids}
    unique_records = []
    excluded_source_count = 0
    duplicate_count = 0
    missing_id_count = 0

    for article in records:
        source = "".join(str(article.get("source") or "").casefold().split())
        if "reuters" in source or "globalnewswire" in source:
            excluded_source_count += 1
            continue

        article_id = article.get("finnhub_id")
        if article_id is None or not str(article_id).strip():
            missing_id_count += 1
            continue

        article_id = str(article_id).strip()
        if article_id in seen_ids:
            duplicate_count += 1
            continue

        seen_ids.add(article_id)
        unique_records.append(article)

    return unique_records, excluded_source_count, duplicate_count, missing_id_count

def show_refresh_error(error_text, api_key):
    details = safe_error_details(error_text, api_key)
    logger.error("Daily News refresh failed:\n%s", details)
    st.error("Daily News refresh failed.")
    with st.expander("Show full error"):
        st.code(details, language="text")
    
def rows(table, limit=500, order=None):
    try:
        q=db().table(table).select("*").limit(limit)
        return pd.DataFrame((q.order(order,desc=True) if order else q).execute().data)
    except Exception as e: st.error(f"Supabase request failed: {e}"); return pd.DataFrame()
def col(df, options): return next((x for x in options if x in df.columns), None)
MARKET_NAME_COLUMN="name"
MARKET_SYMBOL_COLUMN="symbol"
MARKET_PRICE_COLUMNS=["latest_price","current_price","price","close"]
MARKET_SYSTEM_COLUMNS={"id","market_id","created_at","updated_at"}
def iso(v):
    t=pd.Timestamp(v); return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).isoformat()

def finance(mid, symbol, start, end):
    f = yf.download(
        symbol,
        start=start,
        end=pd.Timestamp(end) + pd.Timedelta(days=1),
        auto_adjust=False,
        progress=False
    )

    if isinstance(f.columns, pd.MultiIndex):
        f.columns = f.columns.get_level_values(0)

    if f.empty:
        return 0

    data = []

    for i, x in f.iterrows():
        close = x["Close"]

        if pd.isna(close):
            continue

        data.append({
            "market_id": mid,
            "timestamp": iso(i),
            "value": float(close),
            "volume": int(x["Volume"]) if pd.notna(x["Volume"]) else 0
        })

    if not data:
        return 0

    db().table("market_history").upsert(
        data,
        on_conflict="market_id,timestamp"
    ).execute()

    return len(data)

def update_market_snapshot(id_column, mid, symbol, selected_date):
    selected = pd.Timestamp(selected_date)

    # Fetch enough history to calculate previous close and 52-week range
    start = selected - pd.Timedelta(days=400)
    end = selected + pd.Timedelta(days=1)

    f = yf.download(
        symbol,
        start=start,
        end=end,
        auto_adjust=False,
        progress=False
    )

    if isinstance(f.columns, pd.MultiIndex):
        f.columns = f.columns.get_level_values(0)

    if f.empty:
        return False, "No Yahoo Finance data"

    f = f.dropna(subset=["Close"])

    # Normalize date index
    f.index = pd.to_datetime(f.index).tz_localize(None)

    selected_rows = f[f.index.normalize() == selected.normalize()]

    # Selected date must actually be a trading day
    if selected_rows.empty:
        return False, "No trading data for selected date"

    row = selected_rows.iloc[-1]

    current_value = float(row["Close"])
    open_value = float(row["Open"])
    high_value = float(row["High"])
    low_value = float(row["Low"])

    # Previous available trading day
    previous_rows = f[f.index < selected.normalize()]

    if previous_rows.empty:
        previous_close = None
        change_value = None
        change_percent = None
    else:
        previous_close = float(previous_rows.iloc[-1]["Close"])
        change_value = current_value - previous_close

        if previous_close != 0:
            change_percent = (change_value / previous_close) * 100
        else:
            change_percent = None

    # 52-week range AS OF selected date
    week_start = selected - pd.Timedelta(days=365)

    year_data = f[
        (f.index >= week_start) &
        (f.index <= selected)
    ]

    week_52_high = (
        float(year_data["High"].max())
        if not year_data.empty else None
    )

    week_52_low = (
        float(year_data["Low"].min())
        if not year_data.empty else None
    )

    # Yahoo Finance currency
    currency = None

    try:
        ticker = yf.Ticker(symbol)
        fast_info = ticker.fast_info
        currency = fast_info.get("currency")
    except Exception:
        pass

    payload = {
        "current_value": current_value,
        "change_value": change_value,
        "change_percent": change_percent,
        "open_value": open_value,
        "high_value": high_value,
        "low_value": low_value,
        "previous_close": previous_close,
        "week_52_high": week_52_high,
        "week_52_low": week_52_low,
        "currency": currency,
    }

    db().table("markets").update(payload).eq(
        id_column,
        mid
    ).execute()

    return True, payload


def latest_price(symbol):
    history=yf.Ticker(symbol).history(period="5d",auto_adjust=False)
    close=history.get("Close")
    if close is None or close.dropna().empty: raise ValueError(f"Yahoo Finance returned no recent closing price for {symbol}.")
    return float(close.dropna().iloc[-1])

def bulk_fetch_daily_prices(markets_df, idc, tc, selected_date):
    """
    Find missing market_history records for selected_date,
    then fetch all missing tickers in one Yahoo Finance request.

    Returns:
        missing_rows: list of market rows that need updating
        prices: DataFrame containing Close prices
    """

    selected = pd.Timestamp(selected_date)
    start = selected.strftime("%Y-%m-%d")
    end = (selected + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    # -----------------------------------------
    # 1. Get existing history for selected date
    # -----------------------------------------

    history = db().table("market_history") \
        .select("market_id,timestamp,value") \
        .gte("timestamp", f"{start}T00:00:00+00:00") \
        .lt("timestamp", f"{end}T00:00:00+00:00") \
        .execute().data

    existing = pd.DataFrame(history)

    existing_ids = set()

    if not existing.empty:
        for _, row in existing.iterrows():
            value = row.get("value")

            # Only consider it complete if value is not NULL
            if pd.notna(value):
                existing_ids.add(str(row["market_id"]))

    # -----------------------------------------
    # 2. Identify ONLY missing indices
    # -----------------------------------------

    missing_rows = []

    for _, row in markets_df.iterrows():

        market_id = str(row[idc])
        symbol = str(row[tc]).strip()

        if not symbol or symbol.lower() == "nan":
            continue

        # Already has valid data -> SKIP
        if market_id in existing_ids:
            continue

        missing_rows.append(row)

    if not missing_rows:
        return [], pd.DataFrame()

    # -----------------------------------------
    # 3. Download ALL missing tickers together
    # -----------------------------------------

    tickers = [
        str(row[tc]).strip()
        for row in missing_rows
    ]

    data = yf.download(
        tickers,
        start=start,
        end=end,
        progress=False,
        threads=True,
        auto_adjust=False
    )

    if data.empty:
        return missing_rows, pd.DataFrame()

    # -----------------------------------------
    # 4. Extract Close
    # -----------------------------------------

    if isinstance(data.columns, pd.MultiIndex):

        close = data["Close"]

    else:
        # Single ticker case
        symbol = tickers[0]

        if "Close" in data.columns:
            close = data[["Close"]].copy()
            close.columns = [symbol]
        else:
            close = pd.DataFrame()

    return missing_rows, close

def fill_missing_market_dates(
    markets_df,
    idc,
    tc,
    nc,
    target_dates,
    existing_dates=None,
    mode="all"
):
    """
    Fill missing market_history records.

    mode="all":
        Fill every missing ticker/date combination.

    mode="all_tickers":
        Fill only dates where ALL tickers are missing.

    Returns a report dictionary.
    """

    target_dates = sorted(
        pd.to_datetime(list(target_dates)).date.tolist()
    )

    if not target_dates:
        return {
            "attempted": 0,
            "inserted": 0,
            "remaining": 0,
            "failed": 0,
            "failures": [],
        }

    if existing_dates is None:
        existing_dates = {}

    progress = st.progress(0)
    status = st.empty()
    activity = st.empty()

    # -----------------------------------------
    # Build ticker list
    # -----------------------------------------

    market_rows = []

    for _, row in markets_df.iterrows():

        market_id = str(row[idc])

        symbol = str(
            row.get(tc, "")
        ).strip()

        if not symbol or symbol.lower() == "nan":
            symbol = str(
                row.get(nc, "")
            ).strip()

        if not symbol or symbol.lower() == "nan":
            continue

        market_rows.append({
            "market_id": market_id,
            "symbol": symbol,
        })

    if not market_rows:
        st.error("No valid market tickers were found.")
        return {
            "attempted": 0,
            "inserted": 0,
            "remaining": 0,
            "failed": 0,
            "failures": [],
        }

    # -----------------------------------------
    # Determine exactly what is missing
    # -----------------------------------------

    missing_pairs = []

    for target in target_dates:

        for market in market_rows:

            market_id = market["market_id"]
            symbol = market["symbol"]

            already_exists = (
                target in existing_dates.get(
                    market_id,
                    set()
                )
            )

            if not already_exists:

                missing_pairs.append({
                    "date": target,
                    "market_id": market_id,
                    "symbol": symbol,
                })

    # -----------------------------------------
    # ALL-TICKERS MODE
    # Only dates where every ticker is missing
    # -----------------------------------------

    if mode == "all_tickers":

        total_markets = len(market_rows)

        missing_by_date = {}

        for item in missing_pairs:

            missing_by_date.setdefault(
                item["date"],
                []
            ).append(item)

        allowed_dates = []

        for target in target_dates:

            missing_count = len(
                missing_by_date.get(target, [])
            )

            if missing_count == total_markets:
                allowed_dates.append(target)

        missing_pairs = [
            item
            for item in missing_pairs
            if item["date"] in allowed_dates
        ]

    # -----------------------------------------
    # Nothing to fill
    # -----------------------------------------

    if not missing_pairs:

        progress.progress(100)

        status.success(
            "Nothing to fill. No missing records matched this option."
        )

        return {
            "attempted": 0,
            "inserted": 0,
            "remaining": 0,
            "failed": 0,
            "failures": [],
        }

    # -----------------------------------------
    # Group missing records by date
    # -----------------------------------------

    dates_to_process = sorted(
        set(item["date"] for item in missing_pairs)
    )

    records_to_save = []

    failures = []

    attempted = len(missing_pairs)

    # -----------------------------------------
    # FETCH EACH DATE
    # -----------------------------------------

    for index, target in enumerate(
        dates_to_process,
        start=1
    ):

        percent = int(
            ((index - 1) / len(dates_to_process)) * 90
        )

        progress.progress(percent)

        status.info(
            f"Processing {index}/{len(dates_to_process)} · {target}"
        )

        date_items = [
            item
            for item in missing_pairs
            if item["date"] == target
        ]

        symbols = [
            item["symbol"]
            for item in date_items
        ]

        date_start = target.strftime("%Y-%m-%d")

        date_end = (
            target + timedelta(days=1)
        ).strftime("%Y-%m-%d")

        activity.write(
            f"🌐 Fetching {len(symbols)} ticker(s) for {target}..."
        )

        # -----------------------------------------
        # Yahoo Finance request
        # -----------------------------------------

        try:

            data = yf.download(
                symbols,
                start=date_start,
                end=date_end,
                progress=False,
                threads=True,
                auto_adjust=False,
                group_by="column",
            )

        except Exception as e:

            error_message = str(e)

            for item in date_items:

                failures.append({
                    "date": str(target),
                    "ticker": item["symbol"],
                    "error": error_message,
                })

            continue

        if data.empty:

            for item in date_items:

                failures.append({
                    "date": str(target),
                    "ticker": item["symbol"],
                    "error": "Yahoo Finance returned no data.",
                })

            continue

        # -----------------------------------------
        # Extract Close
        # -----------------------------------------

        try:

            if isinstance(
                data.columns,
                pd.MultiIndex
            ):

                close = data["Close"]

            else:

                if "Close" in data.columns:

                    close = data[["Close"]].copy()

                    close.columns = [
                        symbols[0]
                    ]

                else:

                    close = pd.DataFrame()

        except Exception as e:

            for item in date_items:

                failures.append({
                    "date": str(target),
                    "ticker": item["symbol"],
                    "error": f"Could not extract Close price: {e}",
                })

            continue

        # -----------------------------------------
        # Extract individual prices
        # -----------------------------------------

        for item in date_items:

            market_id = item["market_id"]
            symbol = item["symbol"]

            if symbol not in close.columns:

                failures.append({
                    "date": str(target),
                    "ticker": symbol,
                    "error": "Ticker was not returned by Yahoo Finance.",
                })

                continue

            try:

                price = close[symbol]

                if isinstance(price, pd.DataFrame):
                    price = price.iloc[:, 0]

                price.index = pd.to_datetime(
                    price.index
                ).tz_localize(None)

                selected = price[
                    price.index.normalize()
                    == pd.Timestamp(target).normalize()
                ].dropna()

                if selected.empty:

                    failures.append({
                        "date": str(target),
                        "ticker": symbol,
                        "error": "No trading price available for this date.",
                    })

                    continue

                close_value = float(
                    selected.iloc[0]
                )

                records_to_save.append({
                    "market_id": market_id,
                    "timestamp": iso(target),
                    "value": close_value,
                    "volume": 0,
                })

            except Exception as e:

                failures.append({
                    "date": str(target),
                    "ticker": symbol,
                    "error": str(e),
                })

    # -----------------------------------------
    # INSERT INTO SUPABASE
    # -----------------------------------------

    inserted = 0

    if records_to_save:

        status.info(
            f"Saving {len(records_to_save)} records to Supabase..."
        )

        activity.write(
            f"💾 Inserting {len(records_to_save)} recovered records..."
        )

        try:

            # Use chunks so one very large request
            # does not fail unnecessarily.

            chunk_size = 500

            for start in range(
                0,
                len(records_to_save),
                chunk_size
            ):

                chunk = records_to_save[
                    start:start + chunk_size
                ]

                db().table(
                    "market_history"
                ).upsert(
                    chunk,
                    on_conflict="market_id,timestamp"
                ).execute()

                inserted += len(chunk)

                progress.progress(
                    min(
                        98,
                        90 + int(
                            (
                                inserted
                                / len(records_to_save)
                            ) * 8
                        )
                    )
                )

        except Exception as e:

            # Database insertion itself failed.
            failures.append({
                "date": "DATABASE",
                "ticker": "DATABASE",
                "error": f"Supabase insert failed: {e}",
            })

    # -----------------------------------------
    # VERIFY DATABASE
    # -----------------------------------------

    status.info(
        "Verifying saved records..."
    )

    activity.write(
        "🔍 Checking Supabase for remaining missing records..."
    )

    # Re-read the affected date range from Supabase
    # to determine what is STILL missing.

    remaining = []

    try:

        start_date = min(
            dates_to_process
        )

        end_date = max(
            dates_to_process
        )

        history = (
            db()
            .table("market_history")
            .select("market_id,timestamp,value")
            .gte(
                "timestamp",
                f"{start_date.isoformat()}T00:00:00+00:00"
            )
            .lt(
                "timestamp",
                f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00+00:00"
            )
            .execute()
            .data
        )

        saved_df = pd.DataFrame(history)

        saved_dates = {}

        if not saved_df.empty:

            saved_df["market_id"] = (
                saved_df["market_id"]
                .astype(str)
            )

            saved_df["date_key"] = (
                pd.to_datetime(
                    saved_df["timestamp"]
                )
                .dt.tz_localize(None)
                .dt.date
            )

            saved_df = saved_df[
                saved_df["value"].notna()
            ]

            saved_dates = (
                saved_df
                .groupby("market_id")["date_key"]
                .apply(set)
                .to_dict()
            )

        for item in missing_pairs:

            target = item["date"]
            market_id = item["market_id"]
            symbol = item["symbol"]

            if target not in saved_dates.get(
                market_id,
                set()
            ):

                remaining.append({
                    "date": str(target),
                    "ticker": symbol,
                    "market_id": market_id,
                })

    except Exception as e:

        failures.append({
            "date": "VERIFICATION",
            "ticker": "DATABASE",
            "error": f"Could not verify Supabase records: {e}",
        })

    progress.progress(100)

    # -----------------------------------------
    # FINAL REPORT
    # -----------------------------------------

    status.success(
        "Gap filling completed."
    )

    st.success(
        f"""
        Gap filling completed.

        • Missing records identified: {attempted}
        • Records successfully saved: {inserted}
        • Records still missing: {len(remaining)}
        • Failed records: {len(failures)}
        """
    )

    if remaining:

        st.warning(
            f"⚠️ {len(remaining)} records are still missing."
        )

        st.markdown(
            "### Remaining missing records"
        )

        st.dataframe(
            pd.DataFrame(remaining),
            use_container_width=True,
            hide_index=True,
        )

    if failures:

        st.markdown(
            "### Errors / skipped records"
        )

        st.dataframe(
            pd.DataFrame(failures),
            use_container_width=True,
            hide_index=True,
        )

    return {
        "attempted": attempted,
        "inserted": inserted,
        "remaining": len(remaining),
        "failed": len(failures),
        "failures": failures,
    }


def scan_market_gaps(markets_df, idc, tc, nc, start_date, end_date):
    """Scan market_history and return all missing market/date combinations."""

    active_days = []
    current = start_date

    while current <= end_date:
        if current.weekday() < 5:
            active_days.append(current)
        current += timedelta(days=1)

    if not active_days:
        return {
            "active_days": [],
            "existing_dates": {},
            "results": [],
            "all_missing_dates": [],
        }

    history = (
        db()
        .table("market_history")
        .select("market_id,timestamp,value")
        .gte(
            "timestamp",
            f"{start_date.isoformat()}T00:00:00+00:00"
        )
        .lt(
            "timestamp",
            f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00+00:00"
        )
        .execute()
        .data
    )

    existing = pd.DataFrame(history)

    existing_dates = {}

    if not existing.empty:

        existing["market_id"] = (
            existing["market_id"]
            .astype(str)
        )

        timestamps = pd.to_datetime(
            existing["timestamp"],
            utc=True,
            errors="coerce"
        )

        existing["date_key"] = timestamps.dt.date

        # Only count records that actually contain a value
        existing = existing[
            existing["value"].notna()
        ]

        existing_dates = (
            existing
            .dropna(subset=["date_key"])
            .groupby("market_id")["date_key"]
            .apply(set)
            .to_dict()
        )

    # -----------------------------------------
    # VALID MARKET ROWS
    # -----------------------------------------

    market_rows = []
    seen_market_ids = set()

    for _, row in markets_df.iterrows():

        market_id = str(row[idc]).strip()

        symbol = str(
            row.get(tc, "")
        ).strip()

        if not symbol or symbol.lower() == "nan":
            symbol = str(
                row.get(nc, "")
            ).strip()

        if (
            not market_id
            or market_id.lower() == "nan"
            or not symbol
            or symbol.lower() == "nan"
        ):
            continue

        # Prevent duplicate market IDs
        if market_id in seen_market_ids:
            continue

        seen_market_ids.add(market_id)

        market_rows.append({
            "market_id": market_id,
            "symbol": symbol,
            "name": str(row.get(nc, "")),
        })

    results = []
    all_missing_dates = []

    # -----------------------------------------
    # FIND MISSING DATA
    # -----------------------------------------

    for market in market_rows:

        market_id = market["market_id"]
        symbol = market["symbol"]
        name = market["name"]

        missing_dates = []

        for d in active_days:

            if d not in existing_dates.get(
                market_id,
                set()
            ):
                missing_dates.append(
                    d.isoformat()
                )

        if missing_dates:

            results.append({
                "ticker": symbol,
                "index_name": name,
                "count": len(missing_dates),
                "missing_dates": ", ".join(missing_dates),
            })

    # -----------------------------------------
    # DATES WHERE ALL TICKERS ARE MISSING
    # -----------------------------------------

    for d in active_days:

        missing_for_date = []

        for market in market_rows:

            market_id = market["market_id"]
            symbol = market["symbol"]

            if d not in existing_dates.get(
                market_id,
                set()
            ):
                missing_for_date.append(symbol)

        if (
            len(missing_for_date) == len(market_rows)
            and len(market_rows) > 0
        ):

            all_missing_dates.append({
                "date": d.isoformat(),
                "count": len(missing_for_date),
                "missing_tickers": ", ".join(
                    missing_for_date
                ),
            })

    return {
        "active_days": active_days,
        "existing_dates": existing_dates,
        "results": results,
        "all_missing_dates": all_missing_dates,
    }


def _extract_close_dataframe(data):
    """Safely extract Close prices from any yfinance column layout."""

    if data is None or data.empty:
        return pd.DataFrame()

    try:

        if isinstance(
            data.columns,
            pd.MultiIndex
        ):

            close = None

            # Find which MultiIndex level contains "Close"
            for level in range(
                data.columns.nlevels
            ):

                values = list(
                    data.columns.get_level_values(level)
                )

                if "Close" in values:

                    close = data.xs(
                        "Close",
                        axis=1,
                        level=level
                    )

                    break

            if close is None:
                return pd.DataFrame()

        else:

            if "Close" not in data.columns:
                return pd.DataFrame()

            close = data[["Close"]].copy()

            # Single ticker
            close.columns = [
                "__single__"
            ]

        if isinstance(
            close,
            pd.Series
        ):
            close = close.to_frame()

        close.columns = [
            str(c).strip()
            for c in close.columns
        ]

        index = pd.to_datetime(
            close.index,
            errors="coerce"
        )

        if getattr(index, "tz", None) is not None:
            index = index.tz_convert(None)

        close.index = index

        return close

    except Exception:
        return pd.DataFrame()


def fill_missing_market_dates(
    markets_df,
    idc,
    tc,
    nc,
    target_dates,
    mode="all"
):
    """
    mode="all":
        Fill every missing ticker/date combination.

    mode="all_tickers":
        Fill only dates where ALL tickers are missing.
    """

    target_dates = sorted({
        pd.Timestamp(d).date()
        for d in target_dates
        if pd.Timestamp(d).weekday() < 5
    })

    if not target_dates:

        return {
            "attempted": 0,
            "inserted": 0,
            "remaining": 0,
            "failed": 0,
            "failures": [],
        }

    progress = st.progress(0)
    status = st.empty()
    activity = st.empty()

    # =========================================
    # BUILD MARKET LIST
    # =========================================

    market_rows = []
    seen_market_ids = set()

    for _, row in markets_df.iterrows():

        market_id = str(
            row[idc]
        ).strip()

        symbol = str(
            row.get(tc, "")
        ).strip()

        if not symbol or symbol.lower() == "nan":

            symbol = str(
                row.get(nc, "")
            ).strip()

        if (
            not market_id
            or market_id.lower() == "nan"
            or not symbol
            or symbol.lower() == "nan"
        ):
            continue

        if market_id in seen_market_ids:
            continue

        seen_market_ids.add(market_id)

        market_rows.append({
            "market_id": market_id,
            "symbol": symbol,
        })

    if not market_rows:

        st.error("No valid market tickers were found.")

        return {
            "attempted": 0,
            "inserted": 0,
            "remaining": 0,
            "failed": 0,
            "failures": [],
        }

    # =========================================
    # IMPORTANT:
    # READ DATABASE AGAIN
    #
    # Do NOT trust the old scan result.
    # =========================================

    try:

        db_history = (
            db()
            .table("market_history")
            .select("market_id,timestamp,value")
            .gte(
                "timestamp",
                f"{min(target_dates).isoformat()}T00:00:00+00:00"
            )
            .lt(
                "timestamp",
                f"{(max(target_dates) + timedelta(days=1)).isoformat()}T00:00:00+00:00"
            )
            .execute()
            .data
        )

    except Exception as e:

        logger.error(
            "Gap filler database read failed:\n%s",
            traceback.format_exc()
        )

        st.error(
            f"Could not read market_history from Supabase: {e}"
        )

        return {
            "attempted": 0,
            "inserted": 0,
            "remaining": 0,
            "failed": 1,
            "failures": [{
                "date": "DATABASE",
                "ticker": "DATABASE",
                "error": str(e),
            }],
        }

    existing_df = pd.DataFrame(
        db_history
    )

    existing_dates = {}

    if not existing_df.empty:

        existing_df["market_id"] = (
            existing_df["market_id"]
            .astype(str)
        )

        existing_df["date_key"] = (
            pd.to_datetime(
                existing_df["timestamp"],
                utc=True,
                errors="coerce"
            ).dt.date
        )

        existing_df = existing_df[
            existing_df["value"].notna()
        ]

        existing_dates = (
            existing_df
            .dropna(subset=["date_key"])
            .groupby("market_id")["date_key"]
            .apply(set)
            .to_dict()
        )

    # =========================================
    # DETERMINE EXACT MISSING RECORDS
    # =========================================

    missing_pairs = []

    for target in target_dates:

        for market in market_rows:

            market_id = market["market_id"]
            symbol = market["symbol"]

            if target not in existing_dates.get(
                market_id,
                set()
            ):

                missing_pairs.append({
                    "date": target,
                    "market_id": market_id,
                    "symbol": symbol,
                })

    # =========================================
    # OPTION 1:
    # ONLY DATES WHERE ALL TICKERS ARE MISSING
    # =========================================

    if mode == "all_tickers":

        total_markets = len(
            market_rows
        )

        missing_by_date = {}

        for item in missing_pairs:

            missing_by_date.setdefault(
                item["date"],
                []
            ).append(item)

        allowed_dates = []

        for target in target_dates:

            if len(
                missing_by_date.get(
                    target,
                    []
                )
            ) == total_markets:

                allowed_dates.append(
                    target
                )

        missing_pairs = [
            item
            for item in missing_pairs
            if item["date"] in allowed_dates
        ]

    # =========================================
    # NOTHING TO FILL
    # =========================================

    if not missing_pairs:

        progress.progress(100)

        status.success(
            "Nothing to fill."
        )

        return {
            "attempted": 0,
            "inserted": 0,
            "remaining": 0,
            "failed": 0,
            "failures": [],
        }

    attempted = len(
        missing_pairs
    )

    dates_to_process = sorted({
        item["date"]
        for item in missing_pairs
    })

    inserted = 0
    failures = []

    # =========================================
    # PROCESS EACH DATE
    # =========================================

    for date_number, target in enumerate(
        dates_to_process,
        start=1
    ):

        progress.progress(
            int(
                ((date_number - 1)
                / len(dates_to_process))
                * 90
            )
        )

        status.info(
            f"Processing {date_number}/"
            f"{len(dates_to_process)} · {target}"
        )

        date_items = [
            item
            for item in missing_pairs
            if item["date"] == target
        ]

        symbols = list(dict.fromkeys(
            item["symbol"]
            for item in date_items
        ))

        date_start = target.strftime(
            "%Y-%m-%d"
        )

        date_end = (
            target + timedelta(days=1)
        ).strftime("%Y-%m-%d")

        activity.write(
            f"🌐 Fetching {len(symbols)} ticker(s) "
            f"for {target}..."
        )

        # =====================================
        # YAHOO DOWNLOAD
        # =====================================

        try:

            data = yf.download(
                symbols,
                start=date_start,
                end=date_end,
                progress=False,
                threads=True,
                auto_adjust=False,
                group_by="column",
            )

        except Exception as e:

            error_message = str(e)

            logger.error(
                "Yahoo Finance failed for %s:\n%s",
                target,
                traceback.format_exc()
            )

            for item in date_items:

                failures.append({
                    "date": str(target),
                    "ticker": item["symbol"],
                    "error": error_message,
                })

            continue

        if data is None or data.empty:

            for item in date_items:

                failures.append({
                    "date": str(target),
                    "ticker": item["symbol"],
                    "error": (
                        "Yahoo Finance returned "
                        "no data."
                    ),
                })

            continue

        # =====================================
        # EXTRACT CLOSE
        # =====================================

        close = _extract_close_dataframe(
            data
        )

        if close.empty:

            for item in date_items:

                failures.append({
                    "date": str(target),
                    "ticker": item["symbol"],
                    "error": (
                        "Could not extract Close "
                        "price from Yahoo Finance."
                    ),
                })

            continue

        # =====================================
        # EXTRACT EACH TICKER
        # =====================================

        date_records = []
        date_pairs = []

        for item in date_items:

            market_id = item["market_id"]
            symbol = item["symbol"]

            if symbol not in close.columns:

                failures.append({
                    "date": str(target),
                    "ticker": symbol,
                    "error": (
                        "Ticker was not returned "
                        "by Yahoo Finance."
                    ),
                })

                continue

            try:

                price = close[symbol]

                if isinstance(
                    price,
                    pd.DataFrame
                ):
                    price = price.iloc[:, 0]

                selected = price[
                    price.index.normalize()
                    == pd.Timestamp(
                        target
                    ).normalize()
                ].dropna()

                if selected.empty:

                    failures.append({
                        "date": str(target),
                        "ticker": symbol,
                        "error": (
                            "No trading price available "
                            "for this date."
                        ),
                    })

                    continue

                close_value = float(
                    selected.iloc[0]
                )

                date_records.append({
                    "market_id": market_id,
                    "timestamp": iso(target),
                    "value": close_value,
                    "volume": 0,
                })

                date_pairs.append(
                    (market_id, symbol)
                )

            except Exception as e:

                logger.error(
                    "Price extraction failed for %s %s:\n%s",
                    target,
                    symbol,
                    traceback.format_exc()
                )

                failures.append({
                    "date": str(target),
                    "ticker": symbol,
                    "error": str(e),
                })

        # =====================================
        # SAVE THIS DATE IMMEDIATELY
        # =====================================

        if date_records:

            activity.write(
                f"💾 Saving {len(date_records)} "
                f"record(s) for {target}..."
            )

            try:

                db().table(
                    "market_history"
                ).upsert(
                    date_records,
                    on_conflict="market_id,timestamp"
                ).execute()

                inserted += len(
                    date_records
                )

                # Update local state
                for market_id, _ in date_pairs:

                    existing_dates.setdefault(
                        market_id,
                        set()
                    ).add(target)

            except Exception as e:

                logger.error(
                    "Supabase insert failed for %s:\n%s",
                    target,
                    traceback.format_exc()
                )

                for market_id, symbol in date_pairs:

                    failures.append({
                        "date": str(target),
                        "ticker": symbol,
                        "error": (
                            f"Supabase insert failed: {e}"
                        ),
                    })

        progress.progress(
            min(
                98,
                int(
                    (
                        date_number
                        / len(dates_to_process)
                    ) * 90
                )
            )
        )

    # =========================================
    # VERIFY DATABASE
    # =========================================

    status.info(
        "Verifying database..."
    )

    activity.write(
        "🔍 Checking Supabase for remaining records..."
    )

    remaining = []

    try:

        verify_history = (
            db()
            .table("market_history")
            .select(
                "market_id,timestamp,value"
            )
            .gte(
                "timestamp",
                f"{min(target_dates).isoformat()}T00:00:00+00:00"
            )
            .lt(
                "timestamp",
                f"{(max(target_dates) + timedelta(days=1)).isoformat()}T00:00:00+00:00"
            )
            .execute()
            .data
        )

        verify_df = pd.DataFrame(
            verify_history
        )

        verified_dates = {}

        if not verify_df.empty:

            verify_df["market_id"] = (
                verify_df["market_id"]
                .astype(str)
            )

            verify_df["date_key"] = (
                pd.to_datetime(
                    verify_df["timestamp"],
                    utc=True,
                    errors="coerce"
                ).dt.date
            )

            verify_df = verify_df[
                verify_df["value"].notna()
            ]

            verified_dates = (
                verify_df
                .dropna(subset=["date_key"])
                .groupby("market_id")["date_key"]
                .apply(set)
                .to_dict()
            )

        # Check ONLY the records this fill operation
        # was supposed to fill.
        for item in missing_pairs:

            if item["date"] not in verified_dates.get(
                item["market_id"],
                set()
            ):

                remaining.append({
                    "date": str(
                        item["date"]
                    ),
                    "ticker": item["symbol"],
                    "market_id": item["market_id"],
                })

    except Exception as e:

        logger.error(
            "Verification failed:\n%s",
            traceback.format_exc()
        )

        failures.append({
            "date": "VERIFICATION",
            "ticker": "DATABASE",
            "error": str(e),
        })

    progress.progress(100)

    # =========================================
    # FINAL REPORT
    # =========================================

    status.success(
        "Gap filling completed."
    )

    return {
        "attempted": attempted,
        "inserted": inserted,
        "remaining": len(remaining),
        "failed": len(failures),
        "failures": failures,
        "remaining_records": remaining,
    }


def value_text(v):
    return "" if v is None or (isinstance(v,float) and pd.isna(v)) else str(v)
def market_extra_inputs(record, excluded):
    """Render editable scalar market columns and return only the editable values."""
    payload={}
    for key, value in record.items():
        if key in excluded: continue
        label=key.replace("_"," ").title()
        if isinstance(value,bool): payload[key]=st.checkbox(label,value,key=f"market_{key}")
        elif isinstance(value,Real) and not isinstance(value,bool):
            if isinstance(value,int): payload[key]=st.number_input(label,value=int(value),step=1,key=f"market_{key}")
            else: payload[key]=st.number_input(label,value=float(value or 0),step=0.01,key=f"market_{key}")
        else: payload[key]=st.text_input(label,value_text(value),key=f"market_{key}")
    return payload

MAX_LOGIN_ATTEMPTS = 5
LOGIN_COOKIE = "budasai_admin_session"
LOGIN_DURATION_DAYS = 30
APP_VERSION = "26.9.1"

def login_cookies():
    return CookieController()

def session_token_hash(token):
    return hmac.new(
        str(st.secrets["SUPABASE_SERVICE_ROLE_KEY"]).encode(),
        token.encode(),
        "sha256",
    ).hexdigest()

def require_login():
    today = date.today().isoformat()
    cookies = login_cookies()
    if not st.session_state.get("login_cookie_loaded"):
        st.session_state.login_cookie_loaded = True
        st.rerun()
    session_token = (cookies.getAll() or {}).get(LOGIN_COOKIE)
    expected_username = str(st.secrets.get("ADMIN_USERNAME", ""))
    expected_password = str(st.secrets.get("ADMIN_PASSWORD", ""))

    if not expected_username or not expected_password:
        st.error("Login is not configured. Add `ADMIN_USERNAME` and `ADMIN_PASSWORD` to .streamlit/secrets.toml.")
        st.stop()

    if session_token and not st.session_state.get("authenticated"):
        try:
            session = db().table("admin_login_sessions").select(
                "session_token_hash,expires_at"
            ).eq("session_token_hash", session_token_hash(session_token)).limit(1).execute().data
            if session and str(session[0].get("expires_at", "")) > datetime.now(timezone.utc).isoformat():
                st.session_state.authenticated = True
        except Exception:
            pass

    try:
        record = db().table("admin_login_attempts").select(
            "login_key,attempt_date,failed_attempts"
        ).eq("login_key", "admin").limit(1).execute().data
        current = record[0] if record else None

        if current is None:
            db().table("admin_login_attempts").insert({
                "login_key": "admin",
                "attempt_date": today,
                "failed_attempts": 0,
            }).execute()
            failed_attempts = 0
        elif str(current.get("attempt_date")) != today:
            db().table("admin_login_attempts").update({
                "attempt_date": today,
                "failed_attempts": 0,
            }).eq("login_key", "admin").execute()
            failed_attempts = 0
        else:
            failed_attempts = int(current.get("failed_attempts") or 0)
    except Exception:
        st.error("Login storage is unavailable. Run `setup.sql` and check the Supabase secrets.")
        st.stop()

    if st.session_state.get("authenticated"):
        return

    st.markdown("## BudasAI Admin")
    st.caption("Sign in to continue.")
    with st.container(border=True):
        with st.form("admin_login"):
            username = st.text_input("Username", autocomplete="username")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("Sign in", type="primary")

        if failed_attempts >= MAX_LOGIN_ATTEMPTS:
            st.error("Too many failed attempts. Login is locked until tomorrow.")
        elif submitted:
            valid_username = hmac.compare_digest(username, expected_username)
            valid_password = hmac.compare_digest(password, expected_password)
            if valid_username and valid_password:
                session_token = token_secrets.token_urlsafe(32)
                expires_at = datetime.now(timezone.utc) + timedelta(days=LOGIN_DURATION_DAYS)
                db().table("admin_login_sessions").insert({
                    "session_token_hash": session_token_hash(session_token),
                    "expires_at": expires_at.isoformat(),
                }).execute()
                st.session_state["admin_login_cookie_writer"] = {}
                cookie_writer = CookieController("admin_login_cookie_writer")
                cookie_writer.set(
                    LOGIN_COOKIE,
                    session_token,
                    expires=expires_at.replace(tzinfo=None),
                    max_age=LOGIN_DURATION_DAYS * 24 * 60 * 60,
                )
                st.session_state.authenticated = True
                st.rerun()

            failed_attempts += 1
            db().table("admin_login_attempts").update({
                "failed_attempts": failed_attempts,
            }).eq("login_key", "admin").execute()
            if failed_attempts >= MAX_LOGIN_ATTEMPTS:
                st.error("Too many failed attempts. Login is locked until tomorrow.")
            else:
                remaining = MAX_LOGIN_ATTEMPTS - failed_attempts
                st.error(f"Incorrect username or password. {remaining} attempt(s) remaining today.")

    st.stop()

st.markdown("""
<style>
.stApp, .stApp p, .stApp label, .stApp span, .stApp div { color:#18212f; }
.stApp { background:#f8fafc; }
.block-container { max-width:1400px; padding-top:1.7rem; }
h1,h2,h3 { color:#1f4d72!important; }
[data-testid=stSidebar] { background:#edf4f8; }
[data-testid=stSidebar] * { color:#18212f!important; }
div[data-testid=stMetric] { background:#fff; border:1px solid #d5e1ea; border-radius:10px; padding:12px; }
[data-baseweb=input] input, [data-baseweb=textarea] textarea { background:#fff!important; color:#18212f!important; -webkit-text-fill-color:#18212f!important; }
[data-baseweb=input], [data-baseweb=textarea], [data-baseweb=select] > div, [data-baseweb=select] { background:#fff!important; border-color:#a9cddd!important; }
[data-baseweb=popover[role=listbox], [data-testid=stDateInput] div, [data-testid=stNumberInput] div { background:#fff!important; color:#18212f!important; }
[data-testid=stDataFrame], [data-testid=stDataEditor], [data-testid=stExpander] { background:#fff!important; border:1px solid #c7dfeb; border-radius:10px; }
[data-testid=stAlert] { background:#edf8ff!important; color:#18212f!important; }
[data-testid=stProgress] > div > div { background:#1976a8!important; }
[data-baseweb=select] * { color:#18212f!important; }
[data-testid=stTabs] button { color:#36536a!important; }
[data-testid=stTabs] button[aria-selected=true] { color:#1f4d72!important; }
.stButton button { background:#e8f1f7; color:#183e5a!important; border-color:#a9c5d7; }
.stButton button[kind=primary] { background:#1f6b99; color:#fff!important; border-color:#1f6b99; }
[data-testid="stSidebarNav"] {
  display: flex;
  flex-direction: column;
}
[data-testid="stSidebarNav"] > div:last-child {
  margin-top: auto;
  padding-top: 0.75rem;
}
.sidebar-version {
  display: block;
  margin-top: 1rem;
  padding: 0.45rem 0.7rem;
  border-radius: 999px;
  background: rgba(31, 62, 90, 0.08);
  border: 1px solid rgba(31, 62, 90, 0.2);
  color: #1f4d72;
  font-size: 0.75rem;
  font-weight: 600;
  text-align: center;
  letter-spacing: 0.02em;
  width: 100%;
  box-sizing: border-box;
}
</style>
""",unsafe_allow_html=True)
require_login()
st.title("BudasAI · Admin")
st.caption("Manage indices, daily prices, and research publishing from one private workspace.")
page=st.sidebar.radio("Admin area",["Overview","Market indices","Price history","Articles & sources","Daily News"])
st.sidebar.markdown('<div class="sidebar-version">version 26.9.2</div>', unsafe_allow_html=True)

if page=="Overview":
    m,a=rows("markets"),rows("research_articles")
    x,y=st.columns(2); x.metric("Market indices",len(m)); y.metric("Research articles",len(a))
    st.markdown("**Market indices** creates, edits, hides, and removes indices. **Price history** imports inception data, runs daily/backfill updates, and corrects saved OHLCV prices. **Articles & sources** gives you a live HTML preview before publishing.")

elif page == "Daily News":
    st.header("Daily News")
    st.caption("Fetch general, forex, crypto, and merger news; exclude Reuters and save new articles.")

    is_refreshing = st.session_state.get(
        "daily_news_refreshing",
        False
    )

    if st.button(
        "🔄 Refresh Daily News",
        type="primary",
        disabled=is_refreshing
    ):

        st.session_state.daily_news_refreshing = True

        progress = st.progress(0)
        status = st.empty()

        api_key = secret("News_Api_Key")
        records = []

        try:

            with st.spinner(
                "Refreshing Daily News..."
            ):

                # =========================================
                # STEP 1 — FETCH GENERAL NEWS
                # =========================================

                status.info(
                    "Fetching general, forex, crypto, and merger news from Finnhub..."
                )

                progress.progress(20)

                all_news = fetch_daily_news(
                    api_key
                )

                fetched_count = len(
                    all_news
                )

                status.info(
                    f"Received {fetched_count} articles. "
                    "Checking saved Finnhub IDs and filtering Reuters/GlobalNewswire..."
                )

                # =========================================
                # STEP 2 — EXCLUDE SOURCES AND DUPLICATES
                # =========================================

                existing_ids = existing_daily_news_ids()
                (
                    records,
                    excluded_source_count,
                    duplicate_count,
                    missing_id_count,
                ) = prepare_daily_news(all_news, existing_ids)

                progress.progress(40)

                status.info(
                    f"Excluded {excluded_source_count} Reuters/GlobalNewswire articles and "
                    f"skipped {duplicate_count} duplicate IDs. "
                    f"Saving {len(records)} new articles..."
                )

                # =========================================
                # STEP 3 — NO REMAINING ARTICLES
                # =========================================

                if not records:

                    progress.progress(100)

                    status.warning(
                        "No new eligible articles were returned. "
                        "Existing Daily News was not changed."
                    )

                    st.warning(
                        f"Fetched {fetched_count}; excluded "
                        f"{excluded_source_count} Reuters/GlobalNewswire articles, skipped "
                        f"{duplicate_count} duplicate IDs and "
                        f"{missing_id_count} articles without Finnhub IDs. "
                        "Existing Daily News was not changed."
                    )

                else:

                    # =====================================
                    # STEP 4 — SAVE NEW NEWS
                    # =====================================

                    status.info(
                        f"Saving {len(records)} new articles to Supabase..."
                    )

                    progress.progress(60)

                    db().table(
                        "daily_news"
                    ).insert(
                        records
                    ).execute()

                    progress.progress(100)

                    status.success(
                        "Daily News refreshed successfully."
                    )

                    st.success(
                        f"""
Daily News updated successfully.

• Finnhub articles fetched across 4 categories: {fetched_count}
• Reuters/GlobalNewswire articles excluded: {excluded_source_count}
• Duplicate Finnhub IDs skipped: {duplicate_count}
• Articles without Finnhub IDs skipped: {missing_id_count}
• New articles saved: {len(records)}
"""
                    )

        except Exception:

            progress.progress(
                100
                if records
                else 20
            )

            show_refresh_error(
                traceback.format_exc(),
                api_key
            )

        finally:

            st.session_state.daily_news_refreshing = False

    st.subheader("Manage saved news")
    try:
        delete_options = daily_news_delete_options()
    except Exception as error:
        delete_options = []
        st.error(f"Could not load saved news options: {error}")

    source_options = sorted(
        {
            str(article["source"])
            for article in delete_options
            if article.get("source") is not None
        },
        key=str.casefold,
    )
    database_id_options = list(
        dict.fromkeys(
            article["id"]
            for article in delete_options
            if article.get("id") is not None
        )
    )

    delete_by_date, delete_by_source, delete_by_id, reassign_ids = st.tabs(
        [
            "Delete before date",
            "Delete by source",
            "Delete by database ID",
            "Reassign IDs",
        ]
    )

    with delete_by_date:
        cutoff_date = st.date_input(
            "Delete articles published before",
            value=date.today(),
            key="news_delete_cutoff_date",
        )
        st.caption(
            "Articles dated before midnight UTC on this date will be deleted."
        )
        confirm_date_delete = st.checkbox(
            "I understand this permanently deletes matching articles.",
            key="news_confirm_date_delete",
        )
        if st.button(
            "Delete older news",
            type="primary",
            key="news_delete_before_date_button",
        ):
            if not confirm_date_delete:
                st.warning("Confirm the deletion before continuing.")
            else:
                cutoff = datetime.combine(
                    cutoff_date,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ).isoformat()
                try:
                    matching = (
                        db()
                        .table("daily_news")
                        .select("id", count="exact")
                        .lt("published_at", cutoff)
                        .execute()
                    )
                    match_count = matching.count
                    if match_count is None:
                        match_count = len(matching.data or [])

                    if match_count:
                        db().table("daily_news").delete().lt(
                            "published_at", cutoff
                        ).execute()
                        st.success(f"Deleted {match_count} older news articles.")
                    else:
                        st.info("No news articles matched that date.")
                except Exception as error:
                    st.error(f"Could not delete older news: {error}")

    with delete_by_source:
        if not source_options:
            st.info("No saved news sources are available to delete.")
        else:
            if st.session_state.get("news_delete_source") not in source_options:
                st.session_state.news_delete_source = source_options[0]
            selected_source = st.selectbox(
                "Source",
                source_options,
                key="news_delete_source",
            )
            confirm_source_delete = st.checkbox(
                "I understand this deletes every article from the selected source.",
                key="news_confirm_source_delete",
            )
            if st.button(
                "Delete source news",
                type="primary",
                key="news_delete_source_button",
            ):
                if not confirm_source_delete:
                    st.warning("Confirm the deletion before continuing.")
                elif selected_source not in source_options:
                    st.error("Select a valid news source.")
                else:
                    try:
                        matching = (
                            db()
                            .table("daily_news")
                            .select("id", count="exact")
                            .eq("source", selected_source)
                            .execute()
                        )
                        match_count = matching.count
                        if match_count is None:
                            match_count = len(matching.data or [])

                        if match_count:
                            db().table("daily_news").delete().eq(
                                "source", selected_source
                            ).execute()
                            st.success(
                                f"Deleted {match_count} article(s) from {selected_source}."
                            )
                        else:
                            st.info("No articles from that source remain.")
                    except Exception as error:
                        st.error(f"Could not delete source news: {error}")

    with delete_by_id:
        if not database_id_options:
            st.info("No saved database IDs are available to delete.")
        else:
            if st.session_state.get("news_delete_database_id") not in database_id_options:
                st.session_state.news_delete_database_id = database_id_options[0]
            selected_database_id = st.selectbox(
                "Database row ID",
                database_id_options,
                format_func=str,
                key="news_delete_database_id",
            )
            confirm_id_delete = st.checkbox(
                "I understand this permanently deletes the selected row.",
                key="news_confirm_database_id_delete",
            )
            if st.button(
                "Delete selected row",
                type="primary",
                key="news_delete_database_id_button",
            ):
                if not confirm_id_delete:
                    st.warning("Confirm the deletion before continuing.")
                elif selected_database_id not in database_id_options:
                    st.error("Select a valid database row ID.")
                else:
                    try:
                        db().table("daily_news").delete().eq(
                            "id", selected_database_id
                        ).execute()
                        st.success(
                            f"Deleted news database row {selected_database_id}."
                        )
                    except Exception as error:
                        st.error(f"Could not delete that database row: {error}")

    with reassign_ids:
        st.caption("The newest article receives ID 1; older articles receive increasing IDs.")
        st.warning(
            "This changes primary keys and may affect other tables that reference news IDs."
        )
        confirm_reassign_ids = st.checkbox(
            "I understand this changes every news database ID.",
            key="news_confirm_reassign_ids",
        )
        if st.button(
            "Reassign all news IDs",
            type="primary",
            key="news_reassign_ids_button",
        ):
            if not confirm_reassign_ids:
                st.warning("Confirm the ID reassignment before continuing.")
            else:
                try:
                    result = db().rpc("resequence_daily_news_ids").execute()
                    st.success(
                        f"Reassigned IDs for {result.data or 0} news articles."
                    )
                except Exception as error:
                    st.error(
                        "Could not reassign IDs. Run the updated setup.sql in "
                        f"Supabase first. Details: {error}"
                    )

elif page=="Market indices":
    st.header("Market indices")
    m=rows("markets"); idc=col(m,["id","market_id"]); nc=MARKET_NAME_COLUMN; tc=MARKET_SYMBOL_COLUMN; vc=col(m,["is_visible","visible","is_active","enabled"]); pc=col(m,MARKET_PRICE_COLUMNS)
    t1,t2,t3,t4,t5=st.tabs(["Index list","Add index","Edit / hide","Daily batch","Gap filler"])
    with t5:

        if m.empty or not idc:

            st.info("Add an index first.")

        else:

            selected_range = st.date_input(
                "Date range",
                value=(
                    date.today() - timedelta(days=30),
                    date.today()
                ),
                min_value=date(2000, 1, 1),
                max_value=date.today(),
                key="gap_date_range"
            )

            if isinstance(
                selected_range,
                (list, tuple)
            ) and len(selected_range) == 2:

                start_date, end_date = selected_range

            else:

                st.warning(
                    "Please select a start and end date."
                )
                st.stop()

            if start_date > end_date:

                st.warning(
                    "Start date must be before end date."
                )

            else:

                # =========================================
                # SCAN
                # =========================================

                if st.button(
                    "🔍 Scan for missing dates",
                    type="primary",
                    key="gap_scan_button"
                ):

                    with st.spinner(
                        "Scanning market_history..."
                    ):

                        try:

                            scan = scan_market_gaps(
                                m,
                                idc,
                                tc,
                                nc,
                                start_date,
                                end_date
                            )

                            st.session_state[
                                "gap_scan"
                            ] = {
                                "start_date": start_date,
                                "end_date": end_date,
                                **scan,
                            }

                        except Exception as e:

                            logger.error(
                                "Gap scan failed:\n%s",
                                traceback.format_exc()
                            )

                            st.error(
                                f"Gap scan failed: {e}"
                            )

                # =========================================
                # LOAD LAST SCAN
                # =========================================

                scan = st.session_state.get(
                    "gap_scan"
                )

                if scan:

                    results = scan["results"]
                    all_missing_dates = scan[
                        "all_missing_dates"
                    ]

                    # =====================================
                    # NORMAL MISSING DATA
                    # =====================================

                    if results:

                        st.markdown(
                            "### Missing data by ticker"
                        )

                        st.dataframe(
                            pd.DataFrame(results)[[
                                "ticker",
                                "index_name",
                                "count",
                                "missing_dates"
                            ]],
                            use_container_width=True,
                            hide_index=True
                        )

                    else:

                        st.success(
                            "No missing weekday data found "
                            "for the selected range."
                        )

                    # =====================================
                    # ALL-TICKER MISSING DATES
                    # =====================================

                    if all_missing_dates:

                        st.markdown(
                            "### Dates missing across ALL tickers"
                        )

                        st.dataframe(
                            pd.DataFrame(
                                all_missing_dates
                            )[[
                                "date",
                                "count",
                                "missing_tickers"
                            ]],
                            use_container_width=True,
                            hide_index=True
                        )

                    # =====================================
                    # FILL OPTIONS
                    # =====================================

                    st.markdown(
                        "### Fill missing data"
                    )

                    col_fill_all, col_fill_every = (
                        st.columns(2)
                    )

                    # =====================================
                    # OPTION 1
                    # ONLY DATES WHERE ALL TICKERS MISSING
                    # =====================================

                    with col_fill_all:

                        if st.button(
                            "Fill missing dates across all tickers",
                            type="primary",
                            disabled=not bool(
                                all_missing_dates
                            ),
                            key="gap_fill_all_tickers"
                        ):

                            selected_dates = [
                                date.fromisoformat(
                                    item["date"]
                                )
                                for item
                                in all_missing_dates
                            ]

                            report = (
                                fill_missing_market_dates(
                                    m,
                                    idc,
                                    tc,
                                    nc,
                                    selected_dates,
                                    mode="all_tickers"
                                )
                            )

                            st.markdown(
                                "### Fill report"
                            )

                            a, b, c, d = st.columns(4)

                            a.metric(
                                "Missing identified",
                                report["attempted"]
                            )

                            b.metric(
                                "Filled",
                                report["inserted"]
                            )

                            c.metric(
                                "Remaining",
                                report["remaining"]
                            )

                            d.metric(
                                "Errors",
                                report["failed"]
                            )

                            if report["remaining"] == 0:

                                st.success(
                                    "All selected missing "
                                    "records were filled."
                                )

                            else:

                                st.warning(
                                    f"{report['remaining']} "
                                    "records are still missing."
                                )

                            if report["failures"]:

                                with st.expander(
                                    "Show errors"
                                ):

                                    st.dataframe(
                                        pd.DataFrame(
                                            report["failures"]
                                        ),
                                        use_container_width=True,
                                        hide_index=True
                                    )

                            # Re-scan immediately
                            try:

                                refreshed_scan = (
                                    scan_market_gaps(
                                        m,
                                        idc,
                                        tc,
                                        nc,
                                        start_date,
                                        end_date
                                    )
                                )

                                st.session_state[
                                    "gap_scan"
                                ] = {
                                    "start_date": start_date,
                                    "end_date": end_date,
                                    **refreshed_scan,
                                }

                                st.success(
                                    "Scan refreshed. "
                                    "The tables above now reflect "
                                    "the current database."
                                )

                            except Exception as e:

                                st.error(
                                    f"Could not refresh scan: {e}"
                                )

                    # =====================================
                    # OPTION 2
                    # EVERY MISSING TICKER/DATE
                    # =====================================

                    with col_fill_every:

                        if st.button(
                            "Fill all missing dates",
                            type="primary",
                            disabled=not bool(results),
                            key="gap_fill_everything"
                        ):

                            selected_dates = sorted({
                                date.fromisoformat(
                                    missing_date
                                )
                                for row in results
                                for missing_date
                                in row[
                                    "missing_dates"
                                ].split(", ")
                                if missing_date
                            })

                            report = (
                                fill_missing_market_dates(
                                    m,
                                    idc,
                                    tc,
                                    nc,
                                    selected_dates,
                                    mode="all"
                                )
                            )

                            st.markdown(
                                "### Fill report"
                            )

                            a, b, c, d = st.columns(4)

                            a.metric(
                                "Missing identified",
                                report["attempted"]
                            )

                            b.metric(
                                "Filled",
                                report["inserted"]
                            )

                            c.metric(
                                "Remaining",
                                report["remaining"]
                            )

                            d.metric(
                                "Errors",
                                report["failed"]
                            )

                            if report["remaining"] == 0:

                                st.success(
                                    "All selected missing "
                                    "records were filled."
                                )

                            else:

                                st.warning(
                                    f"{report['remaining']} "
                                    "records are still missing."
                                )

                            if report["failures"]:

                                with st.expander(
                                    "Show errors"
                                ):

                                    st.dataframe(
                                        pd.DataFrame(
                                            report["failures"]
                                        ),
                                        use_container_width=True,
                                        hide_index=True
                                    )

                            # Re-scan immediately
                            try:

                                refreshed_scan = (
                                    scan_market_gaps(
                                        m,
                                        idc,
                                        tc,
                                        nc,
                                        start_date,
                                        end_date
                                    )
                                )

                                st.session_state[
                                    "gap_scan"
                                ] = {
                                    "start_date": start_date,
                                    "end_date": end_date,
                                    **refreshed_scan,
                                }

                                st.success(
                                    "Scan refreshed. "
                                    "The tables above now reflect "
                                    "the current database."
                                )

                            except Exception as e:

                                st.error(
                                    f"Could not refresh scan: {e}"
                                )

                    # =====================================
                    # EXPLANATION
                    # =====================================

                    st.caption(
                        "Option 1 fills only dates where every "
                        "ticker is missing. Option 2 fills every "
                        "missing ticker/date combination. "
                        "Remaining records can include market holidays "
                        "or tickers for which Yahoo Finance has no price."
                    )
    with t1:
        st.dataframe(m,width="stretch",hide_index=True) if not m.empty else st.info("No indices yet. Add your first one below.")
    with t2:
        with st.form("add"):
            name=st.text_input("Index name")
            ticker=st.text_input("Yahoo Finance ticker",placeholder="^GSPC")
            region=st.text_input("Region", placeholder="US")
            visible=st.checkbox("Visible in public product",True)
            go=st.form_submit_button("Create index",type="primary")
        if go:
            if not name or not ticker: st.warning("Name and ticker are required.")
            else:
                p={nc:name,tc:ticker,"region":region};  
                if vc:p[vc]=visible
                try: db().table("markets").insert(p).execute(); st.success("Index created."); st.rerun()
                except Exception as e: st.error(e)
    with t3:
        if m.empty or not idc: st.info("An existing markets row with an ID is needed.")
        else:
            opts=m[idc].astype(str).tolist(); chosen=st.selectbox("Index",opts,format_func=lambda v:f"{m[m[idc].astype(str)==v].iloc[0].get(nc,v)} ({m[m[idc].astype(str)==v].iloc[0].get(tc,'')})"); r=m[m[idc].astype(str)==chosen].iloc[0]
            with st.form("edit"):
                name=st.text_input("Index name",str(r.get(nc,""))); ticker=st.text_input("Yahoo Finance ticker",str(r.get(tc,""))); visible=st.checkbox("Visible in public product",bool(r.get(vc,True))) if vc else True
                extras=market_extra_inputs(r.to_dict(),MARKET_SYSTEM_COLUMNS|{nc,tc,vc}|set(MARKET_PRICE_COLUMNS))
                save=st.form_submit_button("Save changes",type="primary")
            if save:
                p={nc:name,tc:ticker,**extras};
                if vc:p[vc]=visible
                try: db().table("markets").update(p).eq(idc,chosen).execute(); st.success("Index updated."); st.rerun()
                except Exception as e:st.error(e)
            if pc:
                if st.button("Import latest price from Yahoo Finance",type="primary"):
                    try:
                        price=latest_price(str(r.get(tc,"")))
                        db().table("markets").update({pc:price}).eq(idc,chosen).execute()
                        st.success(f"Saved latest price: {price:,.2f}"); st.rerun()
                    except Exception as e: st.error(e)
            else:
                st.caption("Add a price column (for example `latest_price`) to `markets` to import the latest Yahoo Finance close into this table.")
            if st.button("Remove this index"):
                try: db().table("markets").delete().eq(idc,chosen).execute(); st.success("Index removed."); st.rerun()
                except Exception as e:st.error(e)
    with t4:

        if m.empty or not idc:

            st.info("Add an index first.")

        else:

            d = st.date_input(
                "Date to update / backfill",
                date.today()
            )

            hidden = st.checkbox(
                "Include hidden indices"
            )

            if st.button(
                "Run daily price batch",
                type="primary"
            ):

                # =========================================
                # PROGRESS
                # =========================================

                progress = st.progress(0)

                status = st.empty()

                activity = st.empty()

                # =========================================
                # STEP 1 — SELECT INDICES
                # =========================================

                status.info("Step 1/5 · Checking indices...")
                activity.write("🔄 Loading market indices from Supabase...")

                use = m.copy()

                if vc and not hidden:
                    use = use[use[vc].fillna(False)]

                if use.empty:

                    progress.progress(100)

                    status.warning(
                        "No indices available for update."
                    )

                    st.stop()

                progress.progress(10)

                # =========================================
                # STEP 2 — CHECK MISSING DATA
                # =========================================

                status.info(
                    f"Step 2/5 · Checking existing data for {d}..."
                )

                activity.write(
                    f"🔍 Checking {len(use)} indices in Supabase..."
                )

                missing_rows, close_data = bulk_fetch_daily_prices(
                    use,
                    idc,
                    tc,
                    d
                )

                total_indices = len(use)
                missing_count = len(missing_rows)
                skipped_count = total_indices - missing_count

                
                activity.write(
                    f"✓ Database check complete — "
                    f"{missing_count} indices need data, "
                    f"{skipped_count} are already up to date."
                )

                

                progress.progress(25)

                st.write(
                    f"**Total indices:** {total_indices}  \n"
                    f"**Already have data:** {skipped_count}  \n"
                    f"**Missing data:** {missing_count}"
                )

                # =========================================
                # NOTHING TO UPDATE
                # =========================================

                if missing_count == 0:
                    progress.progress(100)

                    status.success(
                        f"✓ Data is up to date for {d}"
                    )

                    st.success(
                        f"All {total_indices} indices already have valid "
                        f"price data for {d}. No update was required."
                    )

                    st.stop()       

                # =========================================
                # STEP 3 — FETCH DATA
                # =========================================

                status.info(
                    f"Step 3/5 · Fetching {missing_count} "
                    f"missing indices from Yahoo Finance..."
                )

                activity.write(
                    f"🌐 Downloading {missing_count} tickers from Yahoo Finance "
                    f"in one bulk request..."
                )

                if close_data.empty:

                    activity.write(
                        "✓ Yahoo Finance download completed. "
                        "Checking whether the selected date actually has trading data..."
                    )

                    progress.progress(100)

                    status.error(
                        "Yahoo Finance returned no data."
                    )

                    st.stop()

                progress.progress(55)

                # =========================================
                # STEP 4 — PREPARE BULK INSERT
                # =========================================

                status.info(
                    "Step 4/5 · Preparing missing price records..."
                )

                records = []
                successful_rows = []

                for row in missing_rows:

                    market_id = row[idc]
                    symbol = str(row[tc]).strip()

                    if symbol not in close_data.columns:
                        continue

                    try:

                        price = close_data[symbol]

                        # Make sure the selected date actually exists
                        price.index = pd.to_datetime(price.index).tz_localize(None)

                        selected_price = price[
                            price.index.normalize() == pd.Timestamp(d).normalize()
                        ].dropna()

                        if selected_price.empty:
                            st.write(
                                f"⏭️ {symbol}: No trading data for {d} — skipped "
                                f"(market may be closed)"
                            )
                            continue

                        close = float(selected_price.iloc[0])

                        records.append({
                            "market_id": market_id,
                            "timestamp": iso(d),
                            "value": close,
                            "volume": 0
                        })

                        successful_rows.append(
                            (row, symbol, close)
                        )

                    except Exception:
                        continue

                progress.progress(70)

                # =========================================
                # STEP 5 — BULK INSERT
                # =========================================

                status.info(
                    f"Step 5/5 · Saving {len(records)} "
                    f"price records to Supabase..."
                )

                if records:

                    db().table("market_history").upsert(
                        records,
                        on_conflict="market_id,timestamp"
                    ).execute()

                progress.progress(85)

                # =========================================
                # UPDATE MARKETS SNAPSHOT
                # =========================================

                status.info(
                    "Step 5/5 · Updating market snapshots..."
                )

                snapshot_success = []
                snapshot_failed = []

                if successful_rows:

                    for i, (row, symbol, close) in enumerate(successful_rows, 1):

                        try:
                            db().table("markets").update({
                                "current_value": close,
                                "previous_close": None,
                                "change_value": None,
                                "change_percent": None
                            }).eq(
                                idc,
                                row[idc]
                            ).execute()

                            snapshot_success.append(symbol)

                        except Exception as e:
                            snapshot_failed.append(
                                f"{symbol}: {str(e)}"
                            )

                        # Progress from 85% → 98%
                        progress.progress(
                            min(98, 85 + int((i / len(successful_rows)) * 13))
                        )

                else:
                    progress.progress(98)

                progress.progress(100)

                # =========================================
                # FINAL RESULT
                # =========================================

                status.success(
                    "Daily batch completed."
                )

                st.success(
                    f"""
                    Daily batch completed for {d}

                    • Total indices checked: {total_indices}
                    • Already had data: {skipped_count}
                    • Missing records checked: {missing_count}
                    • Trading prices found: {len(records)}
                    • Records inserted: {len(records)}
                    • Markets closed / no trading data: {missing_count - len(records)}
                    • Snapshots updated: {len(snapshot_success)}
                    """
                )

                if snapshot_success:

                    st.write(
                        "**Updated:**",
                        ", ".join(snapshot_success)
                    )

                if snapshot_failed:

                    st.warning(
                        "Some market snapshots could not be updated:\n\n"
                        + "\n".join(
                            f"- {x}"
                            for x in snapshot_failed
                        )
                    )   

elif page=="Price history":
    st.header("Market price history")
    m=rows("markets"); h=rows("market_history",500,"timestamp"); idc=col(m,["id","market_id"]); nc=col(m,["name","title"]) or "name"; tc=col(m,["ticker","yahoo_ticker","symbol","code"]) or "ticker"
    if m.empty or not idc: st.info("Create a market index before adding history.")
    else:
        mid=st.selectbox("Index",m[idc].astype(str),format_func=lambda v:str(m[m[idc].astype(str)==v].iloc[0].get(nc,v))); r=m[m[idc].astype(str)==mid].iloc[0]; a,b,c=st.tabs(["Import inception range","Update missed day","Manage records"])
        with a:
            with st.form("range"):
                sym=st.text_input("Ticker",str(r.get(tc,""))); s=st.date_input("Start / inception",date(2000,1,1)); e=st.date_input("End",date.today()); go=st.form_submit_button("Import history",type="primary")
            if go:
                try: st.success(f"Saved {finance(mid,sym,s,e)} daily prices.")
                except Exception as x: st.error(x)
        with b:
            d=st.date_input("Date",date.today(),key="one")
            if st.button("Fetch and update this date",type="primary"):
                try: st.success("Price updated." if finance(mid,str(r.get(tc,"")),d,d) else "No price available.")
                except Exception as x:st.error(x)
        with c:
            st.dataframe(h[h.market_id.astype(str)==mid] if not h.empty and "market_id" in h else h,width="stretch",hide_index=True)
            st.caption("Manual correction / new record")
            with st.form("manual"):
                d = st.date_input(
                    "Date",
                    date.today(),
                    key="manual"
                )

                z = st.columns(2)

                value = z[0].number_input(
                    "Value",
                    min_value=0.0,
                    value=0.0
                )

                volume = z[1].number_input(
                    "Volume",
                    min_value=0,
                    value=0
                )

                go = st.form_submit_button(
                    "Save record",
                    type="primary"
                )

            if go:
                try:
                    db().table("market_history").upsert(
                        [{
                            "market_id": mid,
                            "timestamp": iso(d),
                            "value": value,
                            "volume": volume
                        }],
                        on_conflict="market_id,timestamp"
                    ).execute()

                    st.success("Saved.")
                    st.rerun()

                except Exception as x:
                    st.error(x)

else:
    st.header("Articles & research sources")
    a=rows("research_articles"); s=rows("research_sources"); idc=col(a,["id","article_id"]); titlec=col(a,["title","name","headline"]) or "title"; htmlc=col(a,["body_html","content_html","content","body"]) or "body_html"; slugc=col(a,["slug"]); pubc=col(a,["published_at","is_published","published"])
    article_order_column = col(a, ["published_at", "created_at", "id", "article_id"])
    if article_order_column and not a.empty:
        a = a.sort_values(article_order_column, ascending=False, na_position="last")
    edit,src=st.tabs(["Article editor","Research sources"])


    with edit:
        options = ["New article"] + (
            [] if a.empty or not idc else a[idc].astype(str).tolist()
        )

        chosen = st.selectbox(
            "Article",
            options,
            format_func=lambda v: (
                "New article"
                if v == "New article"
                else str(
                    a[a[idc].astype(str) == v].iloc[0].get(titlec, v)
                )
            )
        )

        cur = (
            {}
            if chosen == "New article"
            else a[a[idc].astype(str) == chosen].iloc[0].to_dict()
        )

        st.subheader("Article details")

        # -------------------------
        # BASIC INFORMATION
        # -------------------------

        col1, col2 = st.columns(2)

        with col1:
            title = st.text_input(
                "Title *",
                value=str(cur.get("title", ""))
            )

            slug = st.text_input(
                "Slug *",
                value=str(cur.get("slug", ""))
            )

            subtitle = st.text_input(
                "Subtitle",
                value=str(cur.get("subtitle", ""))
            )

            category = st.text_input(
                "Category *",
                value=str(cur.get("category", ""))
            )

            author = st.text_input(
                "Author",
                value=str(cur.get("author", ""))
            )

        with col2:
            reading_time = st.text_input(
                "Reading time",
                value=str(cur.get("reading_time", ""))
            )

            cover_image = st.text_input(
                "Cover image URL",
                value=str(cur.get("cover_image", ""))
            )

            featured = st.checkbox(
                "Featured article",
                value=bool(cur.get("featured", False))
            )

            published_at_current = cur.get("published_at")

            published = st.checkbox(
                "Publish article",
                value=published_at_current is not None
            )

        # -------------------------
        # EXCERPT
        # -------------------------

        excerpt = st.text_area(
            "Excerpt *",
            value=str(cur.get("excerpt", "")),
            height=100,
            help="Short description used on article cards and SEO."
        )

        # -------------------------
        # ARTICLE HTML
        # -------------------------

        st.subheader("Article content")

        html = st.text_area(
            "Article HTML",
            value=str(cur.get("body_html", "")),
            height=500,
            help="Write the complete article HTML here."
        )


        # -------------------------
        # PREVIEW
        # -------------------------

        st.subheader("Live HTML preview")

        st.iframe(
            html or "<p>Start writing to preview.</p>",
            height=600
        )

        # -------------------------
        # SAVE ARTICLE
        # -------------------------

        save_article = st.button(
            "Create article" if chosen == "New article" else "Save article",
            type="primary"
        )

        if save_article:

            # Required fields
            if not title.strip():
                st.error("Title is required.")
                st.stop()

            if not slug.strip():
                st.error("Slug is required.")
                st.stop()

            if not excerpt.strip():
                st.error("Excerpt is required.")
                st.stop()

            if not category.strip():
                st.error("Category is required.")
                st.stop()

            # Published timestamp
            if published:
                if published_at_current:
                    published_at = published_at_current
                else:
                    published_at = datetime.now(timezone.utc).isoformat()
            else:
                published_at = None

            # -------------------------
            # COMPLETE DATABASE PAYLOAD
            # -------------------------

            payload = {
                "slug": slug.strip(),
                "title": title.strip(),
                "subtitle": subtitle.strip() or None,
                "excerpt": excerpt.strip(),
                "category": category.strip(),
                "author": author.strip() or None,
                "published_at": published_at,
                "reading_time": reading_time.strip() or None,
                "featured": featured,
                "cover_image": cover_image.strip() or None,
                "body_html": html,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }

            try:

                if chosen == "New article":

                    payload["created_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()

                    db().table(
                        "research_articles"
                    ).insert(payload).execute()

                    st.success("Article created successfully.")

                else:

                    db().table(
                        "research_articles"
                    ).update(payload).eq(
                        idc,
                        chosen
                    ).execute()

                    st.success("Article updated successfully.")

                st.rerun()

            except Exception as e:
                st.error(f"Failed to save article: {e}")
    with src:
        st.caption("Maintain the supporting research-source rows before publishing. This replaces the generic JSON table browser.")
        if s.empty:
            st.info("No readable `research_sources` rows found yet. Add a source below if this table exists in your database.")
        else:
            st.dataframe(s,width="stretch",hide_index=True)
        sid=col(s,["id","source_id"]); source_title=col(s,["title","name","source_name","citation"]) or "title"; urlc=col(s,["url","source_url","link"]); relation=col(s,["article_id","research_article_id"])
        choice="New source" if s.empty or not sid else st.selectbox("Source record",["New source"]+s[sid].astype(str).tolist())
        current={} if choice=="New source" else s[s[sid].astype(str)==choice].iloc[0].to_dict()
        related_options=[""]+([] if a.empty or not idc else a[idc].astype(str).tolist())
        related_value=str(current.get(relation,""))
        related_index=related_options.index(related_value) if related_value in related_options else 0
        with st.form("source_form"):
            source_name=st.text_input("Source title / citation",str(current.get(source_title,"")))
            source_url=st.text_input("Source URL",str(current.get(urlc,""))) if urlc else ""
            source_article=st.selectbox("Related article",related_options,index=related_index) if relation else ""
            save_source=st.form_submit_button("Add source" if choice=="New source" else "Save source",type="primary")
        if save_source:
            if not source_name: st.warning("A source title or citation is required.")
            else:
                payload={source_title:source_name}
                if urlc: payload[urlc]=source_url
                if relation and source_article: payload[relation]=source_article
                try:
                    (db().table("research_sources").insert(payload) if choice=="New source" else db().table("research_sources").update(payload).eq(sid,choice)).execute();st.success("Source saved.");st.rerun()
                except Exception as x: st.error(x)
        if choice!="New source" and st.button("Delete source"):
            try: db().table("research_sources").delete().eq(sid,choice).execute();st.success("Source deleted.");st.rerun()
            except Exception as x: st.error(x)
