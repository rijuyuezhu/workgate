import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type Edge = {
  key: string; a: string; b: string; status: string; selected: boolean;
  label: string; title: string; x1: number; y1: number; x2: number; y2: number;
};
type Node = {
  id: string; name: string; online: boolean; presence: string;
  focused: boolean; left?: string; top?: string;
};
type Direction = {
  from: string; to: string; label: string; testLabel: string; status: string;
  statusLabel: string; details: string[];
};
type GraphModel = { edges: Edge[]; nodes: Node[] };
type Callbacks = {
  pickPair: (a: string, b: string) => void;
  focus: (id: string) => void;
  test: (from: string, to: string) => void;
};

function Graph({ model, actions }: { model: GraphModel; actions: Callbacks }) {
  return <>
    <svg id="topology-edges" className="topology-edges"
      aria-label="Executor connection edges" viewBox="0 0 1000 660" preserveAspectRatio="none">
      {model.edges.map(edge => <g key={edge.key} role="button" tabIndex={0}
        aria-label={edge.label} data-pair={edge.key} data-status={edge.status}
        className={edge.selected ? "selected" : undefined}
        onClick={() => actions.pickPair(edge.a, edge.b)}
        onKeyDown={event => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          actions.pickPair(edge.a, edge.b);
        }}>
        <line className="topology-edge-line" x1={edge.x1} y1={edge.y1} x2={edge.x2} y2={edge.y2} />
        <line className="topology-edge-hit" x1={edge.x1} y1={edge.y1} x2={edge.x2} y2={edge.y2} />
        <title>{edge.title}</title>
      </g>)}
    </svg>
    <div id="topology-nodes" className="topology-nodes">
      {model.nodes.map(node => <button key={node.id} type="button"
        className="topology-node" data-executor-id={node.id}
        data-online={String(node.online)} aria-pressed={node.focused}
        aria-label={`Focus connections for ${node.name} (${node.online ? "online" : "offline"})`}
        style={node.left && node.top ? {left: node.left, top: node.top} : undefined}
        onClick={() => actions.focus(node.id)}>
        <span className="executor-band-mark">{node.name.slice(0, 1).toUpperCase()}</span>
        <span className="topology-node-name">{node.name}</span>
        <span className="topology-node-presence">{node.presence}</span>
      </button>)}
    </div>
  </>;
}

function Directions({ rows, test, busy }: {
  rows: Direction[]; test: Callbacks["test"]; busy: boolean;
}) {
  return <>{rows.map(row => <div key={`${row.from}\u0000${row.to}`}
    className="topology-direction" data-status={row.status}>
    <div className="topology-direction-main">
      <div className="topology-direction-title">
        <strong>{row.label}</strong>
        <span className="topology-direction-status">{row.statusLabel}</span>
      </div>
      {row.details.length > 0 && <div className="topology-direction-meta">
        {row.details.map((detail, i) => <span key={i}>{detail}</span>)}
      </div>}
    </div>
    <button type="button" aria-disabled={busy} data-source={row.from}
      data-destination={row.to} aria-label={row.testLabel}
      onClick={() => test(row.from, row.to)}>Test</button>
  </div>)}</>;
}

function mount(stage: HTMLElement, directions: HTMLElement, callbacks: Callbacks) {
  const graphRoot = createRoot(stage);
  const directionRoot = createRoot(directions);
  return {
    renderGraph(model: GraphModel) {
      flushSync(() => graphRoot.render(<Graph model={model} actions={callbacks} />));
    },
    renderDirections(rows: Direction[], busy: boolean) {
      flushSync(() => directionRoot.render(<Directions rows={rows} busy={busy} test={callbacks.test} />));
    },
  };
}

export const executorTopology = { mount };
