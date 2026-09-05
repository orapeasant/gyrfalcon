/**
 * Root App component — GatewayProvider + AppLayout.
 */
import React, { useState, useEffect, useCallback, useRef } from "react";
import { Box, Text, useInput } from "ink";
import type { GatewayClient } from "./gatewayClient.js";
import { ModelMenu } from "./components/ModelMenu.js";

interface AppProps {
  client: GatewayClient;
}

export function App({ client }: AppProps) {
  const [ready, setReady] = useState(false);
  const [messages, setMessages] = useState<Array<{ role: string; content: string; thinking?: { text: string; elapsed: number } }>>([]);
  const [busy, setBusy] = useState(false);
  const [currentResponse, setCurrentResponse] = useState("");
  const [currentThinking, setCurrentThinking] = useState<{ text: string; elapsed: number } | null>(null);
  const currentThinkingRef = useRef<{ text: string; elapsed: number } | null>(null);
  const [showReasoning, setShowReasoning] = useState(true);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [currentProvider, setCurrentProvider] = useState("copilot");
  const [currentModel, setCurrentModel] = useState("gpt-4o");

  useEffect(() => {
    client.on("gateway.ready", () => {
      setReady(true);
      // Fetch initial provider/model state
      client
        .request<{ provider: string; model: string }>("model.get_state")
        .then((data) => {
          if (data.provider) setCurrentProvider(data.provider);
          if (data.model) setCurrentModel(data.model);
        })
        .catch(() => {/* non-fatal */});
    });

    client.on("message.delta", (params: any) => {
      setCurrentResponse((prev) => prev + (params.content || ""));
    });
    client.on("thinking.block", (params: any) => {
      const t = { text: params.content || "", elapsed: params.elapsed || 0 };
      currentThinkingRef.current = t;
      setCurrentThinking(t);
    });
    client.on("message.complete", (params: any) => {
      const thinking = currentThinkingRef.current ?? undefined;
      setMessages((prev) => [...prev, { role: "assistant", content: params.content, thinking }]);
      currentThinkingRef.current = null;
      setCurrentThinking(null);
      setCurrentResponse("");
      setBusy(false);
    });
    client.on("status.update", (params: any) => {
      setBusy(params.state === "thinking");
    });
    client.on("tool.start", (_params: any) => {
      // Tool activity indicator — future use
    });
    // Backend pushes model.changed when model.set succeeds
    client.on("model.changed", (params: any) => {
      if (params.provider) setCurrentProvider(params.provider);
      if (params.model) setCurrentModel(params.model);
    });
  }, [client]);

  // Open model menu with 'm' — only when not busy and menu is closed
  useInput(
    useCallback(
      (input: string, _key: any) => {
        if (input === "m") {
          setModelMenuOpen(true);
        }
        if (input === "r") {
          setShowReasoning((prev) => !prev);
        }
      },
      []
    ),
    { isActive: ready && !busy && !modelMenuOpen }
  );

  if (!ready) {
    return (
      <Box>
        <Text color="yellow">Connecting to Gyrfalcon...</Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column" height="100%">
      {/* Banner */}
      <Box borderStyle="single" borderColor="yellow" paddingX={1}>
        <Text bold color="yellow">
          Gyrfalcon
        </Text>
        <Text color="gray"> — AI Agent TUI</Text>
      </Box>

      {/* Model Menu Overlay */}
      {modelMenuOpen && (
        <Box marginX={2} marginY={1}>
          <ModelMenu
            client={client}
            isOpen={modelMenuOpen}
            currentProvider={currentProvider}
            currentModel={currentModel}
            onClose={() => setModelMenuOpen(false)}
            onSaved={(provider, model) => {
              setCurrentProvider(provider);
              setCurrentModel(model);
            }}
          />
        </Box>
      )}

      {/* Transcript — hidden while model menu is open */}
      {!modelMenuOpen && (
        <Box flexDirection="column" flexGrow={1} paddingX={1}>
          {messages.map((msg, i) => (
            <Box key={i} marginY={0} flexDirection="column">
              {/* Thinking block — shown only for assistant messages that have one */}
              {msg.role === "assistant" && msg.thinking && showReasoning && (
                <Box flexDirection="column" marginBottom={0}>
                  <Text color="gray" dimColor italic>
                    {`✓ Thought for ${msg.thinking.elapsed}s`}
                  </Text>
                  {msg.thinking.text.split("\n").map((line, li) => (
                    <Text key={li} color="gray" dimColor italic>
                      {"  "}{line}
                    </Text>
                  ))}
                </Box>
              )}
              <Text color={msg.role === "user" ? "cyan" : "white"}>
                {msg.role === "user" ? "❯ " : "◆ "}
                {msg.content}
              </Text>
            </Box>
          ))}
          {/* In-progress thinking block */}
          {currentThinking && showReasoning && (
            <Box flexDirection="column">
              <Text color="gray" dimColor italic>
                {`✓ Thought for ${currentThinking.elapsed}s`}
              </Text>
              {currentThinking.text.split("\n").slice(0, 8).map((line, li) => (
                <Text key={li} color="gray" dimColor italic>
                  {"  "}{line}
                </Text>
              ))}
            </Box>
          )}
          {currentResponse && (
            <Box>
              <Text color="white">◆ {currentResponse}</Text>
            </Box>
          )}
          {busy && !currentResponse && !currentThinking && (
            <Box>
              <Text color="gray">⠋ thinking...</Text>
            </Box>
          )}
        </Box>
      )}

      {/* Status bar */}
      <Box borderStyle="single" borderColor="gray" paddingX={1} justifyContent="space-between">
        <Text color="gray">
          {busy ? "Processing..." : "Ready"} | /help for commands
        </Text>
        <Text>
          <Text color="gray"> | </Text>
          <Text color="#FF6012" bold>
            {currentProvider}
          </Text>
          <Text color="gray"> › </Text>
          <Text color="cyan">{currentModel}</Text>
          <Text color="gray" dimColor>
            {"  "}[m] change
          </Text>
          <Text color="gray" dimColor>
            {"  "}[r] reasoning:{showReasoning ? "on" : "off"}
          </Text>
        </Text>
      </Box>
    </Box>
  );
}
