from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import market  # relative import works in container

app = FastAPI(title="AI Trading Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all for now or your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market.router, prefix="/api/v1", tags=["market"])

@app.get("/")
async def root():
    return {"message": "Welcome to the AI Trading Dashboard API"}
