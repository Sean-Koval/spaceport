"""Fresh contract-test servers with prompt, graceful fixture stop signaling.

Only the idle wait between Uvicorn's stock ticks changes. Startup, HTTP handling,
request/task draining and lifespan shutdown all remain the pinned implementation.
"""
import asyncio

import uvicorn


class LoopbackServer(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self._stop_requested = asyncio.Event()
        self._serving_loop = None

    def request_exit(self):
        def stop():
            self.should_exit = True
            self._stop_requested.set()
        if self._serving_loop is None:
            stop()
        else:
            self._serving_loop.call_soon_threadsafe(stop)

    async def main_loop(self):
        self._serving_loop = asyncio.get_running_loop()
        counter = 0
        should_exit = await self.on_tick(counter)
        while not should_exit:
            counter += 1
            counter = counter % 864000
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=0.1)
            except TimeoutError:
                pass
            should_exit = await self.on_tick(counter)
