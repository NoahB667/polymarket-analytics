"""Re-export from core.redis_config for backward compatibility."""

from core.redis_config import r, REDIS_URL

__all__ = ["r", "REDIS_URL"]
