import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Filemanager, Willow, type IEntity } from "@svar-ui/react-filemanager";
import "@svar-ui/react-filemanager/all.css";
import "./react-files.css";

type FileEntry = {
  name: string;
  path: string;
  type: string;
  size?: number;
  modified?: number;
};

type FilesModel = {
  executorId: string;
  directory: string;
  generation: number;
  filter: string;
  sort: string;
  descending: boolean;
  showHidden: boolean;
  entries: FileEntry[];
};

type FilesEvents = {
  select: (path: string) => void;
  openFolder: (path: string) => void;
};

function toFilemanagerData(entries: FileEntry[]): IEntity[] {
  return entries.map((entry, index) => ({
    // Widget IDs describe the displayed tree, not remote filesystem paths.
    id: `/${encodeURIComponent(entry.name)}`,
    name: entry.name,
    type: entry.type === "dir" ? "folder" : "file",
    size: entry.type === "dir" ? undefined : Number(entry.size) || 0,
    date: entry.modified ? new Date(entry.modified * 1000) : undefined,
    // WorkGate already sorted the visible entries. Preserve that order inside
    // SVAR rather than running a second name/size/date sort.
    sortIndex: index,
  }));
}

const svgIcon = (paths: string) =>
  `data:image/svg+xml,${encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`,
  )}`;

const folderIcon = svgIcon('<path d="M3 6h7l2 2h9v12H3z" fill="#d8ecff" stroke="#4c91c7"/>');
const fileIcon = svgIcon('<path d="M6 2h8l5 5v15H6z" fill="#f4f7fb"/><path d="M14 2v5h5"/><path d="M9 13h7M9 17h7"/>');
const imageIcon = svgIcon('<rect x="3" y="3" width="18" height="18" rx="2" fill="#edf7f0"/><path d="m4 19 6-6 4 4 3-3 4 4"/>');

function fileIconFor(file: { type?: string; name?: string }): string {
  if (file.type === "folder") return folderIcon;
  if (/\.(png|jpe?g|webp|svg|gif)$/i.test(file.name || "")) return imageIcon;
  return fileIcon;
}

function FilesView({ model, events }: { model: FilesModel; events: FilesEvents }) {
  const [mobile, setMobile] = useState(
    () => window.matchMedia("(max-width: 780px)").matches,
  );
  useEffect(() => {
    const media = window.matchMedia("(max-width: 780px)");
    const update = () => setMobile(media.matches);
    media.addEventListener("change", update);
    update();
    return () => media.removeEventListener("change", update);
  }, []);
  const byId = new Map(model.entries.map((entry) => [
    `/${encodeURIComponent(entry.name)}`,
    entry,
  ]));

  return <div className="wg-files-list" aria-label="Files">
    <Willow fonts={false}>
      <Filemanager
        key={JSON.stringify([
          model.executorId, model.directory, model.generation,
          model.filter, model.sort, model.descending, model.showHidden, mobile,
        ])}
        data={toFilemanagerData(model.entries)}
        panels={[{
          path: "/",
          _sorts: { "/": { key: "sortIndex", order: "asc" } },
        }]}
        mode={mobile ? "cards" : "table"}
        readonly
        preview={false}
        icons={fileIconFor}
        onSelectFile={({ id }) => {
          if (id && byId.has(id)) events.select(byId.get(id)!.path);
        }}
        onSetPath={({ id }) => {
          const entry = byId.get(id);
          if (entry?.type === "dir") events.openFolder(entry.path);
        }}
        onOpenFile={({ id }) => {
          const entry = byId.get(id);
          if (entry) events.select(entry.path);
        }}
      />
    </Willow>
  </div>;
}

function mount(host: HTMLElement, events: FilesEvents) {
  const root = createRoot(host);
  return {
    render(model: FilesModel) {
      root.render(<FilesView model={model} events={events} />);
    },
  };
}

declare global {
  interface Window {
    WorkgateReactFiles?: { mount: typeof mount };
  }
}

window.WorkgateReactFiles = { mount };
