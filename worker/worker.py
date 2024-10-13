import os
import logging
import asyncio
import signal
import time

import redis.asyncio as redis
import aiofiles
from aiolimiter import AsyncLimiter

# Configuration
TEMP_STORAGE_PATH = os.getenv("TEMP_STORAGE_PATH", "/app/temp_storage")
REDIS_URL = os.getenv("REDIS_URL", "redis://:redis_password@redis:6379")
CLAMAV_PORT = int(os.getenv("CLAMAV_PORT", 3310))
CLAMAV_HOST = os.getenv("CLAMAV_HOST", "envoy-av-lb")
SOCKET_TIMEOUT = int(os.getenv("SOCKET_TIMEOUT", 60))  # seconds
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", 100))  # Max number of concurrent tasks per instance
AV_REQUESTS_PER_SECOND = int(os.getenv("AV_REQUESTS_PER_SECOND", 100))  # Rate limit: requests per second per instance
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))  # Max number of retries before marking as unprocessable
VISIBILITY_TIMEOUT = int(os.getenv("VISIBILITY_TIMEOUT", 900))  # Time after which a task is considered stale and should be re-queued
STALE_TASK_CHECK_INTERVAL = int(os.getenv("STALE_TASK_CHECK_INTERVAL", 60))  # Interval at which the system checks for and re-queues stale tasks
MAX_PROCESSING_TIME = int(os.getenv("MAX_PROCESSING_TIME", 900))  # Maximum time allowed for processing a single file before timing out
MAX_BACKOFF_TIME = 64  # Max backoff time for retries in seconds

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Redis client initialization with a timeout to avoid potential blocking
async def create_redis_client():
    return await redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=SOCKET_TIMEOUT)

# Rate limiter
rate_limiter = AsyncLimiter(max_rate=AV_REQUESTS_PER_SECOND, time_period=1)

# Semaphore to limit concurrent tasks per instance
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

async def fetch_and_mark_in_progress(redis_client):
    """
    Atomically fetches a file ID from the queue and marks it as in progress with the current timestamp.
    Returns the file ID or None if no file is available.
    """
    script = """
    local file_id = redis.call('LPOP', KEYS[1])
    if file_id then
        redis.call('ZADD', KEYS[2], ARGV[1], file_id)
    end
    return file_id
    """
    now = int(time.time())
    try:
        # Adding a timeout to Redis operation
        file_id = await asyncio.wait_for(redis_client.eval(script, 2, 'file_processing_queue', 'file_processing_in_progress', now), timeout=SOCKET_TIMEOUT)
        return file_id
    except asyncio.TimeoutError:
        logging.error("Timeout occurred while fetching file ID from Redis")
        return None
    except redis.exceptions.RedisError:
        logging.exception("Error fetching file ID from Redis")
        return None

async def requeue_stale_tasks(redis_client):
    """
    Re-queues tasks that have been in progress for longer than the visibility timeout.
    """
    while True:
        try:
            now = int(time.time())
            stale_time = now - VISIBILITY_TIMEOUT
            stale_file_ids = await redis_client.zrangebyscore('file_processing_in_progress', '-inf', stale_time)
            if stale_file_ids:
                async with redis_client.pipeline() as pipe:
                    try:
                        pipe.multi()
                        pipe.zrem('file_processing_in_progress', *stale_file_ids)
                        if stale_file_ids:  # Ensure it's non-empty
                            pipe.lpush('file_processing_queue', *stale_file_ids)
                        await pipe.execute()
                        logging.info(f"Re-queued stale tasks: {stale_file_ids}")
                    except redis.exceptions.RedisError:
                        logging.exception("Error re-queuing stale tasks")
        except redis.exceptions.RedisError:
            logging.exception("Error during stale task check")
        await asyncio.sleep(STALE_TASK_CHECK_INTERVAL)

async def process_queue(redis_client) -> None:
    """
    Continuously processes files from the Redis queue.
    """
    tasks = []
    retries = 0

    while True:
        try:
            async with semaphore:
                file_id = await fetch_and_mark_in_progress(redis_client)
                if file_id and len(tasks) < MAX_CONCURRENT_TASKS:
                    task = asyncio.create_task(process_file_task(redis_client, file_id))
                    tasks.append(task)
                else:
                    await asyncio.sleep(0.5)  # Avoid busy-waiting
        except asyncio.CancelledError:
            logging.info("Queue processing cancelled.")
            raise
        except redis.exceptions.RedisError:
            retries += 1
            delay = min(2 ** retries, MAX_BACKOFF_TIME)
            logging.warning(f"Retrying after Redis error. Attempt {retries}, sleeping for {delay} seconds.")
            await asyncio.sleep(delay)  # Exponential backoff with a cap
        except Exception:
            logging.exception("Error fetching tasks from Redis")
            retries = 0  # Reset retries on any other errors
            await asyncio.sleep(1)
        else:
            retries = 0  # Reset retries on success
        
        # Clean up finished tasks
        for task in tasks:
            if task.done() and task.exception():
                logging.error(f"Task failed: {task.exception()}")
        tasks = [task for task in tasks if not task.done()]

    await asyncio.gather(*tasks)

async def process_file_task(redis_client, file_id: str) -> None:
    """
    Task to process a single file.
    """
    async with rate_limiter:
        logging.info(f"Processing file with ID {file_id}")
        success = False
        try:
            success = await asyncio.wait_for(process_file(redis_client, file_id), timeout=MAX_PROCESSING_TIME)
        except asyncio.TimeoutError:
            logging.error(f"Processing file {file_id} timed out.")
        except Exception:
            logging.exception(f"Error processing file with ID {file_id}")
        finally:
            await finalize_file_processing(redis_client, file_id, success)

async def finalize_file_processing(redis_client, file_id: str, success: bool):
    """
    Finalizes the processing of a file, either retrying or marking it as failed.
    """
    try:
        attempts = int(await redis_client.hincrby(file_id, "attempts", 1))
        await redis_client.zrem('file_processing_in_progress', file_id)
        if not success and attempts < MAX_RETRIES:
            await redis_client.lpush("file_processing_queue", file_id)
        elif not success and attempts >= MAX_RETRIES:
            await redis_client.hset(file_id, mapping={"status": "unprocessable"})
            logging.warning(f"File with ID {file_id} marked as unprocessable after {attempts} attempts.")
    except redis.exceptions.RedisError:
        logging.exception(f"Error finalizing file processing for file ID {file_id}")

async def process_file(redis_client, file_id: str) -> bool:
    """
    Processes a file by scanning it with ClamAV.
    """
    success = False
    file_path = None
    try:
        file_info = await redis_client.hgetall(file_id)
        if not file_info:
            logging.error(f"No file info found in Redis for file ID {file_id}")
            return False

        file_path = file_info.get("file_path")
        file_hash_hex = file_info.get("file_hash")
        attempts = int(file_info.get("attempts", "0"))

        if not file_path or not file_hash_hex:
            logging.error(f"Invalid file information for file ID {file_id}")
            return False

        if attempts >= MAX_RETRIES:
            logging.warning(f"File with ID {file_id} has exceeded the max retry limit ({MAX_RETRIES}). Marking as unprocessable.")
            await redis_client.hset(file_id, mapping={"status": "unprocessable", "file_hash": file_hash_hex})
            success = True
        else:
            if not await asyncio.to_thread(os.path.exists, file_path):
                logging.error(f"File {file_path} does not exist for file ID {file_id}")
                return False

            async with aiofiles.open(file_path, 'rb') as file:
                try:
                    scan_result = await scan_file(file)
                    is_infected = "unknown"
                    if "FOUND" in scan_result:
                        is_infected = "true"
                    elif "OK" in scan_result or "not found" in scan_result.lower():
                        is_infected = "false"

                    await redis_client.hset(file_id, mapping={
                        "status": scan_result,
                        "file_hash": file_hash_hex,
                        "is_infected": is_infected
                    })
                    logging.info(f"File with ID {file_id} scanned, result: {scan_result}, is_infected: {is_infected}")
                    success = True
                except Exception as e:
                    logging.exception(f"Error scanning file: {file_path}")
                    return False

    except Exception:
        logging.exception(f"Error processing file with ID {file_id}")
        await redis_client.hincrby(file_id, "attempts", 1)
        await redis_client.hset(file_id, mapping={"status": "Error occurred"})
        success = False
    finally:
        if file_path and (await asyncio.to_thread(os.path.exists, file_path)) and (success or attempts >= MAX_RETRIES):
            try:
                await asyncio.to_thread(os.remove, file_path)
                logging.info(f"Temporary file {file_path} removed")
            except FileNotFoundError:
                logging.warning(f"File {file_path} not found during removal")
            except Exception:
                logging.exception(f"Error removing temporary file {file_path}")
    return success

async def scan_file(file: aiofiles.threadpool.binary.AsyncBufferedReader) -> str:
    """
    Scans a file using ClamAV and returns the scan result.
    """
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(CLAMAV_HOST, CLAMAV_PORT),
            timeout=SOCKET_TIMEOUT
        )
        logging.info(f"Connected to ClamAV at {CLAMAV_HOST}:{CLAMAV_PORT}")

        writer.write(b"zINSTREAM\0")
        await writer.drain()

        chunk_size = 16384

        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break

            writer.write(len(chunk).to_bytes(4, byteorder="big") + chunk)
            await writer.drain()

        writer.write((0).to_bytes(4, byteorder="big"))
        await writer.drain()

        response = await asyncio.wait_for(reader.readline(), timeout=SOCKET_TIMEOUT)
        response = response.decode("utf-8").strip()
        logging.info(f"ClamAV response: {response}")

        if "FOUND" in response:
            parts = response.split(':', 1)
            return f"malware found: {parts[1].strip()}" if len(parts) > 1 else "malware found: unknown"
        if "OK" in response:
            return "malware not found"

        logging.warning(f"Unexpected ClamAV response: {response}")
        return "Unexpected error"

    except asyncio.TimeoutError:
        logging.error("Connection to ClamAV timed out")
        return "Connection timed out"
    except ConnectionError as e:
        logging.error(f"Connection error: {e}")
        return "Connection error"
    except Exception:
        logging.exception("Unexpected error during scanning")
        return "Unexpected error"
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        if reader:
            reader.feed_eof()

async def shutdown(sig, redis_client):
    """
    Gracefully shut down the event loop and all running tasks.
    """
    logging.info(f"Received exit signal {sig.name}. Cancelling all tasks.")
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        logging.info(f"Shutdown complete. Results: {results}")
    except Exception as e:
        logging.error(f"Error during task shutdown: {e}")
    await redis_client.close()  # Ensure redis connection is closed
    loop = asyncio.get_event_loop()
    await loop.shutdown_asyncgens()  # Ensures generators are closed properly

async def main():
    redis_client = await create_redis_client()

    # Setup signal handling inside the main task
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(shutdown(signal.SIGINT, redis_client)))
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown(signal.SIGTERM, redis_client)))

    await asyncio.gather(
        requeue_stale_tasks(redis_client),
        process_queue(redis_client)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.exception("Error running the application")
