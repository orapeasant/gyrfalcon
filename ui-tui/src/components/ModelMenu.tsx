/**
 * ModelMenu — Two-stage provider → model selection overlay.
 *
 * Stage 1: pick a provider (↑↓ Enter)
 * Stage 2: pick a model   (↑↓ Enter, Esc = back)
 * Esc at provider stage cancels.
 *
 * Saves via model.set JSON-RPC call and emits onSaved with the new values.
 */
import React, { useState, useEffect, useCallback } from "react";
import { Box, Text, useInput } from "ink";
import type { GatewayClient } from "../gatewayClient.js";

// ── Types ──────────────────────────────────────────────────────────────────

interface Provider {
  name: string;
  display_name: string;
  authenticated: boolean;
}

type Stage = "providers" | "models" | "saving";

interface ModelMenuProps {
  client: GatewayClient;
  isOpen: boolean;
  currentProvider: string;
  currentModel: string;
  onClose: () => void;
  onSaved: (provider: string, model: string) => void;
}

// ── Helpers ────────────────────────────────────────────────────────────────

const VISIBLE = 10; // max rows shown at once in a list

function useScrollWindow(items: string[], selected: number) {
  const start = Math.max(0, Math.min(selected - Math.floor(VISIBLE / 2), items.length - VISIBLE));
  const slice = items.slice(start, start + VISIBLE);
  return { slice, start };
}

// ── Component ──────────────────────────────────────────────────────────────

export function ModelMenu({
  client,
  isOpen,
  currentProvider,
  currentModel,
  onClose,
  onSaved,
}: ModelMenuProps) {
  const [stage, setStage] = useState<Stage>("providers");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [providerIdx, setProviderIdx] = useState(0);
  const [modelIdx, setModelIdx] = useState(0);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");
  // The provider chosen in stage 1, used for stage 2 fetch
  const [pendingProvider, setPendingProvider] = useState(currentProvider);

  // Re-initialise every time the menu opens
  useEffect(() => {
    if (!isOpen) return;
    setStage("providers");
    setErrorMsg("");
    setLoading(true);

    client
      .request<{ providers: Provider[]; provider: string; model: string }>(
        "model.get_state"
      )
      .then((data) => {
        const list = data.providers || [];
        setProviders(list);
        const idx = list.findIndex((p) => p.name === currentProvider);
        setProviderIdx(idx >= 0 ? idx : 0);
        setPendingProvider(currentProvider);
        setLoading(false);
      })
      .catch((e) => {
        setErrorMsg(`Failed to load providers: ${e.message}`);
        setLoading(false);
      });
  }, [isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Keyboard ──────────────────────────────────────────────────────────────

  useInput(
    useCallback(
      (_input: string, key: any) => {
        if (loading) return;

        if (stage === "providers") {
          if (key.upArrow) {
            setProviderIdx((i) => Math.max(0, i - 1));
          } else if (key.downArrow) {
            setProviderIdx((i) => Math.min(providers.length - 1, i + 1));
          } else if (key.return) {
            const chosen = providers[providerIdx];
            if (!chosen) return;
            setPendingProvider(chosen.name);
            setLoading(true);
            setErrorMsg("");
            client
              .request<{ models: string[] }>("model.list_models", {
                provider: chosen.name,
              })
              .then((data) => {
                const mlist = data.models || [];
                setModels(mlist);
                const mIdx = mlist.findIndex((m) => m === currentModel);
                setModelIdx(mIdx >= 0 ? mIdx : 0);
                setStage("models");
                setLoading(false);
              })
              .catch((e) => {
                setErrorMsg(`Failed to load models: ${e.message}`);
                setLoading(false);
              });
          } else if (key.escape) {
            onClose();
          }
        } else if (stage === "models") {
          if (key.upArrow) {
            setModelIdx((i) => Math.max(0, i - 1));
          } else if (key.downArrow) {
            setModelIdx((i) => Math.min(models.length - 1, i + 1));
          } else if (key.return) {
            const model = models[modelIdx];
            if (!model) return;
            setStage("saving");
            setErrorMsg("");
            client
              .request<{ status: string }>("model.set", {
                provider: pendingProvider,
                model,
              })
              .then(() => {
                onSaved(pendingProvider, model);
                onClose();
              })
              .catch((e) => {
                setErrorMsg(`Save failed: ${e.message}`);
                setStage("models");
              });
          } else if (key.escape) {
            setStage("providers");
            setErrorMsg("");
          }
        }
      },
      [
        stage,
        loading,
        providers,
        models,
        providerIdx,
        modelIdx,
        pendingProvider,
        currentModel,
        client,
        onClose,
        onSaved,
      ]
    ),
    { isActive: isOpen }
  );

  if (!isOpen) return null;

  // ── Render helpers ────────────────────────────────────────────────────────

  function renderProviderList() {
    const { slice, start } = useScrollWindowLocal(
      providers.map((p) => p.display_name),
      providerIdx
    );
    return (
      <Box flexDirection="column">
        <Text bold color="#FF6012">
          Select Provider
        </Text>
        <Text color="gray" dimColor>
          ↑↓ navigate · Enter select · Esc cancel
        </Text>
        <Box marginTop={1} flexDirection="column">
          {slice.map((name, rel) => {
            const abs = start + rel;
            const p = providers[abs];
            const isSelected = abs === providerIdx;
            const isCurrent = p?.name === currentProvider;
            return (
              <Box key={abs}>
                <Text color={isSelected ? "#FF6012" : "white"} bold={isSelected}>
                  {isSelected ? "► " : "  "}
                </Text>
                <Text color={isSelected ? "#FF6012" : "white"} bold={isSelected}>
                  {name}
                </Text>
                {isCurrent && (
                  <Text color="gray" dimColor>
                    {" "}(current)
                  </Text>
                )}
                {p && !p.authenticated && (
                  <Text color="red"> ✗ not authenticated</Text>
                )}
                {p && p.authenticated && (
                  <Text color="green"> ✓</Text>
                )}
              </Box>
            );
          })}
        </Box>
        {providers.length > VISIBLE && (
          <Text color="gray" dimColor>
            {start + 1}–{start + slice.length} of {providers.length}
          </Text>
        )}
      </Box>
    );
  }

  function renderModelList() {
    const { slice, start } = useScrollWindowLocal(models, modelIdx);
    const chosenProvider =
      providers.find((p) => p.name === pendingProvider)?.display_name ||
      pendingProvider;
    return (
      <Box flexDirection="column">
        <Text bold color="#FF6012">
          Select Model
        </Text>
        <Text color="gray" dimColor>
          Provider: <Text color="cyan">{chosenProvider}</Text>{"  "}↑↓ navigate · Enter confirm · Esc back
        </Text>
        <Box marginTop={1} flexDirection="column">
          {slice.map((model, rel) => {
            const abs = start + rel;
            const isSelected = abs === modelIdx;
            const isCurrent =
              model === currentModel && pendingProvider === currentProvider;
            return (
              <Box key={abs}>
                <Text color={isSelected ? "#FF6012" : "white"} bold={isSelected}>
                  {isSelected ? "► " : "  "}
                </Text>
                <Text color={isSelected ? "#FF6012" : "white"} bold={isSelected}>
                  {model}
                </Text>
                {isCurrent && (
                  <Text color="gray" dimColor>
                    {" "}(current)
                  </Text>
                )}
              </Box>
            );
          })}
        </Box>
        {models.length > VISIBLE && (
          <Text color="gray" dimColor>
            {start + 1}–{start + slice.length} of {models.length}
          </Text>
        )}
      </Box>
    );
  }

  // ── Top-level render ──────────────────────────────────────────────────────

  return (
    <Box
      borderStyle="round"
      borderColor="#FF6012"
      flexDirection="column"
      paddingX={2}
      paddingY={1}
      minWidth={50}
    >
      {/* Header */}
      <Box marginBottom={1}>
        <Text bold color="#FF6012">
          ◆ Model Menu
        </Text>
        <Text color="gray" dimColor>
          {"  "}gyrfalcon
        </Text>
      </Box>

      {/* Stage indicator */}
      <Box marginBottom={1}>
        <Text color={stage === "providers" ? "#FF6012" : "gray"}>
          ① Provider
        </Text>
        <Text color="gray"> › </Text>
        <Text color={stage === "models" ? "#FF6012" : "gray"}>② Model</Text>
      </Box>

      {/* Content */}
      {loading && (
        <Box>
          <Text color="yellow">⠋ Loading...</Text>
        </Box>
      )}
      {stage === "saving" && (
        <Box>
          <Text color="yellow">⠋ Saving...</Text>
        </Box>
      )}
      {!loading && stage === "providers" && renderProviderList()}
      {!loading && stage === "models" && renderModelList()}

      {/* Error */}
      {errorMsg && (
        <Box marginTop={1}>
          <Text color="red">✗ {errorMsg}</Text>
        </Box>
      )}
    </Box>
  );
}

// Standalone helper (avoids calling hooks conditionally inside render helpers)
function useScrollWindowLocal(items: string[], selected: number) {
  const start = Math.max(
    0,
    Math.min(selected - Math.floor(VISIBLE / 2), items.length - VISIBLE)
  );
  const slice = items.slice(start, start + VISIBLE);
  return { slice, start };
}
