"""
Beta5 frontend/core/ws_proxy.py

Bug fixed vs Beta3: reconnect path now always sends get_results first
and only falls back to start_checks if the backend responds no_results.
This prevents the duplicate-run bug where a reconnect could fire a
second check run against a cluster that already completed.
"""
from __future__ import annotations
import asyncio, json
import websockets
from fastapi import WebSocket

HEARTBEAT_INTERVAL = 15
RECONNECT_DELAY    = 3
MAX_RECONNECTS     = 12


class WSProxy:
    """Proxy one cluster's backend WebSocket to the browser UI."""

    async def proxy_cluster(
        self,
        ui_ws:          WebSocket,
        local_port:     int,
        cluster_name:   str,
        config_payload: dict,
        on_backend_ws=None,
    ):
        """Proxy events from the bastion backend's WS to the UI WS.
        on_backend_ws(ws) is invoked once with the live websocket each time a
        connection is established, so the caller can register the handle for
        cancellation. It receives None when the connection drops."""
        uri        = f"ws://127.0.0.1:{local_port}/ws"
        reconnects = 0
        started    = False   # True once start_checks was sent this session

        while reconnects <= MAX_RECONNECTS:
            try:
                async with websockets.connect(
                    uri, ping_interval=None
                ) as backend_ws:
                    if on_backend_ws is not None:
                        try:
                            on_backend_ws(backend_ws)
                        except Exception:
                            pass

                    # ── Await ready handshake ─────────────────────────────────
                    raw  = await asyncio.wait_for(backend_ws.recv(), timeout=10)
                    first = json.loads(raw)

                    if first.get("type") == "error":
                        await ui_ws.send_json({
                            "type":    "error",
                            "cluster": cluster_name,
                            "message": first.get("message", "Backend error"),
                        })
                        return None

                    hb_task = asyncio.create_task(
                        self._heartbeat(backend_ws))
                    try:
                        # ── Fixed reconnect logic ─────────────────────────────
                        # Always ask for results first.  Only start fresh run
                        # if backend says it has nothing (no_results).
                        if first.get("running") or first.get("has_results"):
                            await backend_ws.send(
                                json.dumps({"action": "get_results"}))
                        elif started:
                            # We already sent start_checks; reconnected mid-run
                            await backend_ws.send(
                                json.dumps({"action": "get_results"}))
                        else:
                            await backend_ws.send(json.dumps({
                                "action": "start_checks",
                                "config": config_payload,
                            }))
                            started = True

                        # ── Message loop ──────────────────────────────────────
                        while True:
                            raw  = await backend_ws.recv()
                            data = json.loads(raw)
                            mtype = data.get("type")

                            if mtype == "pong":
                                continue
                            if mtype in {"ready", "checks_started",
                                         "checks_in_progress"}:
                                continue

                            # Backend has nothing — start fresh (once only)
                            if mtype == "no_results":
                                if not started:
                                    await backend_ws.send(json.dumps({
                                        "action": "start_checks",
                                        "config": config_payload,
                                    }))
                                    started = True
                                    continue
                                # Already started and got no_results — done
                                break

                            # Run complete
                            if mtype == "all_done":
                                summary          = data.get("summary") or data.get("results")
                                prev_checks      = data.get("prev_checks", [])
                                history_snapshot = data.get("history_snapshot", [])
                                await ui_ws.send_json({
                                    "type":             "complete",
                                    "cluster":          cluster_name,
                                    "summary":          summary,
                                    "prev_checks":      prev_checks,
                                    "history_snapshot": history_snapshot,
                                })
                                return {
                                    "summary":          summary,
                                    "prev_checks":      prev_checks,
                                    "history_snapshot": history_snapshot,
                                }

                            # Cancel acknowledged + final partial summary
                            if mtype == "cancelled":
                                summary          = data.get("summary")
                                prev_checks      = data.get("prev_checks", [])
                                history_snapshot = data.get("history_snapshot", [])
                                await ui_ws.send_json({
                                    "type":             "cancelled",
                                    "cluster":          cluster_name,
                                    "summary":          summary,
                                    "prev_checks":      prev_checks,
                                    "history_snapshot": history_snapshot,
                                })
                                return {
                                    "summary":          summary,
                                    "prev_checks":      prev_checks,
                                    "history_snapshot": history_snapshot,
                                }

                            if mtype == "cancelling":
                                await ui_ws.send_json({
                                    "type":    "cancelling",
                                    "cluster": cluster_name,
                                })
                                continue

                            # Forward all other messages (headline, result,
                            # check_result, section_start, section_done, error)
                            data["cluster"] = cluster_name
                            await ui_ws.send_json(data)

                            if mtype == "error":
                                return None

                    finally:
                        hb_task.cancel()
                        try:
                            await hb_task
                        except asyncio.CancelledError:
                            pass

            except (websockets.exceptions.ConnectionClosed,
                    ConnectionRefusedError, OSError):
                reconnects += 1
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            except Exception as exc:
                await ui_ws.send_json({
                    "type":    "error",
                    "cluster": cluster_name,
                    "message": f"Proxy error: {exc}",
                })
                return None

        await ui_ws.send_json({
            "type":    "error",
            "cluster": cluster_name,
            "message": "Lost connection to backend after maximum retries.",
        })
        return None

    async def _heartbeat(self, ws):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send(json.dumps({"action": "ping"}))
        except Exception:
            pass
