import os

class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.getenv("SECRET_KEY", "dev")
    WHALE_THRESHOLD = float(os.getenv("WHALE_THRESHOLD", 10000))
    POLY_API_BASE = os.getenv("POLY_API_BASE")
