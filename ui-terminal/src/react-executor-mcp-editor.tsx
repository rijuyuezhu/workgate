import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type Tool = { name: string; description?: string };
type Tone = "plain" | "success" | "warning" | "error";

function SecretRows({ names, disabled, onRemove }: {
  names: string[] | null;
  disabled: boolean;
  onRemove: (name: string) => void;
}) {
  if (names === null) return null;
  if (!names.length) return <p className="muted">No private secrets saved for this MCP.</p>;
  return <>{names.map(name => <div key={name} className="executor-auth-secret-row">
    <code>{name}</code><span>Value hidden</span>
    <button type="button" disabled={disabled} onClick={() => onRemove(name)}>Remove</button>
  </div>)}</>;
}

function TestResult({ message, tools }: { message: string; tools: Tool[] }) {
  return <>{message}{tools.map((tool, i) => <div key={`${tool.name}:${i}`}>
    {tool.name}{tool.description ? `: ${tool.description}` : ""}
  </div>)}</>;
}

function mount(secretHost: HTMLElement, testHost: HTMLElement, onRemove: (name: string) => void) {
  const secrets = createRoot(secretHost);
  const test = createRoot(testHost);
  return {
    renderSecrets(names: string[] | null, disabled: boolean) {
      flushSync(() => secrets.render(<SecretRows names={names} disabled={disabled} onRemove={onRemove} />));
    },
    renderTest(message: string, tone: Tone = "plain", tools: Tool[] = []) {
      // Preserve the existing notice styling and accessible status role on this stable host.
      testHost.classList.toggle("executor-notice", Boolean(message) && tone !== "plain");
      if (message && tone !== "plain") testHost.dataset.tone = tone;
      else delete testHost.dataset.tone;
      testHost.setAttribute("role", tone === "error" ? "alert" : "status");
      flushSync(() => test.render(<TestResult message={message} tools={tools} />));
    },
  };
}

export const executorMcpEditor = { mount };
