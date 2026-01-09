import os
import threading
import asyncio
import requests
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Query
from sqlalchemy import create_engine, Column, Integer, String, Float, UniqueConstraint
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from telegram import Bot
from dotenv import load_dotenv
import redis
from typing import Dict

from WebSocketOrderBook import WebSocketOrderBook

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL", "redis://polymarket_redis:6379/0")

# Database config
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///polymarket.db")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Subscription(Base):
    __tablename__ = "subscription"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(50), nullable=False)
    slug = Column(String(200), nullable=False)
    limit_usd = Column(Float, default=0.0)
    __table_args__ = (UniqueConstraint('chat_id', 'slug', name='_chat_slug_uc'),)

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
    def on_trade_callback(message_text, trade_value):
        # Redis Key: subscriptions:{slug} -> Hash { chat_id: limit_usd }
        try:
            subscribers = r.hgetall(f"subscriptions:{slug}")
            for chat_id, limit in subscribers.items():
                try:
                    user_limit = float(limit)
                    if trade_value >= user_limit:
                        send_telegram_alert(chat_id, message_text)
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
    return {"status": "healthy", "service": "polymarket-analytics-api"}

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
