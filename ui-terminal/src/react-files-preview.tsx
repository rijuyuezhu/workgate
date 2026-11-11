import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type Entry = { name: string; path: string; type: string };
type Token = { type: string; text: string };
type Preview = {
  kind: "message" | "directory" | "image" | "binary" | "text";
  message?: string;
  directory?: string;
  entries?: Entry[];
  imageSrc?: string;
  imageAlt?: string;
  source?: string;
  language?: string;
  tokens?: Token[];
};

function FilePreview({ model, onNavigate }: {
  model: Preview;
  onNavigate: (directory: string, path: string) => void;
}) {
  if (model.kind === "message") return <div className="empty-state">{model.message}</div>;
  if (model.kind === "directory") return <div className="file-preview-directory">
    {model.entries?.length ? model.entries.map(entry => <button key={entry.path} type="button"
      className="file-preview-entry" title={entry.path}
      onClick={() => onNavigate(model.directory || "", entry.path)}>
      {entry.type === "dir" ? "▰" : entry.type === "link" ? "↗" : "▤"} {entry.name}
    </button>) : <div className="empty-state">Directory is empty.</div>}
  </div>;
  if (model.kind === "image") return <img className="file-preview-image"
    src={model.imageSrc} alt={model.imageAlt || "File image"} />;
  return <pre className="file-preview-text">
    {model.kind === "text" && model.tokens && model.language !== "plain"
      ? <code className={`syntax-code language-${model.language}`}>
          {model.tokens.map((token, i) => token.type === "plain" ? token.text
            : <span key={i} className={`syntax-token syntax-${token.type}`}>{token.text}</span>)}
        </code>
      : model.source}
  </pre>;
}

function mount(host: HTMLElement, onNavigate: (directory: string, path: string) => void) {
  const root = createRoot(host);
  return {
    render(model: Preview) {
      // File selection and editor visibility are controlled by the existing JS controller.
      flushSync(() => root.render(<FilePreview model={model} onNavigate={onNavigate} />));
    },
  };
}

export const filesPreview = { mount };
