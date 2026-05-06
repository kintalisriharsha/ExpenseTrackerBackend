from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from contextlib import asynccontextmanager
import logging
import os
from dotenv import load_dotenv
import uvicorn

logging.basicConfig(
    level= logging.WARNING,
    format='%(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Expense Tracker Backend",
    description="API for managing expense tracker",
    version="1.0.0",
    # lifespan=lifespace
    redirect_slashes=False
)

# """This is for the websites to check wheater the origins which browser calling was same."""

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins = ["*"],
#     allow_methods = ["*"],
#     allow_headers = ["*"]
# )

"""Only allow the https requests for retrofit"""
app.add_middleware(HTTPSRedirectMiddleware)

@app.get("/")
async def root():
    """Root Endpoint"""
    return{
        "message": "Root API",
        "version": "1.0.0",
        "docs":"/docs"
        }

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )