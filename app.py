import os
import threading
import asyncio
import requests
import json
import time
from contextlib import contextmanager, asynccontextmanager
from queue import Queue, Full
from typing import Dict

from fastapi import FastAPI, HTTPException, Query
from telegram import Bot
from dotenv import load_dotenv
from redis_config import r
from db import engine, SessionLocal, get_db_session, logger

from models.orm import Base, PriceImpactCheck, Subscription, Trade
from websocket_order_book import WebSocketOrderBook
from analytics.order_flow import append_trade, generate_signal_score, price_impact_evaluator_worker


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Active C++ Core Streams
market_streams: Dict[str, WebSocketOrderBook] = {}
streams_lock = threading.Lock()
# Non-blocking async workers
trade_write_queue: "Queue[dict]" = Queue(maxsize=50000)
_writer_thread_started = False
_pubsub_thread_started = False
_evaluator_thread_started = False

def db_writer_worker():
    """Drains allocation queue cleanly without locking up hot path API requests."""
    while True:
        try:
            payload = trade_write_queue.get(timeout=1)
            task_type = payload.pop("task_type", "RAW_TRADE") # Default to old schema mapping
            
            db = SessionLocal()
            try:
                if task_type == "PRICE_IMPACT_CHECK":
                    # Deduplicate safely inside the background thread context
                    already_tracking = db.query(PriceImpactCheck).filter_by(
                        asset_id=payload["asset_id"],
                        is_completed=False
                    ).first()

                    if not already_tracking:
                        new_check = PriceImpactCheck(
                            slug=payload["slug"],
                            market_id=payload["market_id"],
                            asset_id=payload["asset_id"],
                            entry_price=payload["price"],
                            direction=payload["direction"],
                            entry_time=payload["entry_time"],
                            checkpoint_interval=payload["checkpoint_interval"],
                            target_check_time=payload["target_check_time"],
                            is_completed=False
                        )
                        db.add(new_check)
                        db.commit()
                        logger.info(f"Async worker initialized price impact monitoring for asset: {payload['asset_id']}")
                
                else: # Handle standard RAW_TRADE tracking
                    trade = Trade(**payload)
                    db.add(trade)
                    db.commit()
                    
            except Exception as e:
                db.rollback()
                logger.error(f"Async DB worker transaction dropped out: {e}")
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
        C++ engine handles filtration logic; Python updates metrics,
        caches live signals in Redis, and schedules DB updates.
        """
        # Extract numeric primitives for our math metrics safely
        price = float(details.get("price", 0.0))
        size = float(details.get("size", 0.0))
        usd = float(details.get("usd", 0.0))
        side = details.get("side", "BUY")
        trade_slug = details.get("slug", slug) # Fallback to context slug if needed

        # Pack a localized dictionary for our rolling memory windows
        trade_payload = {
            "price": price,
            "size": size,
            "usd": usd,
            "side": side,
            "timestamp": time.time()
        }

        # 1. Update In-Memory Order Flow Sliding Windows
        append_trade(trade_slug, trade_payload)

        # 2. Re-evaluate real-time signal metrics across all 4 timeframes
        signal_data = generate_signal_score(trade_slug, price, r)

        # 3. Cache the live Signal1Score inside Redis with a 5-minute TTL
        try:
            r.setex(f"signal:1:score:{trade_slug}", 300, json.dumps(signal_data))
        except Exception as re:
            logger.error(f"Redis signal caching failure for {trade_slug}: {re}")

        SIGNAL_THRESHOLD = 0.85
        EVALUATION_DELAY_SECONDS = 300

        if abs(signal_data["score"]) >= SIGNAL_THRESHOLD:
            try:
                trade_write_queue.put_nowait({
                    "task_type": "PRICE_IMPACT_CHECK",
                    "slug": trade_slug,
                    "market_id": details.get("market"),
                    "asset_id": str(details.get("asset_id")),
                    "price": price,
                    "direction": signal_data["direction"],
                    "entry_time": time.time(),
                    "checkpoint_interval": "5m",
                    "target_check_time": time.time() + EVALUATION_DELAY_SECONDS
                })
            except Full:
                logger.warning("Database write queue full! Dropping price impact tracking task.")

        # 4. Drop the raw trade record into the database execution queue (existing logic)
        try:
            trade_write_queue.put_nowait({
                "task_type": "RAW_TRADE",
                "slug": trade_slug,
                "market": details.get("market"),
                "asset_id": str(details.get("asset_id")),
                "price": price,
                "size": size,
                "usd": usd,
                "side": side,
                "question": details.get("question"),
                "outcome": details.get("outcome"),
                "timestamp": trade_payload["timestamp"],
            })
        except Full:
            logger.warning(f"Database write queue capacity breached. Dropping stats for slug: {trade_slug}")

        # 5. Handle Telegram Notifications Path (existing logic)
        # Check if this trade was routed via the C++ Priority system
        # If it's a priority match, it triggers our notification routines immediately
        raw_trade_context = details.get("raw", {})
        if raw_trade_context and usd > 0:
            # Check the original subscriptions map in Redis to find who needs the alert
            try:
                subscribers = r.hgetall(f"subscriptions:{trade_slug}")
                for cid, limit_str in subscribers.items():
                    if usd >= float(limit_str):
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
    
    global _writer_thread_started, _pubsub_thread_started, _evaluator_thread_started
    
    # 1. Start DB Writer Thread if not running
    if not _writer_thread_started:
        threading.Thread(target=db_writer_worker, daemon=True).start()
        _writer_thread_started = True

    # 2. Start Redis Pub/Sub Thread if not running
    if not _pubsub_thread_started:
        threading.Thread(target=listen_for_subscription_broadcasts, daemon=True).start()
        _pubsub_thread_started = True
    
    # 3. Start Price Impact Evaluator Thread if not running
    if not _evaluator_thread_started:
        threading.Thread(
            target=price_impact_evaluator_worker, 
            daemon=True
        ).start()
        _evaluator_thread_started = True

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
                    market_streams.pop(slug, None)
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

@app.get('/analytics/{slug}/signal1')
def get_signal_one(slug: str):
    """
    Sub-millisecond retrieval of the latest calculated Order Flow Imbalance matrix.
    Reads straight from the Redis memory layer to prevent hitting primary DB storage.
    """
    try:
        cached_signal = r.get(f"signal:1:score:{slug}")
        if not cached_signal:
            raise HTTPException(
                status_code=404, 
                detail="Signal trace stale or not found for this market."
            )
        return json.loads(cached_signal)
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Failed to query signal cache matrix for slug {slug}: {e}")
        raise HTTPException(status_code=500, detail="Internal signal pipeline breakdown.")