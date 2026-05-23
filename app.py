import os
import threading
import asyncio
import requests
import json
import time
from contextlib import asynccontextmanager
from queue import Queue, Full

from fastapi import FastAPI, HTTPException, Depends, Query
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from telegram import Bot
from dotenv import load_dotenv
import redis
from typing import Dict

from models.orm import Base, Subscription, Trade
from websocket_order_book import WebSocketOrderBook

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL", "redis://polymarket_redis:6379/0")

# Database config
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///polymarket.db")
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Redis Client
r = redis.from_url(REDIS_URL, decode_responses=True)
try:
    r.ping()
    print("Successfully connected to Redis!")
except redis.exceptions.AuthenticationError:
    print("Redis Authentication failed! Check your password in the URL.")
except redis.exceptions.ConnectionError:
    print("Could not connect to Redis. Check the hostname/network.")

# In memory state for active WebSocket connections (Key: slug, Value: WebSocketOrderBook)
# Deduplicates connections.
market_streams: Dict[str, WebSocketOrderBook] = {}

# Background DB write queue (non-blocking for WebSocket thread)
trade_write_queue: "Queue[dict]" = Queue(maxsize=10000)
_writer_thread_started = False

def db_writer_worker():
    """Runs in background thread, drains write queue."""
    while True:
        try:
            trade_data = trade_write_queue.get(timeout=1)
            db = SessionLocal()
            try:
                trade = Trade(**trade_data)
                db.add(trade)
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"DB write failed: {e}")
            finally:
                db.close()
            trade_write_queue.task_done()
        except Exception:
            continue

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def send_telegram_alert(chat_id, message):
    if not BOT_TOKEN or not chat_id:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id, message))
        loop.close()
    except Exception as e:
        print(f"Failed to send alert: {e}")

def get_token_ids(slug):
    gamma_url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        response = requests.get(gamma_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return None, "Event slug not found"

        event = data[0]
        all_token_ids = []

        for market in event.get('markets', []):
            token_ids = market.get('clobTokenIds')
            parsed = []
            if token_ids is None:
                pass
            elif isinstance(token_ids, (list, tuple)):
                parsed = [str(x) for x in token_ids]
            else:
                if isinstance(token_ids, str):
                    s = token_ids.strip()
                    try:
                        decoded = json.loads(s)
                        if isinstance(decoded, (list, tuple)):
                            parsed = [str(x) for x in decoded]
                        else:
                            parsed = [str(decoded)]
                    except Exception:
                        s = s.strip('[]')
                        parsed = [part.strip().strip('"').strip("'") for part in s.split(',') if part.strip()]
                else:
                    parsed = [str(token_ids)]
            all_token_ids.extend(parsed)
        return all_token_ids, None
    except Exception as e:
        return None, str(e)

def ensure_market_stream(slug):
    """
    Ensures a WebSocket connection exists for the given slug
    If it exists, does nothing. If not, starts it.
    """
    if slug in market_streams:
        return True, "Stream already active"

    assets_ids, error = get_token_ids(slug)
    if error:
        print(f"Could not start listener for {slug}: {error}")
        return False, error

    # Callback now handles all users for this slug
    def on_trade_callback(details):
        # details: dict with price/size/usd/market/asset_id/side/question/outcome
        # Enqueue DB write (non-blocking)
        try:
            trade_write_queue.put_nowait({
                "slug": slug,
                "market": details.get("market"),
                "asset_id": str(details.get("asset_id")),
                "price": details.get("price"),
                "size": details.get("size"),
                "usd": details.get("usd"),
                "side": details.get("side"),
                "question": details.get("question"),
                "outcome": details.get("outcome"),
                "timestamp": time.time(),
            })
        except Full:
            print("Write queue full, dropping trade")

        try:
            subscribers = r.hgetall(f"subscriptions:{slug}")
            for chat_id, limit in subscribers.items():
                try:
                    user_limit = float(limit)
                    if details.get("usd", 0) >= user_limit:
                        text = f"{details.get('side', '?')} @ {details.get('price')} ({details.get('usd', 0):.2f}$), {details.get('question')} {details.get('outcome')}"
                        send_telegram_alert(chat_id, text)
                except ValueError:
                    continue
        except Exception as e:
            print(f"Error accessing redis in callback: {e}")

    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Set min_size_usd=0 so socket gets everything
    # Filter per-user inside callback
    market_connection = WebSocketOrderBook(
        "market", url, assets_ids, on_trade_callback, True, min_size_usd=0
    )

    market_streams[slug] = market_connection

    def run_websocket():
        market_connection.run()
        # Cleanup if socket closes unexpectedly
        if slug in market_streams and market_streams[slug] == market_connection:
            del market_streams[slug]

    thread = threading.Thread(target=run_websocket)
    thread.daemon = True
    thread.start()
    return True, "Started"

# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    print("Database initialized")

    # Start DB writer thread once
    global _writer_thread_started
    if not _writer_thread_started:
        writer_thread = threading.Thread(target=db_writer_worker, daemon=True)
        writer_thread.start()
        _writer_thread_started = True

    # Sync Redis with DB on startup
    db = SessionLocal()
    try:
        subscriptions = db.query(Subscription).all()
        print(f"Restoring {len(subscriptions)} active subscriptions from DB to Redis")

        # Clear existing redis keys for safety
        keys = r.keys("subscriptions:*")
        if keys:
            r.delete(*keys)

        for sub in subscriptions:
            # Update Redis state
            r.hset(f"subscriptions:{sub.slug}", sub.chat_id, sub.limit_usd)
            # Ensure stream is running (Deduplicated)
            ensure_market_stream(sub.slug)
    finally:
        db.close()

    yield

    print("Shutting down... closing listeners")
    for key, listener in list(market_streams.items()):
        try:
            listener.close()
        except:
            pass
    market_streams.clear()

app = FastAPI(lifespan=lifespan)

# Routes

@app.get('/')
def health_check():
    # Check Redis
    try:
        r.ping()
        redis_status = "healthy"
    except:
        redis_status = "down"

    # Check DB
    try:
        db = SessionLocal()
        db.execute("SELECT 1")
        db.close()
        db_status = "healthy"
    except:
        db_status = "down"

    # Check active streams
    stream_count = len(market_streams)

    return {
        "status": "healthy",
        "service": "polymarket-analytics-api",
        "redis": redis_status,
        "database": db_status,
        "active_streams": stream_count
    }

@app.get('/get-event-details/{slug}')
def get_event_details(slug: str):
    gamma_url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        response = requests.get(gamma_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return HTTPException(status_code=404, detail="Event slug not found")

        event = data[0]
        market_info = []

        for market in event.get('markets', []):
            question = market.get("question") or "N/A"
            token_ids = market.get('clobTokenIds')

            if token_ids is None:
                parsed = []
            elif isinstance(token_ids, (list, tuple)):
                parsed = [str(x) for x in token_ids]
            else:
                if isinstance(token_ids, str):
                    s = token_ids.strip()
                    try:
                        decoded = json.loads(s)
                        if isinstance(decoded, (list, tuple)):
                            parsed = [str(x) for x in decoded]
                        else:
                            parsed = [str(decoded)]
                    except Exception:
                        s = s.strip('[]')
                        parsed = [part.strip().strip('"').strip("'") for part in s.split(',') if part.strip()]
                else:
                    parsed = [str(token_ids)]

            market_info.append({"question": question, "clobTokenIds": parsed})

        return {"title": event.get('title'), "markets": market_info}

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get('/get-live-trades/{slug}')
@app.get('/get-live-trades/{slug}/{limit}')
def get_live_trades(
        slug: str,
        limit: float = 0.0,
        chat_id: str = Query(..., description="Telegram Chat ID"),
        db: Session = Depends(get_db)
):
    # Persistence
    try:
        sub = db.query(Subscription).filter_by(chat_id=chat_id, slug=slug).first()
        if sub:
            sub.limit_usd = limit
        else:
            sub = Subscription(chat_id=chat_id, slug=slug, limit_usd=limit)
            db.add(sub)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    # Update Redis (Active State)
    try:
        r.hset(f"subscriptions:{slug}", chat_id, limit)
    except Exception as e:
        print(f"Redis error: {e}")

    # Start Listener (runtime)
    success, msg = ensure_market_stream(slug)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {
        "message": f"Started listening for {slug} with limit {limit}",
        "recipient": chat_id
    }

@app.get('/untrack/{slug}')
def untrack_market(
        slug: str,
        chat_id: str = Query(..., description="Telegram Chat ID"),
        db: Session = Depends(get_db)
):
    # Remove from DB
    try:
        sub = db.query(Subscription).filter_by(chat_id=chat_id, slug=slug).first()
        if sub:
            db.delete(sub)
            db.commit()
        else:
            raise HTTPException(status_code=404, detail=f"Not currently tracking {slug} in DB")
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        return HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    # Remove from Redis
    r.hdel(f"subscriptions:{slug}", chat_id)

    # Check if any subscribers left for this slug
    remaining = r.hlen(f"subscriptions:{slug}")

    # Manage Stream
    if remaining == 0:
        if slug in market_streams:
            try:
                market_streams[slug].close()
                del market_streams[slug]
                return {"message": f"Stopped tracking {slug} (Stream closed)"}
            except Exception as e:
                print(f"Error closing stream: {e}")

    return {"message": f"Stopped tracking {slug}"}
