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
    interval: str = Query("1h", alias="interval[value]", regex="^(1m|3m|5m|15m|30m|1h|2h|4h|6h|8h|12h|1d)$"),
    date: str = Query(None, description="ISO date (e.g., 2025-08-11T19:00:00.000Z)")
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

    # Parse date and set range to cover only the selected day
    if not date:
        selected_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        try:
            parsed_date = datetime.fromisoformat(date.replace('Z', '+00:00')).astimezone(timezone.utc)
            selected_date = parsed_date.replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")

    end_date = selected_date + timedelta(days=1)

    logger.info(f"Request: symbol={symbol}, interval={interval}, date={selected_date.isoformat()}")

    market_data = []

    # Interval configuration: (binance_interval, duration, expected_candles)
    interval_config = {
        '1m': ('1m', timedelta(minutes=1), 1440),
        '3m': ('3m', timedelta(minutes=3), 480),
        '5m': ('5m', timedelta(minutes=5), 288),
        '15m': ('15m', timedelta(minutes=15), 96),
        '30m': ('30m', timedelta(minutes=30), 48),
        '1h': ('1h', timedelta(hours=1), 24),
        '2h': ('2h', timedelta(hours=2), 12),
        '4h': ('4h', timedelta(hours=4), 6),
        '6h': ('6h', timedelta(hours=6), 4),
        '8h': ('8h', timedelta(hours=8), 3),
        '12h': ('12h', timedelta(hours=12), 2),
        '1d': ('1d', timedelta(days=1), 1),
    }

    if interval not in interval_config:
        raise HTTPException(status_code=400, detail="Interval not supported")

    binance_interval, interval_duration, expected_candles = interval_config[interval]

    async def get_klines(symbol: str, retries: int = 3):
        url = f"{BINANCE_BASE_URL}/klines"
        # Fetch enough history for MA99
        periods_needed = 99
        start_time = selected_date - (interval_duration * periods_needed)
        params = {
            "symbol": symbol,
            "interval": binance_interval,
            "startTime": int(start_time.timestamp() * 1000),
            "endTime": int(end_date.timestamp() * 1000),
            "limit": expected_candles + periods_needed,  # Enough for MAs + requested candles
        }

        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, params=params, timeout=30.0)
                    response.raise_for_status()
                    logger.info(f"Fetched {len(response.json())} klines for {symbol} interval {binance_interval}")
                    return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error for {symbol}: {e.response.status_code} - {e.response.text}")
                await asyncio.sleep(2 ** attempt)
            except httpx.RequestError as e:
                logger.error(f"Network error for {symbol}: {str(e)}")
                await asyncio.sleep(1)
        logger.error(f"Failed to fetch klines for {symbol} after {retries} attempts")
        return []

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
            return None

    def calculate_metrics(symbol: str, klines: list):
        if not klines:
            logger.warning(f"No data for {symbol}")
            return None

        # Filter klines to selected day only
        filtered_klines = [
            k for k in klines
            if selected_date.timestamp() * 1000 <= k[0] < end_date.timestamp() * 1000
        ]

        if not filtered_klines:
            logger.warning(f"No data for {symbol} within the selected day")
            return None

        closes = [float(k[4]) for k in klines]  # All closes for MAs
        interval_candles = []

        for i, k in enumerate(filtered_klines):
            open_time = datetime.fromtimestamp(k[0] / 1000)
            open_price = float(k[1])
            high_price = float(k[2])
            low_price = float(k[3])
            close_price = float(k[4])
            volume = float(k[5])

            # Find index in full klines for MA calculations
            kline_index = next((j for j, kline in enumerate(klines) if kline[0] == k[0]), len(klines) - 1)
            ma7 = round(pd.Series(closes[max(0, kline_index - 6): kline_index + 1]).mean(), 4)
            ma25 = round(pd.Series(closes[max(0, kline_index - 24): kline_index + 1]).mean(), 4)
            ma99 = round(pd.Series(closes[max(0, kline_index - 98): kline_index + 1]).mean(), 4)

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

        return interval_candles[:expected_candles]

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

    market_data = sorted(market_data, key=lambda x: x["time"])
    logger.info(f"Response ready with {len(market_data)} intervals")
    return market_data