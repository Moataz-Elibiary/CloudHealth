import asyncio
import json

import websockets
from fastapi import WebSocket


class WSProxy:
    """Proxy one cluster backend WebSocket to the browser UI with reconnect support."""

    HEARTBEAT_INTERVAL = 15
    RECONNECT_DELAY = 3
    MAX_RECONNECTS = 10

    async def proxy_cluster(self, ui_ws: WebSocket, local_port: int, cluster_name: str, config_payload: dict):
        uri = f"ws://127.0.0.1:{local_port}/ws"
        reconnects = 0
        started = False

        while reconnects <= self.MAX_RECONNECTS:
            try:
                async with websockets.connect(uri, ping_interval=None) as bastion_ws:
                    first_message = json.loads(await asyncio.wait_for(bastion_ws.recv(), timeout=10))
                    if first_message.get("type") == "error":
                        await ui_ws.send_json(
                            {
                                "type": "error",
                                "cluster": cluster_name,
                                "message": first_message.get("message", "Backend error"),
                            }
                        )
                        return None

                    heartbeat_task = asyncio.create_task(self._heartbeat_sender(bastion_ws))
                    try:
                        if first_message.get("running") or first_message.get("has_results") or started:
                            await bastion_ws.send(json.dumps({"action": "get_results"}))
                        else:
                            await bastion_ws.send(json.dumps({"action": "start_checks", "config": config_payload}))
                            started = True

                        while True:
                            raw_data = await bastion_ws.recv()
                            data = json.loads(raw_data)

                            message_type = data.get("type")
                            if message_type == "pong":
                                continue
                            if message_type in {"ready", "checks_started", "checks_in_progress"}:
                                continue

                            if message_type == "no_results":
                                if not started:
                                    await bastion_ws.send(json.dumps({"action": "start_checks", "config": config_payload}))
                                    started = True
                                    continue
                                break

                            if message_type == "all_done":
                                summary = data.get("summary") or data.get("results")
                                await ui_ws.send_json(
                                    {"type": "complete", "cluster": cluster_name, "summary": summary}
                                )
                                return summary

                            data["cluster"] = cluster_name
                            await ui_ws.send_json(data)

                            if message_type == "error":
                                return None
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass
            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError):
                reconnects += 1
                await asyncio.sleep(self.RECONNECT_DELAY)
                continue
            except Exception as exc:
                await ui_ws.send_json(
                    {
                        "type": "error",
                        "cluster": cluster_name,
                        "message": f"Proxy Error: {exc}",
                    }
                )
                return None

        await ui_ws.send_json(
            {
                "type": "error",
                "cluster": cluster_name,
                "message": "Lost connection to backend after maximum retries.",
            }
        )
        return None

    async def _heartbeat_sender(self, ws):
        try:
            while True:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                await ws.send(json.dumps({"action": "ping"}))
        except Exception:
            pass
