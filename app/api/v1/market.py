from fastapi import APIRouter, HTTPException, Query
import httpx
import pandas as pd
from datetime import datetime, timedelta, timezone
import logging
import asyncio

router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@router.get("/market-data")
async def get_market_data(
    interval: str = Query("1h", regex="^(1h|2h|4h|6h|1d)$"),
    date: str = None
):
    # Use today's date in UTC if date not provided
    if not date:
        selected_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        try:
            selected_date = datetime.fromisoformat(date.replace('Z', '+00:00')).replace(tzinfo=None)
            selected_date = selected_date.replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")

    logger.info(f"Received request for /market-data with interval={interval}, date={selected_date.isoformat()}")

    symbols = ["ltcusdt"]  # Binance uses USDT pairs
    market_data = []

    # Interval configuration: (binance_interval, duration, expected_candles)
    interval_config = {
        '1h': ('1h', timedelta(hours=1), 24),
        '2h': ('1h', timedelta(hours=2), 12),
        '4h': ('1h', timedelta(hours=4), 6),
        '6h': ('1h', timedelta(hours=6), 4),
        '1d': ('1h', timedelta(days=1), 1),
    }

    binance_interval, interval_duration, expected_candles = interval_config[interval]

    async def get_klines(symbol: str, retries: int = 3):
        url = "https://api.binance.com/api/v3/klines"
        
        # Calculate start time to get enough historical data for all MAs
        periods_needed = 99  # For ma99
        start_time = selected_date - interval_duration * periods_needed
        end_time = selected_date + timedelta(days=1)
        
        params = {
            "symbol": symbol.upper(),
            "interval": binance_interval,
            "startTime": int(start_time.timestamp() * 1000),
            "endTime": int(end_time.timestamp() * 1000),
            "limit": 1000  # Binance max limit
        }

        for attempt in range(retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, params=params, timeout=30.0)
                    response.raise_for_status()
                    logger.info(f"Successfully fetched klines for {symbol}")
                    return symbol, response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error for {symbol}: {e.response.status_code} - {e.response.text}")
                await asyncio.sleep(2 ** attempt)
            except httpx.RequestError as e:
                logger.error(f"Network error for {symbol}: {str(e)}")
                await asyncio.sleep(1)
        logger.error(f"Failed to fetch klines for {symbol} after {retries} attempts")
        return symbol, []

    async def get_fallback_price(symbol: str):
        url = "https://api.binance.com/api/v3/ticker/price"
        params = {"symbol": symbol.upper()}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params, timeout=10.0)
                response.raise_for_status()
                data = response.json()
                price = float(data["price"])
                return {
                    "time": selected_date.strftime('%Y-%m-%d %H:%M:%S'),
                    "symbol": symbol.upper(),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "change": "+0.00%",
                    "ma7": price,
                    "ma25": price,
                    "ma99": price,
                    "volume": 0.0
                }
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch fallback price for {symbol}: {str(e)}")
            return None

    def calculate_metrics(symbol: str, klines: list, selected_date: datetime):
        if not klines:
            logger.warning(f"No data for {symbol}")
            return None

        interval_candles = []
        closes_for_ma = [float(k[4]) for k in klines]

        # Filter klines for the selected day only
        day_start = selected_date
        day_end = selected_date + timedelta(days=1)
        filtered_klines = [k for k in klines if day_start.timestamp() * 1000 <= k[0] < day_end.timestamp() * 1000]

        if not filtered_klines:
            logger.warning(f"No data for {symbol} within the selected day")
            return None

        # For intervals >1h, we need to aggregate 1h candles
        if interval != '1h':
            current_interval_start = day_start
            while current_interval_start < day_end:
                current_interval_end = current_interval_start + interval_duration
                
                # Get all 1h klines in this interval
                interval_klines = [
                    k for k in filtered_klines
                    if current_interval_start.timestamp() * 1000 <= k[0] < current_interval_end.timestamp() * 1000
                ]
                
                if interval_klines:
                    # Calculate OHLCV for the interval
                    open_price = float(interval_klines[0][1])
                    high_price = max(float(k[2]) for k in interval_klines)
                    low_price = min(float(k[3]) for k in interval_klines)
                    close_price = float(interval_klines[-1][4])
                    volume = sum(float(k[5]) for k in interval_klines)

                    # Find the index in the full klines list for MA calculations
                    current_timestamp = current_interval_start.timestamp() * 1000
                    kline_index = next((i for i, k in enumerate(klines) if k[0] >= current_timestamp), len(klines) - 1)
                    
                    # Calculate moving averages using the full history
                    ma7 = round(pd.Series(closes_for_ma[max(0, kline_index-6):kline_index+1]).mean(), 4)
                    ma25 = round(pd.Series(closes_for_ma[max(0, kline_index-24):kline_index+1]).mean(), 4)
                    ma99 = round(pd.Series(closes_for_ma[max(0, kline_index-98):kline_index+1]).mean(), 4)

                    candle = {
                        "time": current_interval_start.strftime('%Y-%m-%d %H:%M:%S'),
                        "symbol": symbol.upper(),
                        "open": round(open_price, 4),
                        "high": round(high_price, 4),
                        "low": round(low_price, 4),
                        "close": round(close_price, 4),
                        "change": f"{((close_price - open_price) / open_price * 100):+.2f}%",
                        "volume": round(volume, 4),
                        "ma7": ma7,
                        "ma25": ma25,
                        "ma99": ma99
                    }
                    interval_candles.append(candle)
                
                current_interval_start = current_interval_end
        else:
            # For 1h interval, just process each candle directly
            for kline in filtered_klines:
                open_time = datetime.fromtimestamp(kline[0] / 1000)
                open_price = float(kline[1])
                high_price = float(kline[2])
                low_price = float(kline[3])
                close_price = float(kline[4])
                volume = float(kline[5])

                # Find the index in the full klines list for MA calculations
                current_timestamp = kline[0]
                kline_index = next((i for i, k in enumerate(klines) if k[0] >= current_timestamp), len(klines) - 1)
                
                # Calculate moving averages using the full history
                ma7 = round(pd.Series(closes_for_ma[max(0, kline_index-6):kline_index+1]).mean(), 4)
                ma25 = round(pd.Series(closes_for_ma[max(0, kline_index-24):kline_index+1]).mean(), 4)
                ma99 = round(pd.Series(closes_for_ma[max(0, kline_index-98):kline_index+1]).mean(), 4)

                candle = {
                    "time": open_time.strftime('%Y-%m-%d %H:%M:%S'),
                    "symbol": symbol.upper(),
                    "open": round(open_price, 4),
                    "high": round(high_price, 4),
                    "low": round(low_price, 4),
                    "close": round(close_price, 4),
                    "change": f"{((close_price - open_price) / open_price * 100):+.2f}%",
                    "volume": round(volume, 4),
                    "ma7": ma7,
                    "ma25": ma25,
                    "ma99": ma99
                }
                interval_candles.append(candle)

        return interval_candles[:expected_candles]  # Ensure we don't return more than expected

    async with httpx.AsyncClient() as client:
        tasks = [get_klines(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for symbol, klines in results:
            if isinstance(klines, list) and klines:
                interval_data = calculate_metrics(symbol, klines, selected_date)
                if interval_data:
                    market_data.extend(interval_data)
                else:
                    fallback_data = await get_fallback_price(symbol)
                    if fallback_data:
                        market_data.append(fallback_data)
            else:
                fallback_data = await get_fallback_price(symbol)
                if fallback_data:
                    market_data.append(fallback_data)

    if not market_data:
        raise HTTPException(status_code=500, detail="No valid data retrieved for any symbols.")

    market_data = sorted(market_data, key=lambda x: (x["symbol"], x["time"]))
    logger.info(f"Response time: {len(market_data)} intervals processed")
    return market_data