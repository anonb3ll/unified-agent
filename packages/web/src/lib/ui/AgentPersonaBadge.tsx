import React from "react";
import type { AgentPersona } from "../sessions/types";

export interface AgentPersonaBadgeProps extends Omit<React.HTMLAttributes<HTMLSpanElement>, "color"> {
  label: string;
  persona?: Partial<AgentPersona> | null;
  displayName?: string | null;
  avatar?: string | null;
  color?: string | null;
  size?: "sm" | "md";
  showLabel?: boolean;
}

// Persona stripes use the Catppuccin Latte accents that no state or action
// token uses (design protocol §3.1): mid-tone, so one hue reads on the dark
// and the light ground. Labels may share a hue; the stripe is never the only
// identity signal.
const FLEET_DEFAULT_PERSONAS: Record<string, { displayName: string; avatar: string; color: string }> = {
  Desktop_Agent5: { displayName: "CTO", avatar: "🪽", color: "#7287fd" },
  server_agent5: { displayName: "Chief of Staff", avatar: "🪽", color: "#7287fd" },
  Laptop_Agent5: { displayName: "Chief of Staff", avatar: "🪽", color: "#7287fd" },
  gcp_agent5: { displayName: "Chief of Staff", avatar: "🪽", color: "#7287fd" },
  Desktop_Agent1: { displayName: "Claude Code", avatar: "🧠", color: "#dd7878" },
  server_agent1: { displayName: "Claude Code", avatar: "🧠", color: "#dd7878" },
  Laptop_Agent1: { displayName: "Claude Code", avatar: "🧠", color: "#dd7878" },
  Desktop_Agent2: { displayName: "Codex", avatar: "⚡", color: "#04a5e5" },
  server_agent2: { displayName: "Codex", avatar: "⚡", color: "#04a5e5" },
  Laptop_Agent2: { displayName: "Codex", avatar: "⚡", color: "#04a5e5" },
  Desktop_agent3: { displayName: "Antigravity (Agy)", avatar: "🔮", color: "#ea76cb" },
  server_agent3: { displayName: "Antigravity (Agy)", avatar: "🔮", color: "#ea76cb" },
  Laptop_agent3: { displayName: "Antigravity (Agy)", avatar: "🔮", color: "#ea76cb" },
  Desktop_Agent4: { displayName: "Cursor", avatar: "🎯", color: "#209fb5" },
  server_agent4: { displayName: "Cursor", avatar: "🎯", color: "#209fb5" },
  Laptop_Agent4: { displayName: "Cursor", avatar: "🎯", color: "#209fb5" },
  Desktop_Agent7: { displayName: "Devin", avatar: "🦉", color: "#dc8a78" },
  Desktop_Agent6: { displayName: "Copilot", avatar: "✈️", color: "#179299" },
  Laptop_Agent6: { displayName: "Copilot", avatar: "✈️", color: "#179299" },
  Desktop_agent10: { displayName: "Omp", avatar: "🛠️", color: "#ea76cb" },
  Desktop_agent12: { displayName: "OpenCode", avatar: "🔭", color: "#179299" },
  gcp_agent20: { displayName: "Cloud Worker", avatar: "☁️", color: "#7c7f93" },
  gcp_agent18: { displayName: "Executive Assistant", avatar: "📋", color: "#dc8a78" },
  gcp_agent19: { displayName: "Librarian", avatar: "📚", color: "#209fb5" },
  server_agent8: { displayName: "Grok (Legacy)", avatar: "🤖", color: "#8c8fa1" },
};

const FALLBACK_PALETTE = [
  "#7c7f93",
  "#dd7878",
  "#dc8a78",
  "#179299",
  "#04a5e5",
  "#209fb5",
  "#7287fd",
  "#ea76cb",
  "#8c8fa1",
];

function fallbackColor(label: string): string {
  let hash = 0;
  for (let i = 0; i < label.length; i++) {
    hash = (hash << 5) - hash + label.charCodeAt(i);
    hash |= 0;
  }
  return FALLBACK_PALETTE[Math.abs(hash) % FALLBACK_PALETTE.length];
}

export const AgentPersonaBadge: React.FC<AgentPersonaBadgeProps> = ({
  label,
  persona,
  displayName,
  avatar,
  color,
  size = "sm",
  showLabel = true,
  className = "",
  ...rest
}) => {
  const fallback = FLEET_DEFAULT_PERSONAS[label];
  const resolvedDisplayName =
    displayName || persona?.display_name || fallback?.displayName || label;
  const resolvedAvatar = avatar || persona?.avatar || fallback?.avatar || "🤖";
  const resolvedColor =
    color || persona?.color || fallback?.color || fallbackColor(label);

  const sizeClasses = {
    sm: "text-xs px-2 py-0.5 gap-1.5",
    md: "text-sm px-2.5 py-1 gap-2",
  };

  return (
    <span
      className={`inline-flex items-center font-medium rounded-full border border-[var(--ua-border)] bg-[var(--ua-surface)] select-none ${sizeClasses[size]} ${className}`}
      style={{ borderLeftColor: resolvedColor, borderLeftWidth: "3px" }}
      title={`${resolvedDisplayName} (${label})`}
      {...rest}
    >
      <span className="text-sm leading-none" role="img" aria-label="avatar">
        {resolvedAvatar}
      </span>
      {showLabel && (
        <span className="text-[var(--ua-fg)] truncate max-w-[180px]">
          {resolvedDisplayName}
        </span>
      )}
    </span>
  );
};
