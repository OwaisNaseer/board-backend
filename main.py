import sys
sys.path.append("D:\\board\\boardBackend\\app")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import market

app = FastAPI(title="AI Trading Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market.router, prefix="/api/v1", tags=["market"])

@app.get("/")
async def root():
    return {"message": "Welcome to the AI Trading Dashboard API"}