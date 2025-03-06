import os
from mistralai.client import MistralClient
from mistralai.models.chat_completion import ChatMessage
import discord
import asyncio
import time
from typing import Optional
import backoff
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# Load environment variables
load_dotenv()

MISTRAL_MODEL = "mistral-large-latest"
SYSTEM_PROMPT = "You are a helpful assistant. Make all responses about stories only 4-5 sentances long"


class MistralAgent:
    def __init__(self):
        MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

        self.client = MistralClient(api_key=MISTRAL_API_KEY)
        self.executor = ThreadPoolExecutor(max_workers=1)
        # More conservative limits - 3 concurrent requests max
        self.semaphore = asyncio.Semaphore(3)
        # Track request timestamps for rate limiting
        self.request_timestamps = []
        # More conservative rate limit - 30 requests per minute
        self.requests_per_minute = 30
        # Minimum time between requests in seconds
        self.min_request_interval = 1.0
        self.last_request_time = 0
        
    def _is_rate_limited(self) -> bool:
        """Check if we're approaching rate limits"""
        now = time.time()
        # Remove timestamps older than 1 minute
        self.request_timestamps = [ts for ts in self.request_timestamps if now - ts < 60]
        return len(self.request_timestamps) >= self.requests_per_minute

    async def _wait_for_capacity(self):
        """Wait until we have capacity to make another request"""
        while True:
            now = time.time()
            
            # Check if we need to wait for the minimum interval
            time_since_last = now - self.last_request_time
            if time_since_last < self.min_request_interval:
                await asyncio.sleep(self.min_request_interval - time_since_last)
            
            # Check rate limits
            if not self._is_rate_limited():
                break
                
            # Wait longer if we're rate limited
            await asyncio.sleep(2)

    @backoff.on_exception(
        backoff.expo,
        Exception,  # Will catch any exception including rate limits
        max_tries=5,  # Increase max retries
        max_time=60,  # Increase max time to wait
        base=3,  # More aggressive backoff
        factor=1.5  # Multiply delay by this factor each retry
    )
    async def run(self, message: discord.Message) -> Optional[str]:
        """Send request to Mistral API with rate limiting and retries"""
        try:
            async with self.semaphore:
                await self._wait_for_capacity()
                
                # Create a chat message
                messages = [
                    ChatMessage(role="user", content=str(message.content))  # Convert message to string
                ]

                # Record request time
                now = time.time()
                self.last_request_time = now
                self.request_timestamps.append(now)
                
                # Run the API call in a thread pool to avoid blocking
                loop = asyncio.get_event_loop()
                chat_response = await loop.run_in_executor(
                    self.executor,
                    lambda: self.client.chat(
                        model="mistral-tiny",  # Using smaller model for faster responses
                        messages=messages
                    )
                )

                return chat_response.choices[0].message.content
                
        except Exception as e:
            print(f"Error in Mistral API call: {e}")
            # If it's a rate limit error, wait longer before retrying
            if "rate limit" in str(e).lower():
                await asyncio.sleep(5)
            raise  # Re-raise for backoff to handle