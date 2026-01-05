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

from WebSocketOrderBook import WebSocketOrderBook

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

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

# In memory state (needed for active WebSocket connections)
active_listeners = {}

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

def start_listener(chat_id, slug, limit):
    assets_ids, error = get_token_ids(slug)
    if error:
        print(f"Could not start listener for {slug}: {error}")
        return False, error

    def on_trade_callback(message_text):
        if chat_id:
            send_telegram_alert(chat_id, message_text)

    listener_key = f"{chat_id}_{slug}"

    # Close existing listener key if present
    if listener_key in active_listeners:
        try:
            active_listeners[listener_key].close()
        except Exception as e:
            print(f"Error closing existing listener: {e}")
        del active_listeners[listener_key]

    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    market_connection = WebSocketOrderBook(
        "market", url, assets_ids, on_trade_callback, True, min_size_usd=limit
    )

    active_listeners[listener_key] = market_connection

    def run_websocket():
        market_connection.run()
        if listener_key in active_listeners and active_listeners[listener_key] == market_connection:
            del active_listeners[listener_key]

    thread = threading.Thread(target=run_websocket)
    thread.daemon = True
    thread.start()
    return True, "Started"

# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    print("Database initialized")

    db = SessionLocal()
    try:
        subscriptions = db.query(Subscription).all()
        print(f"Restoring {len(subscriptions)} active subscriptions")
        for sub in subscriptions:
            print(f"Restarting tracker for {sub.slug} (Chat: {sub.chat_id}")
            start_listener(sub.chat_id, sub.slug, sub.limit_usd)
    finally:
        db.close()

    yield

    print("Shutting down... closing listeners")
    for key, listener in list(active_listeners.items()):
        try:
            listener.close()
        except:
            pass
    active_listeners.clear()

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

    # Start Listener (runtime)
    success, msg = start_listener(chat_id, slug, limit)
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

    # Stop listener
    listener_key = f"{chat_id}_{slug}"
    if listener_key in active_listeners:
        try:
            active_listeners[listener_key].close()
            return {"message": f"Stopped tracking {slug}"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error stopping track: {str(e)}")
    else:
        raise HTTPException(status_code=404, detail=f"Not currently tracking {slug}")

if __name__ == '__main__':
    import uvicorn
    is_debug = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=is_debug)
