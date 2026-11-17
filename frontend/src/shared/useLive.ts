import { useEffect, useRef, useState } from "react";
import { getToken, type Live } from "../api/client";

/** Live machine states over WebSocket with automatic reconnect. */
export function useLive(): { machines: Record<number, Live>; connected: boolean } {
  const [machines, setMachines] = useState<Record<number, Live>>({});
  const [connected, setConnected] = useState(false);
  const closed = useRef(false);

  useEffect(() => {
    closed.current = false;
    let socket: WebSocket | null = null;
    let timer: number | undefined;

    const connect = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws/live?token=${encodeURIComponent(getToken() ?? "")}`);
      socket.onopen = () => setConnected(true);
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        if (message.type === "snapshot") {
          setMachines(Object.fromEntries((message.machines as Live[]).map((m) => [m.machine_id, m])));
        } else if (message.type === "update") {
          const machine = message.machine as Live;
          setMachines((previous) => ({ ...previous, [machine.machine_id]: machine }));
        }
      };
      socket.onclose = (event) => {
        setConnected(false);
        if (event.code === 4401) {
          window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
          return;
        }
        if (!closed.current) timer = window.setTimeout(connect, 2000);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      closed.current = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return { machines, connected };
}

/** Re-render every second (timers of state duration). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
