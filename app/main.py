import logging

from fastapi import FastAPI

from app.api.earnings import router as earnings_router
from app.api.market import router as market_router
from app.core.config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="Turkish Financial Market Bot MVP - BIST 100, Currencies & Gold Summaries"
)

app.include_router(market_router)
app.include_router(earnings_router)

@app.get("/health", summary="Health Check")
async def health_check():
    return {"status": "ok"}
