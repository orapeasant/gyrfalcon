/**
 * GatewayClient — spawns Python gateway subprocess, communicates via stdio JSON-RPC.
 */
import { spawn, ChildProcess } from "child_process";
import { createInterface, Interface } from "readline";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

type EventCallback = (params: any) => void;

export class GatewayClient {
  private process: ChildProcess | null = null;
  private readline: Interface | null = null;
  private requestId = 0;
  private pending = new Map<number, { resolve: Function; reject: Function }>();
  private listeners = new Map<string, EventCallback[]>();

  async connect(): Promise<void> {
    // Find Python gateway entry
    const __dirname = dirname(fileURLToPath(import.meta.url));
    const gatewayEntry = resolve(__dirname, "../../tui_gateway/entry.py");

    // Spawn Python process
    this.process = spawn("python", [gatewayEntry], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env },
    });

    if (!this.process.stdout || !this.process.stdin) {
      throw new Error("Failed to spawn gateway process");
    }

    // Read stdout line by line
    this.readline = createInterface({ input: this.process.stdout });
    this.readline.on("line", (line: string) => {
      this.handleLine(line);
    });

    this.process.stderr?.on("data", (data: Buffer) => {
      // Log stderr for debugging
      if (process.env.GYRFALCON_VERBOSE) {
        process.stderr.write(data);
      }
    });

    this.process.on("exit", (code) => {
      if (code !== 0) {
        console.error(`Gateway process exited with code ${code}`);
      }
    });

    // Wait for ready event
    return new Promise((resolve) => {
      this.on("gateway.ready", () => resolve());
      // Timeout fallback
      setTimeout(() => resolve(), 5000);
    });
  }

  disconnect(): void {
    if (this.process) {
      this.process.stdin?.end();
      this.process.kill("SIGTERM");
      this.process = null;
    }
  }

  async request<T = any>(method: string, params: object = {}): Promise<T> {
    const id = ++this.requestId;
    const msg = JSON.stringify({ jsonrpc: "2.0", id, method, params });

    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process?.stdin?.write(msg + "\n");

      // Timeout
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`Request ${method} timed out`));
        }
      }, 60000);
    });
  }

  on(event: string, callback: EventCallback): () => void {
    const list = this.listeners.get(event) || [];
    list.push(callback);
    this.listeners.set(event, list);
    return () => {
      const idx = list.indexOf(callback);
      if (idx >= 0) list.splice(idx, 1);
    };
  }

  private handleLine(line: string): void {
    try {
      const msg = JSON.parse(line);

      // Response to a request
      if ("id" in msg && msg.id !== null) {
        const pending = this.pending.get(msg.id);
        if (pending) {
          this.pending.delete(msg.id);
          if (msg.error) {
            pending.reject(new Error(msg.error.message));
          } else {
            pending.resolve(msg.result);
          }
        }
        return;
      }

      // Notification/event
      if ("method" in msg) {
        const callbacks = this.listeners.get(msg.method) || [];
        for (const cb of callbacks) {
          cb(msg.params || {});
        }
      }
    } catch {
      // Ignore malformed lines
    }
  }
}
