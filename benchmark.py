import asyncio
import time
import os
import sys
import httpx
from threading import Thread
import uvicorn
import logging

# Ensure AUTH_TOKEN is set
os.environ["AUTH_TOKEN"] = "test_token"

def run_server():
    uvicorn.run("main:app", host="127.0.0.1", port=8000, log_level="warning")

async def test_health(client):
    try:
        start_time = time.time()
        resp = await client.get("http://127.0.0.1:8000/api/version", headers={"authorization": "Bearer test_token"})
        if resp.status_code == 200:
            latency = (time.time() - start_time) * 1000
            return latency
    except Exception as e:
        pass
    return -1

async def run_benchmark():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Wait for server to be up
        for _ in range(30):
            try:
                resp = await client.get("http://127.0.0.1:8000/api/version", headers={"authorization": "Bearer test_token"})
                if resp.status_code == 200:
                    break
            except Exception as e:
                pass
            await asyncio.sleep(1)

        # Baseline latency (no update running)
        print("Measuring baseline latency...")
        latencies = []
        for _ in range(10):
            lat = await test_health(client)
            if lat != -1:
                latencies.append(lat)
            await asyncio.sleep(0.1)

        if latencies:
            avg_baseline = sum(latencies)/len(latencies)
            print(f"Average baseline latency: {avg_baseline:.2f} ms")
        else:
            print("Could not measure baseline")

        # Trigger update and measure latency concurrently
        print("\nTriggering update and measuring latency...")
        update_task = asyncio.create_task(
            client.post("http://127.0.0.1:8000/api/update-ytdlp", headers={"authorization": "Bearer test_token"})
        )

        await asyncio.sleep(0.1) # give it a moment to start

        concurrent_latencies = []
        timeout_errors = 0
        # Make requests while it's updating
        for _ in range(50):
            if update_task.done():
                break
            try:
                start_time = time.time()
                resp = await client.get("http://127.0.0.1:8000/api/version", headers={"authorization": "Bearer test_token"}, timeout=1.0)
                if resp.status_code == 200:
                    lat = (time.time() - start_time) * 1000
                    concurrent_latencies.append(lat)
            except httpx.TimeoutException:
                timeout_errors += 1
            except Exception:
                pass
            await asyncio.sleep(0.1)

        await update_task

        print(f"Requests completed: {len(concurrent_latencies)}")
        print(f"Requests timed out (1s): {timeout_errors}")

        if concurrent_latencies:
            avg_concurrent = sum(concurrent_latencies)/len(concurrent_latencies)
            max_concurrent = max(concurrent_latencies)
            print(f"Average latency during update: {avg_concurrent:.2f} ms")
            print(f"Max latency during update: {max_concurrent:.2f} ms")
        else:
            print("No successful concurrent requests during update.")

if __name__ == "__main__":
    t = Thread(target=run_server, daemon=True)
    t.start()
    asyncio.run(run_benchmark())
