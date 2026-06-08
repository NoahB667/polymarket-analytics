import os
import threading
import asyncio
import requests
import json
import time
import logging
from contextlib import contextmanager, asynccontextmanager
from queue import Queue, Full
from typing import Dict

from fastapi import FastAPI, HTTPException, Depends, Query
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from telegram import Bot
from dotenv import load_dotenv
import redis

from models.orm import Base, Subscription, Trade
from websocket_order_book import WebSocketOrderBook

load_dotenv()
logger = logging.getLogger("polymarket.api")

BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL", "redis://polymarket_redis:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///polymarket.db")

# Optimized Engine Configuration
engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

r = redis.from_url(REDIS_URL, decode_responses=True)

# Active C++ Core Streams
market_streams: Dict[str, WebSocketOrderBook] = {}
streams_lock = threading.Lock()
# Non-blocking async workers
trade_write_queue: "Queue[dict]" = Queue(maxsize=50000)
_writer_thread_started = False
_pubsub_thread_started = False


def db_writer_worker():
    """Drains allocation queue cleanly without locking up API requests."""
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
                logger.error(f"Async DB write dropped out: {e}")
            finally:
                db.close()
            trade_write_queue.task_done()
        except Exception:
            continue

def listen_for_subscription_broadcasts():
    pubsub = r.pubsub()
    pubsub.subscribe("channel:subscription_changes")
    
    for message in pubsub.listen():
        if message['type'] == 'message':
            try:
                payload = message['data']
                if ":" not in payload:
                    continue
                    
                slug, chat_id_str = payload.split(":", 1)
                chat_id = int(chat_id_str)  # Casts cleanly now that endpoints pass it down
                
                with streams_lock:
                    stream = market_streams.get(slug)
                    
                if stream:
                    with stream._cache_lock:
                        current_limit = stream._latest_subscriptions.get(chat_id)
                        
                    redis_limit_str = r.hget(f"subscriptions:{slug}", str(chat_id))
                    redis_limit = float(redis_limit_str) if redis_limit_str else None
                    
                    if current_limit == redis_limit:
                        continue
                        
                    stream.sync_subscriptions()
                    logger.info(f"Asynchronously synced subscription configuration for: {slug}")
            except Exception as ex:
                logger.error(f"Pub/Sub update parsing exception: {ex}")
                
@contextmanager
def get_db_session():
    """Provides a thread-safe, isolated database session context."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def send_telegram_alert(chat_id: str, message: str):
    """Fires fire-and-forget telegram notifications outside the hot execution path."""
    if not BOT_TOKEN or not chat_id:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id=chat_id, text=message))
        loop.close()
    except Exception as e:
        logger.error(f"Telegram dispatcher error context: {e}")

def get_token_ids(slug: str):
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
            if token_ids is None:
                continue
            if isinstance(token_ids, (list, tuple)):
                parsed = [str(x) for x in token_ids]
            elif isinstance(token_ids, str):
                s = token_ids.strip()
                try:
                    decoded = json.loads(s)
                    parsed = [str(x) for x in decoded] if isinstance(decoded, (list, tuple)) else [str(decoded)]
                except Exception:
                    s = s.strip('[]')
                    parsed = [part.strip().strip('"').strip("'") for part in s.split(',') if part.strip()]
            else:
                parsed = [str(token_ids)]
            all_token_ids.extend(parsed)
        return all_token_ids, None
    except Exception as e:
        return None, str(e)

def ensure_market_stream(slug: str) -> tuple[bool, str]:
    """Spawns an isolated C++ memory pipeline connection for incoming tokens."""
    if slug in market_streams:
        return True, "Stream already active"

    assets_ids, error = get_token_ids(slug)
    if error:
        return False, error

    # Streamlined unified dispatcher callback
    def on_trade_dispatched(details: dict):
        """
        Hot path pass-through.
        C++ engine handles filtration logic; Python simply handles persistent writes.
        """
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
            logger.warning(f"Database write queue capacity breached. Dropping stats for slug: {slug}")

        # Check if this trade was routed via the C++ Priority system
        # If it's a priority match, it triggers our notification routines immediately
        raw_trade_context = details.get("raw", {})
        if raw_trade_context and details.get("usd", 0.0) > 0:
            # Check the original subscriptions map in Redis to find who needs the alert
            try:
                subscribers = r.hgetall(f"subscriptions:{slug}")
                for cid, limit_str in subscribers.items():
                    if details.get("usd", 0.0) >= float(limit_str):
                        threading.Thread(
                            target=send_telegram_alert,
                            args=(cid, details.get("text")),
                            daemon=True
                        ).start()
            except Exception as ex:
                logger.error(f"Error checking redis alerts: {ex}")

    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    market_connection = WebSocketOrderBook(
        channel_type="market",
        url=url,
        data=assets_ids,
        message_callback=on_trade_dispatched,
        verbose=False,
        min_size_usd=0.0,
        redis_client=r,
        slug=slug
    )

    market_streams[slug] = market_connection

    def _run_lifecycle():
        market_connection.run()
        if slug in market_streams and market_streams[slug] == market_connection:
            del market_streams[slug]

    t = threading.Thread(target=_run_lifecycle, daemon=True)
    t.start()
    
    # Sync initial subscribers into C++ structures immediately
    market_connection.sync_subscriptions()
    return True, "Started"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    
    global _writer_thread_started, _pubsub_thread_started
    
    # 1. Start DB Writer Thread if not running
    if not _writer_thread_started:
        threading.Thread(target=db_writer_worker, daemon=True).start()
        _writer_thread_started = True

    # 2. Start Redis Pub/Sub Thread if not running
    if not _pubsub_thread_started:
        threading.Thread(target=listen_for_subscription_broadcasts, daemon=True).start()
        _pubsub_thread_started = True

    # Sync state tables non-blockingly on startup
    db = SessionLocal()
    try:
        subscriptions = db.query(Subscription).all()
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match="subscriptions:*", count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break

        for sub in subscriptions:
            r.hset(f"subscriptions:{sub.slug}", sub.chat_id, sub.limit_usd)
            ensure_market_stream(sub.slug)
    finally:
        db.close()

    yield

    # Clean shutdown logic
    for _, listener in list(market_streams.items()):
        try:
            listener.close()
        except Exception:
            pass
    market_streams.clear()

app = FastAPI(lifespan=lifespan)


@app.get('/')
def health_check():
    try:
        r.ping()
        redis_status = "healthy"
    except Exception:
        redis_status = "down"

    try:
        with engine.connect() as conn:
            conn.execute("SELECT 1")
        db_status = "healthy"
    except Exception:
        db_status = "down"

    return {
        "status": "healthy",
        "redis": redis_status,
        "database": db_status,
        "active_streams": len(market_streams)
    }


@app.get('/get-live-trades/{slug}')
def get_live_trades(
        slug: str,
        limit: float = 0.0,
        chat_id: int = Query(..., description="Telegram Chat ID"),
):
    with get_db_session() as db:
        try:
            sub = db.query(Subscription).filter_by(chat_id=str(chat_id), slug=slug).first()
            if sub:
                sub.limit_usd = limit
            else:
                sub = Subscription(chat_id=str(chat_id), slug=slug, limit_usd=limit)
                db.add(sub)
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Database synchronization loss: {str(e)}")

    r.hset(f"subscriptions:{slug}", str(chat_id), limit)

    success, msg = ensure_market_stream(slug)
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    r.publish("channel:subscription_changes", f"{slug}:{chat_id}")

    return {"message": f"Bound updates to {slug} on threshold limit {limit}", "recipient": chat_id}


@app.get('/untrack/{slug}')
def untrack_market(
        slug: str,
        chat_id: int = Query(..., description="Telegram Chat ID")
):
    with get_db_session() as db:
        try:
            # chat_id is now an integer, matching standard ORM lookup profiles
            sub = db.query(Subscription).filter_by(chat_id=str(chat_id), slug=slug).first()
            if sub:
                db.delete(sub)
                db.commit()
            else:
                raise HTTPException(status_code=404, detail="Subscription instance mapping not present.")
        except HTTPException as he:
            raise he
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=str(e))

    try:
        r.hdel(f"subscriptions:{slug}", str(chat_id))
        redis_hash_len = r.hlen(f"subscriptions:{slug}")
        no_remaining_subs = (redis_hash_len == 0 or redis_hash_len is None)
    except Exception as re:
        logger.error(f"Redis pipeline failure during untrack: {re}")
        no_remaining_subs = False

    with streams_lock:
        stream_is_active = slug in market_streams

    if no_remaining_subs:
        if stream_is_active:
            with streams_lock:
                if slug in market_streams:
                    market_streams[slug].close()
                    del market_streams[slug]
            return {"message": f"Evicted stream context mapping for {slug}"}
    else:
        if stream_is_active:
            r.publish("channel:subscription_changes", f"{slug}:{chat_id}")

    return {"message": f"Successfully unlinked user tracking profile context for {slug}"}

@app.get('/metrics')
def get_engine_metrics():
    """
    Queries real-time diagnostic counters and sub-microsecond latency 
    histograms directly from the live native C++ filtering core.
    """
    stream_metrics = {}
    
    for slug, stream in market_streams.items():
        try:
            # Pulls structural stats directly from C++ memory
            stream_metrics[slug] = stream.cpp_engine.get_stats()
        except Exception as e:
            stream_metrics[slug] = {"error": f"Failed to retrieve stats: {str(e)}"}
            
    return {
        "active_streams_count": len(market_streams),
        "database_write_queue_depth": trade_write_queue.qsize(),
        "streams": stream_metrics
    }