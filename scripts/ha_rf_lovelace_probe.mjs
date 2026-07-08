import { readFileSync } from "node:fs";

const token = readFileSync("/private/tmp/ha_token", "utf8").trim();

let nextId = 1;
const pending = new Map();

function request(ws, type, extra = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, type, ...extra }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

const ws = new WebSocket("ws://192.168.202.56:8123/api/websocket");
const timeout = setTimeout(() => {
  console.error("Timed out waiting for Home Assistant");
  ws.close();
  process.exitCode = 1;
}, 15000);

ws.addEventListener("message", async (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === "auth_required") {
    ws.send(JSON.stringify({ type: "auth", access_token: token }));
    return;
  }
  if (msg.type === "auth_ok") {
    try {
      const dashboards = await request(ws, "lovelace/dashboards/list");
      console.log(JSON.stringify(dashboards, null, 2));
      clearTimeout(timeout);
      ws.close();
    } catch (error) {
      console.error(error);
      clearTimeout(timeout);
      ws.close();
      process.exitCode = 1;
    }
    return;
  }
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id).resolve(msg);
    pending.delete(msg.id);
  }
});
