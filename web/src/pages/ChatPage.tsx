import React, { useState, useEffect, useRef, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { APP_NAME } from "../lib/constants";
import { Markdown } from "../components/Markdown";
import { SlashMenu, useSlashQuery } from "../components/SlashMenu";

interface ToolCall {
  name: string;
  detail: string;
  read_only: boolean;
}

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
  tools?: ToolCall[];
}

/** The one argument worth showing next to a tool name.
 *
 * The badge used to read "✏️ write · terminal", which says a command ran but
 * not which one. The backend already publishes `args` on `tool.start`; this
 * picks the argument that identifies the call and keeps it to one line. */
function summarizeToolArgs(name: string, args: any): string {
  if (!args || typeof args !== "object") return "";
  const pick = (...keys: string[]) => {
    for (const k of keys) {
      const v = args[k];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    return "";
  };
  let detail =
    pick("command", "file_path", "path", "query", "pattern", "url", "name", "prompt", "message");
  if (!detail) {
    // Unknown tool: fall back to the first string argument it was given.
    const first = Object.values(args).find(v => typeof v === "string" && v.trim());
    detail = typeof first === "string" ? first.trim() : "";
  }
  if (name === "search_files" && args.path) detail = `${detail} in ${args.path}`;
  detail = detail.replace(/\s+/g, " ");
  return detail.length > 160 ? `${detail.slice(0, 159)}…` : detail;
}

/** One tool call, rendered as a read/write pill plus its identifying argument. */
function ToolChip({ tool }: { tool: ToolCall }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "baseline", gap: "6px", maxWidth: "100%" }}>
      <span style={{
        display: "inline-flex", alignItems: "center", gap: "4px",
        padding: "2px 8px", borderRadius: "10px",
        fontSize: "0.78rem", fontWeight: 600, fontFamily: "monospace", flexShrink: 0,
        background: tool.read_only ? "rgba(34,197,94,0.1)" : "rgba(239,68,68,0.1)",
        border: `1px solid ${tool.read_only ? "rgba(34,197,94,0.3)" : "rgba(239,68,68,0.3)"}`,
        color: tool.read_only ? "#22c55e" : "#ef4444",
      }}>
        {tool.read_only ? "👁 read" : "✏️ write"}{" · "}{tool.name}
      </span>
      {tool.detail && (
        <code
          title={tool.detail}
          style={{
            fontSize: "0.76rem", fontFamily: "monospace",
            color: "var(--color-muted)", overflowWrap: "anywhere",
          }}
        >
          {tool.detail}
        </code>
      )}
    </span>
  );
}

interface LogEntry {
  timestamp: string;
  direction: "send" | "receive" | "info" | "error";
  method?: string;
  data?: any;
  message?: string;
}

type ConnectionState = "disconnected" | "connecting" | "connected" | "error";
type EngineState = "unknown" | "checking" | "stopped" | "starting" | "running";

declare global {
  interface Window {
    __GYRFALCON_SESSION_TOKEN__?: string;
    __GYRFALCON_CHAT_LOGS__?: LogEntry[];
  }
}

// Chat communication logger with callback support
class ChatLogger {
  private logs: LogEntry[] = [];
  private maxLogs = 500;
  private enabled = true;
  private onChange: (() => void) | null = null;

  constructor() {
    // Expose logs globally for debugging
    window.__GYRFALCON_CHAT_LOGS__ = this.logs;
  }

  setOnChange(callback: (() => void) | null) {
    this.onChange = callback;
  }

  private formatTimestamp(): string {
    return new Date().toISOString();
  }

  private addLog(entry: LogEntry) {
    if (!this.enabled) return;
    this.logs.push(entry);
    if (this.logs.length > this.maxLogs) {
      this.logs.shift();
    }
    // Also log to console with styling
    const style = entry.direction === "send" 
      ? "color: #22c55e; font-weight: bold"
      : entry.direction === "receive"
      ? "color: #3b82f6; font-weight: bold"
      : entry.direction === "error"
      ? "color: #ef4444; font-weight: bold"
      : "color: #888";
    
    const prefix = entry.direction === "send" ? "⬆️ SEND" 
      : entry.direction === "receive" ? "⬇️ RECV"
      : entry.direction === "error" ? "❌ ERROR"
      : "ℹ️ INFO";
    
    console.log(
      `%c[Chat ${prefix}] ${entry.timestamp}`,
      style,
      entry.method || entry.message || "",
      entry.data || ""
    );
    
    // Notify listener immediately
    this.onChange?.();
  }

  send(method: string, params: any) {
    this.addLog({
      timestamp: this.formatTimestamp(),
      direction: "send",
      method,
      data: params,
    });
  }

  receive(method: string, params: any) {
    this.addLog({
      timestamp: this.formatTimestamp(),
      direction: "receive",
      method,
      data: params,
    });
  }

  info(message: string, data?: any) {
    this.addLog({
      timestamp: this.formatTimestamp(),
      direction: "info",
      message,
      data,
    });
  }

  error(message: string, data?: any) {
    this.addLog({
      timestamp: this.formatTimestamp(),
      direction: "error",
      message,
      data,
    });
  }

  getLogs(): LogEntry[] {
    return [...this.logs];
  }

  clear() {
    this.logs.length = 0;
  }

  exportLogs(): string {
    return JSON.stringify(this.logs, null, 2);
  }

  setEnabled(enabled: boolean) {
    this.enabled = enabled;
  }
}

// Singleton logger instance
const chatLogger = new ChatLogger();

export function ChatPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [cursorPos, setCursorPos] = useState(0);
  const [slashMenuOpen, setSlashMenuOpen] = useState(false);
  const [connectionState, setConnectionState] = useState<ConnectionState>("disconnected");
  const [engineState, setEngineState] = useState<EngineState>("unknown");
  const [isThinking, setIsThinking] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [showLogs, setShowLogs] = useState(false);
  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const [resumedSessionId, setResumedSessionId] = useState<string | null>(null);
  const [resumedTitle, setResumedTitle] = useState<string>("");
  const [activeTool, setActiveTool] = useState<ToolCall | null>(null);
  // Tools used in the current turn. Kept in a ref as well as state because
  // message.complete has to read the final list from the same tick that the
  // last tool.complete was handled in.
  const turnToolsRef = useRef<ToolCall[]>([]);
  const [turnTools, setTurnTools] = useState<ToolCall[]>([]);
  const [streamingEnabled, setStreamingEnabled] = useState<boolean>(() => {
    try { return localStorage.getItem("gyrfalcon_streaming") !== "false"; } catch { return true; }
  });
  const pendingResumeRef = useRef<string | null>(searchParams.get("session"));
  
  const wsRef = useRef<WebSocket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const streamingContentRef = useRef<string>("");
  const streamFlushRef = useRef<number | null>(null);
  const reconnectCountRef = useRef(0);
  const handleMessageRef = useRef<(msg: any) => void>(() => {});

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, streamingContent, scrollToBottom]);

  // Update log entries - immediately on change + periodic sync
  useEffect(() => {
    // Register callback for immediate updates
    const updateLogs = () => setLogEntries(chatLogger.getLogs());
    chatLogger.setOnChange(updateLogs);
    
    // Immediately update when panel opens
    updateLogs();
    
    // Then update periodically as backup
    const interval = setInterval(updateLogs, showLogs ? 300 : 2000);
    
    return () => {
      chatLogger.setOnChange(null);
      clearInterval(interval);
    };
  }, [showLogs]);

  const checkEngineStatus = useCallback(async (): Promise<boolean> => {
    setEngineState("checking");
    chatLogger.info("Checking engine status...");
    try {
      const status = await api.getStatus();
      chatLogger.info("Engine status check successful", status);
      setEngineState("running");
      return true;
    } catch (e) {
      chatLogger.error("Engine status check failed", e);
      setEngineState("stopped");
      return false;
    }
  }, []);

  const startEngine = useCallback(async (): Promise<boolean> => {
    setEngineState("starting");
    setError(null);
    chatLogger.info("Starting engine...");
    try {
      const response = await fetch("/api/engine/start", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Gyrfalcon-Session-Token": window.__GYRFALCON_SESSION_TOKEN__ || "",
        },
      });
      
      if (!response.ok) {
        throw new Error("Failed to start engine");
      }
      
      chatLogger.info("Engine start request successful");
      await new Promise((r) => setTimeout(r, 1000));
      setEngineState("running");
      return true;
    } catch (e) {
      chatLogger.info("Engine already running (start endpoint not needed)");
      setEngineState("running");
      return true;
    }
  }, []);

  const connect = useCallback(async () => {
    // Check if already connected or connecting
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      chatLogger.info("Already connected, skipping reconnect");
      return;
    }
    if (wsRef.current?.readyState === WebSocket.CONNECTING) {
      chatLogger.info("Connection already in progress, skipping");
      return;
    }

    reconnectCountRef.current += 1;
    const currentAttempt = reconnectCountRef.current;
    setConnectionState("connecting");
    setError(null);
    chatLogger.info("Initiating WebSocket connection...", { attempt: currentAttempt });

    // First check if engine is available
    const engineRunning = await checkEngineStatus();
    if (!engineRunning) {
      const started = await startEngine();
      if (!started) {
        setConnectionState("error");
        setError(`Could not start ${APP_NAME} engine`);
        chatLogger.error(`Could not start ${APP_NAME} engine`);
        return;
      }
    }

    // Build WebSocket URL
    const token = window.__GYRFALCON_SESSION_TOKEN__ || "";
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/api/ws?token=${token}`;
    chatLogger.info("Creating WebSocket", { url: wsUrl.replace(token, "***"), protocol });

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        chatLogger.info("WebSocket OPEN - connection established successfully");
        setConnectionState("connected");
        setError(null);
        reconnectCountRef.current = 0;

        // Resume session if navigated from Sessions page
        const sid = pendingResumeRef.current;
        if (sid) {
          pendingResumeRef.current = null;
          const resumeReq = {
            jsonrpc: "2.0", id: Date.now(),
            method: "session.resume",
            params: { session_id: sid },
          };
          chatLogger.info("Sending session.resume", { session_id: sid });
          ws.send(JSON.stringify(resumeReq));
        }
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          chatLogger.receive(msg.method || "response", msg.params || msg);

          // Handle session.resume response (JSON-RPC result, not a notification)
          if ("id" in msg && msg.result?.messages) {
            const history: Message[] = (msg.result.messages as any[])
              .filter(m => m.role === "user" || m.role === "assistant")
              .filter(m => m.content)
              .map((m, i) => ({
                id: `hist-${i}`,
                role: m.role as "user" | "assistant",
                content: m.content,
                timestamp: Date.now() - (msg.result.messages.length - i) * 1000,
              }));
            setMessages(history);
            const title = msg.result.session?.title || "";
            setResumedSessionId(msg.result.session?.id || null);
            setResumedTitle(title);
            // Clear the ?session= param from URL so refresh starts fresh
            setSearchParams({});
            chatLogger.info(`Session resumed: ${history.length} messages loaded`, { title });
            return;
          }

          handleMessageRef.current(msg);
        } catch (e) {
          chatLogger.error("Failed to parse message", { raw: event.data, error: String(e) });
        }
      };

      ws.onerror = (event) => {
        chatLogger.error("WebSocket ERROR event fired", { 
          readyState: ws.readyState,
          type: event.type,
        });
        setConnectionState("error");
        setError("Connection error");
      };

      ws.onclose = (event) => {
        chatLogger.info("WebSocket CLOSE event", { 
          code: event.code, 
          reason: event.reason || "(no reason)",
          wasClean: event.wasClean,
          readyState: ws.readyState,
        });
        setConnectionState("disconnected");
        wsRef.current = null;
        
        // Handle specific close codes
        if (event.code === 1000) {
          chatLogger.info("Clean close (code 1000), not reconnecting");
          return;
        }
        
        if (event.code === 4001) {
          chatLogger.error("Authentication failed (code 4001) - session token expired, auto-refreshing page...");
          setError("Session expired - refreshing page...");
          // Auto-refresh to get new token
          setTimeout(() => {
            window.location.reload();
          }, 1000);
          return;
        }
        
        // Auto-reconnect for other close codes
        const currentCount = reconnectCountRef.current;
        const delay = Math.min(3000 * Math.pow(1.5, currentCount), 30000); // Exponential backoff, max 30s
        chatLogger.info(`Scheduling reconnect in ${Math.round(delay/1000)}s...`, { 
          reconnectCount: currentCount,
          nextDelay: delay,
        });
        reconnectTimeoutRef.current = setTimeout(() => {
          chatLogger.info("Reconnect timer fired, attempting connection...");
          connect();
        }, delay);
      };
      
      chatLogger.info("WebSocket object created, waiting for connection...", { readyState: ws.readyState });
    } catch (e) {
      chatLogger.error("Failed to create WebSocket", { error: String(e) });
      setConnectionState("error");
      setError(`Failed to connect: ${e}`);
    }
  }, [checkEngineStatus, startEngine]);

  const handleServerMessage = useCallback((msg: any) => {
    const method = msg.method;
    const params = msg.params || {};

    switch (method) {
      case "gateway.ready":
        // Gateway is ready - streaming is enabled by default
        chatLogger.info("Gateway ready", params);
        break;
        
      case "message.delta":
        // Streaming content delta - always buffer; only render if streaming is enabled.
        // Painting per token re-parses the whole message each time (O(n^2)), so
        // coalesce onto one animation frame and paint at most once per frame.
        streamingContentRef.current += params.content;
        if (streamingEnabled) {
          setIsStreaming(true);
          if (streamFlushRef.current === null) {
            streamFlushRef.current = requestAnimationFrame(() => {
              streamFlushRef.current = null;
              setStreamingContent(streamingContentRef.current);
            });
          }
        }
        break;
        
      case "message.complete":
        // Message completed - use accumulated streaming content or fallback to params.content
        if (streamFlushRef.current !== null) {
          cancelAnimationFrame(streamFlushRef.current);
          streamFlushRef.current = null;
        }
        setIsThinking(false);
        setIsStreaming(false);
        const finalContent = streamingContentRef.current || params.content || "";
        chatLogger.info("Message complete", { responseLength: finalContent.length });
        // React defers this updater callback to flush time, by which point
        // the ref reset below has already run — read it into a local first,
        // or the message is pushed with an already-emptied tools array.
        const completedTools = turnToolsRef.current;
        setMessages((prev) => [
          ...prev,
          {
            id: `msg-${Date.now()}`,
            role: "assistant",
            content: finalContent,
            timestamp: Date.now(),
            // Keep the turn's tool calls with the reply they produced, so the
            // transcript still shows what ran after the live badge is gone.
            tools: completedTools.length ? completedTools : undefined,
          },
        ]);
        turnToolsRef.current = [];
        setTurnTools([]);
        setActiveTool(null);
        streamingContentRef.current = "";
        setStreamingContent("");
        break;
        
      case "status.update":
        if (params.state === "thinking") {
          setIsThinking(true);
        } else if (params.state === "idle") {
          setIsThinking(false);
          setIsStreaming(false);
        }
        break;
        
      case "tool.start": {
        const call: ToolCall = {
          name: params.name || "",
          detail: summarizeToolArgs(params.name || "", params.args),
          read_only: !!params.read_only,
        };
        setActiveTool(call);
        turnToolsRef.current = [...turnToolsRef.current, call];
        setTurnTools(turnToolsRef.current);
        break;
      }

      case "tool.complete":
        setActiveTool(null);
        break;
        
      case "error":
        chatLogger.error("Agent error", params);
        setError(params.message || "Unknown error");
        setIsThinking(false);
        setIsStreaming(false);
        break;
    }
  }, [streamingEnabled]);

  // Keep the ref updated with the latest handler
  useEffect(() => {
    handleMessageRef.current = handleServerMessage;
  }, [handleServerMessage]);

  const sendMessage = useCallback(() => {
    if (!input.trim() || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      chatLogger.info("Cannot send - connection not ready", { 
        hasInput: !!input.trim(),
        wsExists: !!wsRef.current,
        readyState: wsRef.current?.readyState,
      });
      return;
    }

    const userMessage: Message = {
      id: `msg-${Date.now()}`,
      role: "user",
      content: input.trim(),
      timestamp: Date.now(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setIsThinking(true);
    setIsStreaming(false);
    turnToolsRef.current = [];
    setTurnTools([]);
    setActiveTool(null);
    streamingContentRef.current = "";
    setStreamingContent("");

    // Send JSON-RPC request with streaming enabled
    const request = {
      jsonrpc: "2.0",
      id: Date.now(),
      method: "prompt.submit",
      params: { message: userMessage.content, stream: true },
    };

    chatLogger.send("prompt.submit", { messageLength: userMessage.content.length, stream: true });
    wsRef.current.send(JSON.stringify(request));
  }, [input]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Delegate to slash menu first when active
    if (slashMenuOpen && slashMenuKeyHandler.current) {
      const handled = slashMenuKeyHandler.current(e);
      if (handled) return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }, [sendMessage, slashMenuOpen]);

  const exportLogs = useCallback(() => {
    const logsJson = chatLogger.exportLogs();
    const blob = new Blob([logsJson], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `chat-logs-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, []);

  const clearLogs = useCallback(() => {
    chatLogger.clear();
    setLogEntries([]);
  }, []);

  // Connect on mount
  useEffect(() => {
    chatLogger.info("ChatPage mounted, initiating connection");
    connect();
    
    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close(1000);
      }
    };
  }, [connect]);

  const getStatusColor = () => {
    if (isStreaming) return "var(--fg)";
    switch (connectionState) {
      case "connected": return "var(--color-success, #22c55e)";
      case "connecting": return "var(--color-warning, #f59e0b)";
      case "error": return "var(--color-danger, #ef4444)";
      default: return "var(--color-muted)";
    }
  };

  const getStatusText = () => {
    if (isStreaming) return "Streaming...";
    if (isThinking) return "Thinking...";
    if (engineState === "starting") return "Starting engine...";
    if (engineState === "checking") return "Checking engine...";
    switch (connectionState) {
      case "connected": return streamingEnabled ? "Connected • Streaming on" : "Connected • Streaming off";
      case "connecting": return "Connecting...";
      case "error": return "Connection error";
      default: return "Disconnected";
    }
  };

  // ── Slash command menu ───────────────────────────────────────────────────────
  const slashMenuKeyHandler = useRef<((e: React.KeyboardEvent) => boolean) | null>(null);
  const inputAreaRef = useRef<HTMLDivElement>(null);

  const { active: slashActive, query: slashQuery, slashStart } = useSlashQuery(input, cursorPos);

  useEffect(() => {
    setSlashMenuOpen(slashActive);
  }, [slashActive]);

  function handleInputChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    setCursorPos(e.target.selectionStart ?? e.target.value.length);
  }

  function handleInputSelect(e: React.SyntheticEvent<HTMLTextAreaElement>) {
    setCursorPos((e.target as HTMLTextAreaElement).selectionStart ?? 0);
  }

  function handleSlashSelect(value: string) {
    const before = input.slice(0, slashStart);
    const after   = input.slice(cursorPos);
    const newInput = before + value + " " + after;
    setInput(newInput);
    setSlashMenuOpen(false);
    setTimeout(() => {
      if (inputRef.current) {
        const pos = (before + value + " ").length;
        inputRef.current.focus();
        inputRef.current.setSelectionRange(pos, pos);
        setCursorPos(pos);
      }
    }, 0);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      {/* Resumed session banner */}
      {resumedSessionId && (
        <div style={{ display:"flex", alignItems:"center", gap:"8px", padding:"7px 14px", background:"rgba(69,109,230,0.12)", border:"1px solid rgba(69,109,230,0.3)", borderRadius:"6px", marginBottom:"8px", fontSize:"12px", color:"#456DE6" }}>
          <span>↩ Resumed session <code style={{ fontFamily:"monospace", background:"rgba(69,109,230,0.15)", padding:"1px 5px", borderRadius:"3px" }}>{resumedSessionId.slice(0,8)}</code>
          {resumedTitle && <span> — <strong>{resumedTitle}</strong></span>}
          </span>
          <button onClick={() => { setMessages([]); setResumedSessionId(null); setResumedTitle(""); }} style={{ marginLeft:"auto", background:"transparent", border:"none", color:"#456DE6", cursor:"pointer", fontSize:"11px", padding:"0" }}>✕ New session</button>
        </div>
      )}
      {/* Header */}
      <div style={{ 
        display: "flex", 
        justifyContent: "space-between", 
        alignItems: "center",
        marginBottom: "1rem",
        paddingBottom: "0.5rem",
        borderBottom: "1px solid var(--color-border)",
      }}>
        <h2 style={{ color: "var(--fg)", margin: 0 }}>Chat</h2>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <span style={{
            width: "8px",
            height: "8px",
            borderRadius: "50%",
            background: getStatusColor(),
          }} />
          <span style={{ color: "var(--color-muted)", fontSize: "0.85rem" }}>
            {getStatusText()}
          </span>
          {connectionState !== "connected" && connectionState !== "connecting" && (
            <button
              onClick={() => connect()}
              style={{
                padding: "0.25rem 0.5rem",
                background: "var(--btn-bg)",
                color: "var(--btn-fg)",
                border: "none",
                borderRadius: "4px",
                cursor: "pointer",
                fontSize: "0.8rem",
              }}
            >
              Reconnect
            </button>
          )}
          <button
            onClick={() => {
              const next = !streamingEnabled;
              setStreamingEnabled(next);
              try { localStorage.setItem("gyrfalcon_streaming", String(next)); } catch {}
            }}
            title={streamingEnabled ? "Disable streaming (show response all at once)" : "Enable streaming (show response as it arrives)"}
            style={{
              padding: "0.25rem 0.5rem",
              background: streamingEnabled ? "var(--btn-bg)" : "var(--color-midground)",
              color: streamingEnabled ? "var(--btn-fg)" : "var(--color-foreground)",
              border: "1px solid var(--color-border)",
              borderRadius: "4px",
              cursor: "pointer",
              fontSize: "0.8rem",
            }}
          >
            {streamingEnabled ? "⚡ Stream On" : "⏸ Stream Off"}
          </button>
          <button
            onClick={() => setShowLogs(!showLogs)}
            style={{
              padding: "0.25rem 0.5rem",
              background: showLogs ? "var(--btn-bg)" : "var(--color-midground)",
              color: showLogs ? "var(--btn-fg)" : "var(--color-foreground)",
              border: "1px solid var(--color-border)",
              borderRadius: "4px",
              cursor: "pointer",
              fontSize: "0.8rem",
            }}
          >
            {showLogs ? "Hide Logs" : "Show Logs"}
          </button>
        </div>
      </div>

      {/* Logs panel */}
      {showLogs && (
        <div style={{
          background: "var(--color-midground)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "0.75rem",
          marginBottom: "1rem",
          maxHeight: "200px",
          overflow: "auto",
        }}>
          <div style={{ 
            display: "flex", 
            justifyContent: "space-between", 
            alignItems: "center",
            marginBottom: "0.5rem",
            paddingBottom: "0.5rem",
            borderBottom: "1px solid var(--color-border)",
          }}>
            <span style={{ fontWeight: 500, fontSize: "0.85rem" }}>
              Communication Logs ({logEntries.length})
            </span>
            <div style={{ display: "flex", gap: "0.5rem" }}>
              <button
                onClick={exportLogs}
                style={{
                  padding: "0.2rem 0.4rem",
                  background: "var(--color-background)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "3px",
                  cursor: "pointer",
                  fontSize: "0.75rem",
                }}
              >
                Export
              </button>
              <button
                onClick={clearLogs}
                style={{
                  padding: "0.2rem 0.4rem",
                  background: "var(--color-background)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "3px",
                  cursor: "pointer",
                  fontSize: "0.75rem",
                }}
              >
                Clear
              </button>
            </div>
          </div>
          <div style={{ fontFamily: "monospace", fontSize: "0.75rem" }}>
            {logEntries.length === 0 ? (
              <div style={{ color: "var(--color-muted)", textAlign: "center", padding: "1rem" }}>
                No logs yet. Communication will be logged here.
              </div>
            ) : (
              logEntries.slice(-50).map((entry, i) => (
                <div 
                  key={i} 
                  style={{ 
                    padding: "0.2rem 0",
                    borderBottom: "1px solid var(--color-border)",
                    color: entry.direction === "send" 
                      ? "#22c55e" 
                      : entry.direction === "receive" 
                      ? "#3b82f6"
                      : entry.direction === "error"
                      ? "#ef4444"
                      : "var(--color-muted)",
                  }}
                >
                  <span style={{ opacity: 0.7 }}>
                    {entry.timestamp.split("T")[1]?.slice(0, 12)}
                  </span>
                  {" "}
                  <span style={{ fontWeight: 500 }}>
                    {entry.direction === "send" ? "⬆️" : entry.direction === "receive" ? "⬇️" : entry.direction === "error" ? "❌" : "ℹ️"}
                    {" "}
                    {entry.method || entry.message}
                  </span>
                  {entry.data && (
                    <span style={{ opacity: 0.7, marginLeft: "0.5rem" }}>
                      {JSON.stringify(entry.data).slice(0, 80)}
                      {JSON.stringify(entry.data).length > 80 ? "..." : ""}
                    </span>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div style={{
          background: "rgba(239, 68, 68, 0.1)",
          border: "1px solid var(--color-danger, #ef4444)",
          borderRadius: "4px",
          padding: "0.5rem 1rem",
          marginBottom: "1rem",
          color: "var(--color-danger, #ef4444)",
          fontSize: "0.9rem",
        }}>
          {error}
        </div>
      )}

      {/* Messages area */}
      <div style={{
        flex: 1,
        overflow: "auto",
        background: "var(--color-background)",
        border: "1px solid var(--color-border)",
        borderRadius: "8px",
        padding: "1rem",
        marginBottom: "1rem",
      }}>
        {messages.length === 0 && !isThinking && (
          <div style={{ 
            textAlign: "center", 
            color: "var(--color-muted)",
            padding: "2rem",
          }}>
            <p>Start a conversation with {APP_NAME}</p>
            <p style={{ fontSize: "0.85rem" }}>
              Type a message below to begin
            </p>
          </div>
        )}

        {messages.map((msg) => {
          // ── Slash invocation display ─────────────────────────────────────
          // Don't show the raw /skill, /mcp, /tool, /toolset text.
          // Render a compact invocation badge instead.
          const slashMatch = msg.role === "user"
            ? msg.content.match(/^\/(\w+)\s+(\S+)([\s\S]*)$/)
            : null;
          const isSlashCmd = slashMatch && ["skill","mcp","tool","toolset"].includes(slashMatch[1]);

          const SLASH_ICONS: Record<string, string> = {
            skill: "⚡", mcp: "🖥", tool: "🔧", toolset: "🗂",
          };

          return (
          <div
            key={msg.id}
            style={{
              display: "flex",
              justifyContent: msg.role === "user" ? "flex-end" : "flex-start",
              marginBottom: "1rem",
            }}
          >
            {isSlashCmd ? (
              /* Slash invocation pill */
              <div style={{
                display: "inline-flex", alignItems: "center", gap: "6px",
                padding: "4px 12px 4px 8px",
                background: "var(--sidebar-active)",
                border: "1px solid var(--border)",
                borderRadius: "20px",
                fontSize: "0.82rem",
                color: "var(--fg-muted)",
              }}>
                <span style={{ fontSize: "0.95rem" }}>
                  {SLASH_ICONS[slashMatch![1]] ?? "▸"}
                </span>
                <span style={{ fontWeight: 600, color: "var(--fg)", fontFamily: "monospace" }}>
                  {slashMatch![1]}
                </span>
                <span style={{ color: "var(--fg)", fontFamily: "monospace" }}>
                  {slashMatch![2]}
                </span>
                {slashMatch![3].trim() && (
                  <span style={{ color: "var(--fg-muted)" }}>
                    — {slashMatch![3].trim()}
                  </span>
                )}
              </div>
            ) : (
            <div style={{
              maxWidth: "75%",
              padding: "0.75rem 1rem",
              background: msg.role === "user"
                ? "transparent"
                : "var(--color-midground)",
              color: msg.role === "user" ? "var(--fg)" : "var(--color-foreground)",
              borderRadius: msg.role === "user" 
                ? "18px 18px 4px 18px"
                : "18px 18px 18px 4px",
              border: msg.role === "user" ? "none" : "1px solid var(--color-border)",
            }}>
              <div style={{ 
                fontSize: "0.7rem", 
                color: "var(--color-muted)",
                marginBottom: "0.25rem",
                textAlign: msg.role === "user" ? "right" : "left",
              }}>
                {msg.role === "user" ? "You" : APP_NAME}
              </div>
              {msg.tools && msg.tools.length > 0 && (
                <div style={{
                  display: "flex", flexDirection: "column", gap: "4px",
                  marginBottom: "0.5rem",
                }}>
                  {msg.tools.map((t, i) => (
                    <ToolChip key={`${msg.id}-${t.name}-${i}`} tool={t} />
                  ))}
                </div>
              )}
              {msg.role === "user" ? (
                <div style={{ whiteSpace: "pre-wrap" }}>{msg.content}</div>
              ) : (
                <Markdown content={msg.content} />
              )}
            </div>
            )}
          </div>
          );
        })}

        {/* Streaming response */}
        {(isThinking || streamingContent) && (
          <div style={{
            display: "flex",
            justifyContent: "flex-start",
            marginBottom: "1rem",
          }}>
            <div style={{
              maxWidth: "75%",
              padding: "0.75rem 1rem",
              background: "var(--color-midground)",
              borderRadius: "18px 18px 18px 4px",
              border: "1px solid var(--color-border)",
            }}>
              <div style={{ 
                fontSize: "0.7rem", 
                color: "var(--color-muted)",
                marginBottom: "0.25rem",
              }}>
                {APP_NAME}
              </div>
              {/* Tools stay visible while the reply streams — previously the
                  badge was replaced by the first token of text. */}
              {turnTools.length > 0 && (
                <div style={{
                  display: "flex", flexDirection: "column", gap: "4px",
                  marginBottom: streamingContent ? "0.5rem" : 0,
                }}>
                  {turnTools.map((t, i) => (
                    <ToolChip key={`${t.name}-${i}`} tool={t} />
                  ))}
                </div>
              )}
              {streamingContent ? (
                <Markdown content={streamingContent} />
              ) : (
                !activeTool && turnTools.length === 0 && (
                  <span style={{ color: "var(--color-muted)", fontSize: "0.9rem" }}>
                    Thinking…
                  </span>
                )
              )}
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div ref={inputAreaRef} style={{ display: "flex", gap: "0.5rem", position: "relative" }}>
        {/* Slash menu anchored above the input */}
        {slashMenuOpen && (
          <SlashMenu
            query={slashQuery}
            onSelect={handleSlashSelect}
            onClose={() => setSlashMenuOpen(false)}
            anchorRef={inputAreaRef}
            keyHandlerRef={slashMenuKeyHandler}
          />
        )}
        <textarea
          ref={inputRef}
          value={input}
          onChange={handleInputChange}
          onSelect={handleInputSelect}
          onClick={handleInputSelect as any}
          onKeyDown={handleKeyDown}
          placeholder={connectionState === "connected" ? 'Type a message… or "/" for skills & tools' : "Connecting..."}
          disabled={connectionState !== "connected" || isThinking}
          style={{
            flex: 1,
            padding: "0.75rem",
            background: "var(--color-midground)",
            border: "1px solid var(--color-border)",
            borderRadius: "8px",
            color: "var(--color-foreground)",
            resize: "none",
            minHeight: "44px",
            maxHeight: "120px",
            fontFamily: "inherit",
            fontSize: "0.95rem",
          }}
          rows={1}
        />
        <button
          onClick={sendMessage}
          disabled={connectionState !== "connected" || isThinking || !input.trim()}
          style={{
            padding: "0 1.5rem",
            // Must track the theme: --fg is near-white in dark mode, so a
            // hardcoded white label rendered invisible against it.
            background: connectionState === "connected" && !isThinking && input.trim()
              ? "var(--btn-bg)"
              : "var(--btn-bg-disabled)",
            color: connectionState === "connected" && !isThinking && input.trim()
              ? "var(--btn-fg)"
              : "var(--btn-fg-disabled)",
            border: "none",
            borderRadius: "8px",
            cursor: connectionState === "connected" && !isThinking && input.trim()
              ? "pointer"
              : "not-allowed",
            fontWeight: 500,
          }}
        >
          Send
        </button>
      </div>
    </div>
  );
}
