# Standard Library Imports
import os
import time
from datetime import datetime
import logging
import uuid
import secrets
import hashlib

# Typing and Type Hinting Imports
from typing import Optional, List, AsyncGenerator

# Third-Party Library Imports
import redis.asyncio as redis
import aiofiles
from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, constr, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from enum import Enum
from fastapi.middleware.cors import CORSMiddleware

# Configuration using Pydantic BaseSettings
class Settings(BaseSettings):
    TEMP_STORAGE_PATH: str = Field(...)
    REDIS_URL: str = Field(...)
    MAX_FILE_SIZE: PositiveInt = Field(...)  # File size limit in MB
    MASTER_KEY: str = Field(...)
    UPLOAD_CHUNK_SIZE: PositiveInt = Field(...)  # Chunk size in bytes

    @property
    def MAX_FILE_SIZE_BYTES(self) -> int:
        return self.MAX_FILE_SIZE * 1_048_576  # Convert MB to bytes

    model_config = SettingsConfigDict(env_file='.env')

settings = Settings()

# Ensure TEMP_STORAGE_PATH exists
if not os.path.exists(settings.TEMP_STORAGE_PATH):
    os.makedirs(settings.TEMP_STORAGE_PATH)

app = FastAPI(
    title="AVService",
    description="Antivirus as a Service",
    version="0.0.1"
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Enums for status values
class ScanStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    FINISHED = "finished"
    UNPROCESSABLE = "unprocessable"

# Pydantic models for request and response
class ScanResponse(BaseModel):
    file_id: str
    status: str

class ResultResponse(BaseModel):
    file_id: str
    status: str
    is_infected: Optional[bool] = None
    detail: Optional[str] = None

class GenerateAPIKeyRequest(BaseModel):
    name: constr(min_length=1, max_length=100)
    rps: PositiveInt

class APIKeyUpdateRequest(BaseModel):
    name: constr(min_length=1, max_length=100)
    rps: Optional[PositiveInt] = None

class APIKeyDeleteRequest(BaseModel):
    name: constr(min_length=1, max_length=100)

class APIKeyResponse(BaseModel):
    api_key: str

class APIKeyInfoResponse(BaseModel):
    api_key: str
    name: str
    rps: int
    creation_date: str
    total_usage: int

class APIKeyDeleteResponse(BaseModel):
    detail: str

# Helper function for hashing API keys using SHA3-256
def hash_api_key(api_key: str) -> str:
    return hashlib.sha3_256(api_key.encode('utf-8')).hexdigest()

# Dependency to get Redis client
async def get_redis_client() -> AsyncGenerator[redis.Redis, None]:
    redis_client = await redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        yield redis_client
    finally:
        await redis_client.close()

# Dependency to verify master key
def verify_master_key(x_api_key: str = Header(...)):
    if x_api_key != settings.MASTER_KEY:
        logging.warning("Unauthorized API key access attempt")
        raise HTTPException(status_code=401, detail="Unauthorized")

# Dependency to verify API key
async def verify_api_key(x_api_key: str = Header(...), redis_client: redis.Redis = Depends(get_redis_client)) -> str:
    hashed_api_key = hash_api_key(x_api_key)
    key_name = f"apikey:{hashed_api_key}"

    if not await redis_client.exists(key_name):
        logging.warning("Unauthorized access attempt with invalid API key")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Get the name and RPS limit from Redis
    api_key_data = await redis_client.hgetall(key_name)

    api_key_name = api_key_data.get("name")
    rps = int(api_key_data.get("rps", 0))

    # Rate limiting using sliding window algorithm
    current_timestamp = int(time.time())
    counter_key = f"ratelimit:{hashed_api_key}:{current_timestamp}"

    # Increment the counter
    request_count = await redis_client.incr(counter_key)
    if request_count == 1:
        # Set expiration time of 1 second on the first request in this window
        await redis_client.expire(counter_key, 1)

    if request_count > rps:
        logging.warning(f"Rate limit exceeded for API key '{api_key_name}'")
        raise HTTPException(status_code=429, detail="Too Many Requests")

    # Increment total usage atomically
    await redis_client.hincrby(key_name, "total_usage", 1)

    logging.info(f"Authorized access with API key name: '{api_key_name}'. Total usage incremented.")
    return hashed_api_key

# File upload handler
async def handle_file_upload(file: UploadFile, file_id: str) -> str:
    temp_file_path = os.path.join(settings.TEMP_STORAGE_PATH, file_id)
    file_hash = hashlib.sha256()

    try:
        async with aiofiles.open(temp_file_path, 'wb') as temp_file:
            total_size = 0

            while True:
                chunk = await file.read(settings.UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break

                total_size += len(chunk)
                if total_size > settings.MAX_FILE_SIZE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File size exceeds the limit of {settings.MAX_FILE_SIZE} MB"
                    )

                file_hash.update(chunk)
                await temp_file.write(chunk)

        return file_hash.hexdigest()
    except Exception as e:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        logging.error(f"Error during file upload: {e}")
        raise e

# Store file metadata in Redis
async def store_file_metadata_in_redis(redis_client: redis.Redis, file_id: str, file_hash: str, temp_file_path: str):
    await redis_client.hset(file_id, mapping={
        "status": ScanStatus.PENDING.value,
        "file_hash": file_hash,
        "file_path": temp_file_path,
        "is_infected": "unknown",
        "attempts": 0  # Initialize the attempts counter
    })
    await redis_client.rpush("file_processing_queue", file_id)

# Health check endpoint
@app.get("/debug/api/v1/health", tags=["Debug"])
async def health_check(redis_client: redis.Redis = Depends(get_redis_client)):
    logging.info("Health check endpoint called")
    try:
        redis_status = await redis_client.ping()
        return {"status": "healthy", "redis": "connected" if redis_status else "disconnected"}
    except Exception as e:
        logging.error(f"Redis health check failed: {e}")
        return {"status": "unhealthy", "redis": "disconnected"}

# Scan upload endpoint
@app.post("/public/api/v1/scan", tags=["Public"], response_model=ScanResponse)
async def scan_upload(
    file: UploadFile = File(...),
    x_api_key: str = Depends(verify_api_key),
    redis_client: redis.Redis = Depends(get_redis_client)
):
    file_id = str(uuid.uuid4())
    logging.info(f"Received file upload request with file_id: {file_id}")

    try:
        # Handle the file upload and get the file hash
        file_hash = await handle_file_upload(file, file_id)

        # Store the file metadata in Redis
        await store_file_metadata_in_redis(
            redis_client,
            file_id,
            file_hash,
            os.path.join(settings.TEMP_STORAGE_PATH, file_id)
        )

        # Return the file_id as a response for further tracking
        return ScanResponse(file_id=file_id, status="File submitted for processing")

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logging.error(f"Error during file upload: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

# Get scan result endpoint
@app.get("/public/api/v1/scan/{file_id}", tags=["Public"], response_model=ResultResponse)
async def get_scan_result(
    file_id: str,
    x_api_key: str = Depends(verify_api_key),
    redis_client: redis.Redis = Depends(get_redis_client)
):
    logging.info(f"Scan result request for file_id: {file_id}")
    result = await redis_client.hgetall(file_id)
    if not result:
        logging.warning(f"File ID {file_id} not found in Redis")
        raise HTTPException(status_code=404, detail="File ID not found")

    status = result.get("status", "")

    is_infected = result.get("is_infected", "false") == "true"

    if status == ScanStatus.PENDING.value:
        return ResultResponse(
            file_id=file_id,
            status=ScanStatus.PROCESSING.value,
            detail="The file is being processed"
        )
    elif status == ScanStatus.UNPROCESSABLE.value:
        return ResultResponse(
            file_id=file_id,
            status=ScanStatus.UNPROCESSABLE.value,
            detail="The file could not be processed"
        )
    else:
        return ResultResponse(
            file_id=file_id,
            status=ScanStatus.FINISHED.value,
            is_infected=is_infected,
            detail=status
        )

# List API keys endpoint
@app.get(
    "/private/api/v1/apikeys",
    tags=["Private"],
    dependencies=[Depends(verify_master_key)],
    response_model=List[APIKeyInfoResponse]
)
async def list_api_keys(redis_client: redis.Redis = Depends(get_redis_client)):
    logging.info("Fetching list of all API keys")
    
    # Get all API key hashes
    keys = [key async for key in redis_client.scan_iter(match="apikey:*")]

    # Create a list to store API key info
    api_key_info_list = []
    for key in keys:
        api_key_data = await redis_client.hgetall(key)
        api_key_info_list.append(APIKeyInfoResponse(
            api_key=key.split(":", 1)[1],  # Hashed API key
            name=api_key_data.get("name"),
            rps=int(api_key_data.get("rps", 0)),
            creation_date=api_key_data.get("creation_date", ""),
            total_usage=int(api_key_data.get("total_usage", 0))
        ))

    return api_key_info_list

# Generate API key endpoint
@app.post(
    "/private/api/v1/apikeys",
    tags=["Private"],
    dependencies=[Depends(verify_master_key)],
    response_model=APIKeyResponse
)
async def generate_api_key(
    api_key_request: GenerateAPIKeyRequest,
    redis_client: redis.Redis = Depends(get_redis_client)
):
    # Normalize the name
    normalized_name = api_key_request.name.strip().lower()

    # Check if any existing API key has the same normalized name
    existing_hashed_api_key = await redis_client.get(f"apikey_name:{normalized_name}")
    if existing_hashed_api_key:
        logging.info(f"API key for name '{api_key_request.name}' already exists.")
        raise HTTPException(status_code=400, detail="API key with this name already exists.")

    # Generate a unique API key
    raw_api_key = secrets.token_urlsafe(32)  # 32 bytes (256 bits) of randomness

    # Hash API Key
    hashed_api_key = hash_api_key(raw_api_key)

    # Get the current timestamp
    creation_date = datetime.utcnow().isoformat()

    # Store the API key with associated name and rate limit in Redis
    key_name = f"apikey:{hashed_api_key}"
    await redis_client.hset(key_name, mapping={
        "key": hashed_api_key,
        "name": api_key_request.name,
        "rps": api_key_request.rps,
        "creation_date": creation_date,
        "total_usage": 0
    })

    # Store mapping from name to hashed_api_key
    await redis_client.set(f"apikey_name:{normalized_name}", hashed_api_key)

    logging.info(f"Generated new API key for name '{api_key_request.name}' with rate limit {api_key_request.rps}.")
    
    return APIKeyResponse(api_key=raw_api_key)

# Update API key endpoint
@app.patch("/private/api/v1/apikeys", tags=["Private"], dependencies=[Depends(verify_master_key)])
async def update_api_key(
    api_key_update: APIKeyUpdateRequest,
    redis_client: redis.Redis = Depends(get_redis_client)
):
    # Normalize the provided name
    normalized_name = api_key_update.name.strip().lower()

    # Retrieve hashed_api_key using the name mapping
    hashed_api_key = await redis_client.get(f"apikey_name:{normalized_name}")
    if not hashed_api_key:
        logging.warning(f"API key with name '{api_key_update.name}' not found")
        raise HTTPException(status_code=404, detail="API key with this name not found")

    key_name = f"apikey:{hashed_api_key}"

    logging.info(f"Attempting to update API key with name: '{api_key_update.name}'")

    # Update RPS if provided
    if api_key_update.rps is not None:
        await redis_client.hset(key_name, "rps", api_key_update.rps)
        logging.info(f"Updated rps for API key '{api_key_update.name}' to {api_key_update.rps}")

    return {"detail": f"API key with name '{api_key_update.name}' updated successfully"}

# Delete API key endpoint
@app.delete(
    "/private/api/v1/apikeys",
    tags=["Private"],
    dependencies=[Depends(verify_master_key)],
    response_model=APIKeyDeleteResponse
)
async def delete_api_key(
    api_key_delete: APIKeyDeleteRequest,
    redis_client: redis.Redis = Depends(get_redis_client)
):
    # Normalize the provided name
    normalized_name = api_key_delete.name.strip().lower()

    # Retrieve hashed_api_key using the name mapping
    hashed_api_key = await redis_client.get(f"apikey_name:{normalized_name}")
    if not hashed_api_key:
        logging.warning(f"API key with name '{api_key_delete.name}' not found")
        raise HTTPException(status_code=404, detail="API key with this name not found")

    key_name = f"apikey:{hashed_api_key}"

    api_key_data = await redis_client.hgetall(key_name)
    api_key_name = api_key_data.get("name")
    await redis_client.delete(key_name)
    await redis_client.delete(f"apikey_name:{normalized_name}")

    logging.info(f"API key with name '{api_key_name}' deleted successfully")

    return APIKeyDeleteResponse(detail=f"API key with name '{api_key_name}' deleted successfully")

# Exception handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logging.error(f"HTTPException: {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logging.error(f"Unhandled Exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust as needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Application startup and shutdown events
@app.on_event("startup")
async def startup_event():
    logging.info("FastAPI service started")

@app.on_event("shutdown")
async def shutdown_event():
    logging.info("FastAPI service shutdown")
