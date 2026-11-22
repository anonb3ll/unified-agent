/**
 * Unified Agent Extension for Pi (pi.dev)
 *
 * Connects Pi coding agent sessions to unified-agent memory, task board, and workspace context.
 * Enriches system prompts on `agent_start` and exposes workspace hub metadata.
 */

export interface PiExtensionContext {
  on(event: 'agent_start' | 'turn_start' | 'agent_end', listener: (data: any) => Promise<void> | void): void;
  appendSystemPrompt(prompt: string): void;
}

export interface HubConfig {
  workspace: string;
  hubUrl: string;
  agentLabel: string;
}

export function getHubConfig(): HubConfig {
  return {
    workspace: process.env.UA_WORKSPACE || process.cwd(),
    hubUrl: process.env.UA_HTTP_HUB || 'http://127.0.0.1:8765/mcp',
    agentLabel: process.env.UA_AGENT_LABEL || 'pi_worker',
  };
}

export default function registerUnifiedAgentExtension(context: PiExtensionContext) {
  context.on('agent_start', async () => {
    const { workspace, hubUrl, agentLabel } = getHubConfig();

    const systemPromptContext = [
      `[unified-agent context]`,
      `Agent Identity: ${agentLabel}`,
      `Workspace Root: ${workspace}`,
      `State Hub URL: ${hubUrl}`,
      `Connected to unified-agent persistent SQLite memory, task board, and Obsidian vault logs.`,
      `Prefer querying unified-agent tools (memory_search, task_list, fs_repo_map) for codebase discovery.`,
    ].join('\n');

    context.appendSystemPrompt(systemPromptContext);
  });
}
