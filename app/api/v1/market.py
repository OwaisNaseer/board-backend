from fastapi import APIRouter, HTTPException, Query
import httpx
import pandas as pd
from datetime import datetime, timedelta, timezone
import logging
import asyncio
import re

router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BINANCE_BASE_URL = "https://api.binance.com/api/v3"

@router.get("/market-data")
async def get_market_data(
    symbol: str = Query(None, description="Market symbol (e.g., LTCUSDT, BTC, ETH)"),
    interval: str = Query("1h", regex="^(1s|1m|3m|5m|15m|30m|1h|2h|4h|6h|8h|12h|1d|3d|1w|1M)$"),
    date: str = Query(None, description="ISO date (e.g., 2025-08-11T19:00:00.000Z)"),
    end_date: str = Query(None, description="Optional end ISO date (e.g., 2025-08-13T19:00:00.000Z)")
):
    # Input validation
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    symbol = symbol.upper()
    if not symbol.endswith('USDT'):
        symbol += 'USDT'
        logger.info(f"Automatically appended USDT to symbol. New symbol: {symbol}")

    if not re.match(r'^[A-Z0-9]+$', symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol format. Use alphanumeric only (e.g., LTCUSDT, BTC)")

    # Verify symbol exists on Binance
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{BINANCE_BASE_URL}/exchangeInfo")
            response.raise_for_status()
            symbols_list = [s["symbol"] for s in response.json()["symbols"]]
            if symbol not in symbols_list:
                similar = [s for s in symbols_list if s.startswith(symbol.replace('USDT', ''))]
                suggestion = f" Did you mean {similar[0]}?" if similar else ""
                raise HTTPException(
                    status_code=400,
                    detail=f"Symbol '{symbol}' does not exist on Binance.{suggestion}"
                )
        except Exception as e:
            logger.error(f"Failed to verify symbol {symbol}: {e}")
            raise HTTPException(status_code=500, detail="Failed to verify symbol with Binance")

    # Parse date and align with Binance interval
    current_time = datetime.now(timezone.utc)
    interval_config = {
        '1s': ('1s', timedelta(seconds=1)),
        '1m': ('1m', timedelta(minutes=1)),
        '3m': ('3m', timedelta(minutes=3)),
        '5m': ('5m', timedelta(minutes=5)),
        '15m': ('15m', timedelta(minutes=15)),
        '30m': ('30m', timedelta(minutes=30)),
        '1h': ('1h', timedelta(hours=1)),
        '2h': ('2h', timedelta(hours=2)),
        '4h': ('4h', timedelta(hours=4)),
        '6h': ('6h', timedelta(hours=6)),
        '8h': ('8h', timedelta(hours=8)),
        '12h': ('12h', timedelta(hours=12)),
        '1d': ('1d', timedelta(days=1)),
        '3d': ('3d', timedelta(days=3)),
        '1w': ('1w', timedelta(days=7)),
        '1M': ('1M', timedelta(days=30)),
    }

    if interval not in interval_config:
        raise HTTPException(status_code=400, detail="Interval not supported")

    binance_interval, interval_duration = interval_config[interval]

    def align_to_interval(dt, interval_duration):
        """Align datetime to the nearest Binance interval start time."""
        total_seconds = int(dt.timestamp())
        interval_seconds = int(interval_duration.total_seconds())
        aligned_seconds = (total_seconds // interval_seconds) * interval_seconds
        return datetime.fromtimestamp(aligned_seconds, tz=timezone.utc)

    if not date:
        selected_date = align_to_interval(current_time, interval_duration)
    else:
        try:
            parsed_date = datetime.fromisoformat(date.replace('Z', '+00:00')).astimezone(timezone.utc)
            if parsed_date > current_time:
                raise HTTPException(status_code=400, detail="Start date cannot be in the future")
            selected_date = align_to_interval(parsed_date, interval_duration)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start date format")

    if end_date:
        try:
            parsed_end = datetime.fromisoformat(end_date.replace('Z', '+00:00')).astimezone(timezone.utc)
            if parsed_end > current_time:
                raise HTTPException(status_code=400, detail="End date cannot be in the future")
            end_date_dt = align_to_interval(parsed_end, interval_duration) + interval_duration
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end date format")
    else:
        end_date_dt = selected_date + timedelta(days=1)
        end_date_dt = align_to_interval(end_date_dt, interval_duration)

    # Ensure end_date_dt is after selected_date
    if end_date_dt <= selected_date:
        end_date_dt = selected_date + interval_duration

    logger.info(f"Request: symbol={symbol}, interval={interval}, date={selected_date.isoformat()}, end_date={end_date_dt.isoformat()}")

    # Check for too large range
    range_duration = (end_date_dt - selected_date).total_seconds()
    interval_seconds = interval_duration.total_seconds()
    expected_intervals = range_duration / interval_seconds
    if expected_intervals > 10000:
        raise HTTPException(status_code=400, detail="Requested range too large for the selected interval. Please reduce the date range or choose a larger interval.")

    market_data = []

    async def get_klines(symbol: str, retries: int = 3):
        url = f"{BINANCE_BASE_URL}/klines"
        periods_needed = 99  # For MA calculations
        start_time = selected_date - (interval_duration * periods_needed)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_date_dt.timestamp() * 1000)
        klines = []
        current_start = start_ms
        max_iterations = 100

        while current_start < end_ms and len(klines) < int(expected_intervals) + periods_needed and max_iterations > 0:
            params = {
                "symbol": symbol,
                "interval": binance_interval,
                "startTime": current_start,
                "endTime": end_ms,
                "limit": 1000,
            }

            for attempt in range(retries):
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(url, params=params, timeout=30.0)
                        response.raise_for_status()
                        data = response.json()
                        if not data:
                            return klines
                        klines.extend(data)
                        last_close_time = data[-1][6]  # close time
                        if last_close_time <= current_start:
                            logger.warning(f"Timestamp not advancing for {symbol}, breaking loop")
                            return klines
                        current_start = last_close_time + 1
                        logger.info(f"Fetched {len(data)} klines for {symbol} interval {binance_interval}")
                        break
                except httpx.HTTPStatusError as e:
                    logger.error(f"HTTP error for {symbol}: {e.response.status_code} - {e.response.text}")
                    if e.response.status_code == 429:
                        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please try again later.")
                    elif e.response.status_code >= 500:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
                except httpx.RequestError as e:
                    logger.error(f"Network error for {symbol}: {str(e)}")
                    if attempt == retries - 1:
                        return klines
                    await asyncio.sleep(2 ** attempt)
            max_iterations -= 1

        if max_iterations <= 0:
            logger.warning(f"Max iterations reached for {symbol}, returning partial data")
        return klines

    async def get_fallback_price(symbol: str):
        url = f"{BINANCE_BASE_URL}/ticker/price"
        params = {"symbol": symbol}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params, timeout=10.0)
                response.raise_for_status()
                data = response.json()
                price = float(data["price"])
                return {
                    "time": selected_date.strftime('%Y-%m-%d %H:%M:%S'),
                    "symbol": symbol,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "change": "+0.00%",
                    "ma7": price,
                    "ma25": price,
                    "ma99": price,
                    "volume": 0.0,
                    "ma7-ma25": 0.0,
                    "ma7-ma99": 0.0,
                    "ma25-ma99": 0.0
                }
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch fallback price for {symbol}: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to fetch fallback price data")

    def calculate_metrics(symbol: str, klines: list):
        if not klines:
            logger.warning(f"No data for {symbol}")
            return None

        filtered_klines = [
            k for k in klines
            if selected_date.timestamp() * 1000 <= k[0] < end_date_dt.timestamp() * 1000
        ]

        if not filtered_klines:
            logger.warning(f"No data for {symbol} within the selected range")
            return None

        closes = [float(k[4]) for k in klines]
        interval_candles = []

        for i, k in enumerate(filtered_klines):
            open_time = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
            open_price = float(k[1])
            high_price = float(k[2])
            low_price = float(k[3])
            close_price = float(k[4])
            volume = float(k[5])

            kline_index = next((j for j, kline in enumerate(klines) if kline[0] == k[0]), len(klines) - 1)
            ma7 = round(pd.Series(closes[max(0, kline_index - 6):min(kline_index + 1, len(closes))]).mean(), 4) if kline_index >= 6 else round(pd.Series(closes[:kline_index + 1]).mean(), 4)
            ma25 = round(pd.Series(closes[max(0, kline_index - 24):min(kline_index + 1, len(closes))]).mean(), 4) if kline_index >= 24 else round(pd.Series(closes[:kline_index + 1]).mean(), 4)
            ma99 = round(pd.Series(closes[max(0, kline_index - 98):min(kline_index + 1, len(closes))]).mean(), 4) if kline_index >= 98 else round(pd.Series(closes[:kline_index + 1]).mean(), 4)

            candle = {
                "time": open_time.strftime('%Y-%m-%d %H:%M:%S'),
                "symbol": symbol,
                "open": round(open_price, 4),
                "high": round(high_price, 4),
                "low": round(low_price, 4),
                "close": round(close_price, 4),
                "change": f"{((close_price - open_price) / open_price * 100):+.2f}%",
                "volume": round(volume, 4),
                "ma7": ma7,
                "ma25": ma25,
                "ma99": ma99,
                "ma7-ma25": round(ma7 - ma25, 4),
                "ma7-ma99": round(ma7 - ma99, 4),
                "ma25-ma99": round(ma25 - ma99, 4)
            }
            interval_candles.append(candle)

        return interval_candles

    klines = await get_klines(symbol)
    if klines:
        data = calculate_metrics(symbol, klines)
        if data:
            market_data.extend(data)
        else:
            fallback = await get_fallback_price(symbol)
            if fallback:
                market_data.append(fallback)
    else:
        fallback = await get_fallback_price(symbol)
        if fallback:
            market_data.append(fallback)

    if not market_data:
        raise HTTPException(status_code=500, detail="No valid data retrieved for the symbol.")

    # Sort data by time
    market_data = sorted(market_data, key=lambda x: datetime.strptime(x["time"], '%Y-%m-%d %H:%M:%S'))

    # Prepare response with metadata for pagination
    total_records = len(market_data)
    response = {
        "data": market_data,
        "meta": {
            "total_record": total_records,
            "page": 1,
            "per_page": total_records,
            "total_pages": 1
        }
    }

    logger.info(f"Response ready with {total_records} intervals")
    return response