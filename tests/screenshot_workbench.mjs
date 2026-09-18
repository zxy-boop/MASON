// Open the MASON workbench in headless Chrome and save a screenshot (used to check the two-pane page visually).
// node screenshot_workbench.mjs <url> <out.png> [width] [height] [wait-seconds]
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
const [url, out, width = "1500", height = "950", wait = "8"] = process.argv.slice(2);
const CHROME = process.platform === "darwin" ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : "google-chrome";
const PORT = 9333;
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--lang=en-US", `--window-size=${width},${height}`,
  `--remote-debugging-port=${PORT}`, "--user-data-dir=/tmp/mason-shot-profile", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try {
  let pages = null;
  for (let i = 0; i < 40 && !pages; i++) { await sleep(250); try { pages = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json(); } catch {} }
  if (!pages) throw new Error("chrome devtools not reachable");
  const page = pages.find((p) => p.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl); await new Promise((r) => (ws.onopen = r));
  let id = 0; const pending = new Map();
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
  const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  await send("Page.enable"); await send("Page.navigate", { url }); await sleep(Number(wait) * 1000);
  const shot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(out, Buffer.from(shot.result.data, "base64")); console.log("saved", out);
  const txt = await send("Runtime.evaluate", { expression: "(function(){try{var d=document.getElementById('chat').contentDocument;return d?d.body.innerText.slice(0,600):'(no chat doc)'}catch(e){return 'x:'+e}})()", returnByValue: true });
  console.log("chat pane text:", JSON.stringify(txt.result.result.value).slice(0, 700));
} finally { chrome.kill(); }
