from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI(title="AI Trading Dashboard API", version="1.0.0")

# List of allowed origins
origins = [
    "https://board-front-end.vercel.app",
    "http://localhost:5173",
]

# Or read from environment variable
allowed_origins = os.getenv("ALLOWED_ORIGINS", "").split(",")
if allowed_origins and allowed_origins[0]:
    origins.extend(allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)