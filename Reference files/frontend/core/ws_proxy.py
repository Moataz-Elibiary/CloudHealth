"""
WebSocket proxy.
For each bastion tunnel, maintains a WebSocket connection to the backend.
Forwards all backend messages to the browser via the frontend FastAPI WS.
Sends heartbeat pings every 15 seconds.
Handles reconnection if connection drops mid-run.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional

import websockets

from tunnel_manager import TunnelInfo

log = logging.getLogger("frontend.ws_proxy")

HEARTBEAT_INTERVAL = 15   # seconds
RECONNECT_DELAY    = 3    # seconds
MAX_RECONNECTS     = 20


class ClusterProxy:
    """
    Manages the WebSocket connection to one backend (one bastion).
    Streams all received messages to `on_message` callback.
    """

    def __init__(
        self,
        tunnel:     TunnelInfo,
        config:     dict,
        on_message: Callable[[dict], None],
    ):
        self.tunnel     = tunnel
        self.config     = config           # AppConfig.to_backend_dict()
        self.on_message = on_message
        self._done      = False
        self._reconnects= 0

    async def run(self):
        """Connect, start checks, stream results. Reconnect on drop."""
        while not self._done and self._reconnects <= MAX_RECONNECTS:
            try:
                await self._session()
            except (websockets.exceptions.ConnectionClosed,
                    ConnectionRefusedError, OSError) as e:
                if self._done:
                    break
                self._reconnects += 1
                log.warning(f"[{self.tunnel.cluster_name}] WS disconnected ({e}), "
                            f"reconnecting ({self._reconnects}/{MAX_RECONNECTS}) "
                            f"in {RECONNECT_DELAY}s …")
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception as e:
                log.exception(f"[{self.tunnel.cluster_name}] Unexpected proxy error: {e}")
                break

        if self._reconnects > MAX_RECONNECTS:
            log.error(f"[{self.tunnel.cluster_name}] Max reconnects exceeded")
            self.on_message({
                "type":    "cluster_error",
                "cluster": self.tunnel.cluster_name,
                "message": "Lost connection to backend after maximum retries",
            })

    async def _session(self):
        url = self.tunnel.ws_url
        log.info(f"[{self.tunnel.cluster_name}] Connecting to backend: {url}")

        async with websockets.connect(url, ping_interval=None) as ws:
            # ── Await ready handshake ─────────────────────────────────────────
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") == "error":
                self.on_message({
                    "type":    "cluster_error",
                    "cluster": self.tunnel.cluster_name,
                    "message": msg.get("message", "Backend error"),
                })
                self._done = True
                return
            if msg.get("type") != "ready":
                log.warning(f"[{self.tunnel.cluster_name}] Unexpected first message: {msg}")

            # If this is a reconnect, ask for existing results first
            if self._reconnects > 0:
                await ws.send(json.dumps({"action": "get_results"}))
            else:
                # First connect — start checks
                await ws.send(json.dumps({
                    "action": "start_checks",
                    "config": self.config,
                }))

            # ── Message loop ──────────────────────────────────────────────────
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    # Stamp cluster name on every message for frontend routing
                    msg.setdefault("cluster", self.tunnel.cluster_name)
                    self.on_message(msg)
                    if msg.get("type") in ("all_done", "cluster_error"):
                        self._done = True
                        break
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _heartbeat(self, ws):
        """Send ping every HEARTBEAT_INTERVAL seconds."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await ws.send(json.dumps({"action": "ping"}))
            except Exception:
                break


class WSProxyOrchestrator:
    """
    Manages ClusterProxy instances for all bastions.
    Collects messages from all proxies and forwards them to the browser
    via a single asyncio.Queue that the frontend FastAPI WS reads from.
    """

    def __init__(self, tunnels: Dict[str, TunnelInfo], backend_config: dict):
        self.tunnels        = tunnels
        self.backend_config = backend_config
        self._browser_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    @property
    def browser_queue(self) -> asyncio.Queue:
        return self._browser_queue

    async def run_all(self):
        """Start all proxies in parallel and wait for all to complete."""
        def _on_msg(msg: dict):
            try:
                self._browser_queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

        proxies = []
        for name, tunnel in self.tunnels.items():
            if tunnel.error:
                # Unreachable bastion — push error message directly
                _on_msg({
                    "type":    "cluster_error",
                    "cluster": name,
                    "message": f"Bastion unreachable: {tunnel.error}",
                })
                continue
            proxy = ClusterProxy(tunnel, self.backend_config, _on_msg)
            proxies.append(proxy.run())

        # Run all proxies; put sentinel when all done
        if proxies:
            await asyncio.gather(*proxies, return_exceptions=True)

        # Signal browser that everything is finished
        await self._browser_queue.put({"type": "all_clusters_done"})
