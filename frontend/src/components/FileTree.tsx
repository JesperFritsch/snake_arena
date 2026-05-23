import { useEffect, useState } from "react";
import { buildTree, type TreeNode } from "../lib/editor";
import type { ProjectFile } from "../api/types";

interface Props {
  files: ProjectFile[];
  activePath: string | null;
  dirtyPaths: Set<string>;
  onOpen: (path: string) => void;
  onAddFile?: (path: string) => void;
  onRenameFile?: (oldPath: string, newPath: string) => void;
  onDeleteFile?: (path: string) => void;
}

export function FileTree({
  files,
  activePath,
  dirtyPaths,
  onOpen,
  onAddFile,
  onRenameFile,
  onDeleteFile,
}: Props) {
  const tree = buildTree(files);
  const editable = !!onAddFile;

  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null); // path being renamed
  const [draft, setDraft] = useState("");                        // value for add OR rename
  const [menu, setMenu] = useState<{ path: string; x: number; y: number } | null>(null);

  // Close the context menu on any outside interaction.
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [menu]);

  const toggle = (path: string) =>
    setCollapsed((c) => {
      const next = new Set(c);
      next.has(path) ? next.delete(path) : next.add(path);
      return next;
    });

  // Returns an error string, or null if the name is usable. `excludePath`
  // skips one path (the file being renamed, so renaming to itself is fine).
  const validate = (value: string, excludePath?: string): string | null => {
    const v = value.trim();
    if (!v) return "name required";
    if (files.some((f) => f.path === v && f.path !== excludePath)) return "already exists";
    return null;
  };

  const cancelEdit = () => {
    setAdding(false);
    setRenaming(null);
    setDraft("");
  };
  const startAdd = () => {
    setRenaming(null);
    setDraft("");
    setAdding(true);
  };
  const startRename = (path: string) => {
    setMenu(null);
    setAdding(false);
    setDraft(path);
    setRenaming(path);
  };

  const draftError = adding || renaming ? validate(draft, renaming ?? undefined) : null;

  const commitAdd = () => {
    if (validate(draft)) return;
    onAddFile?.(draft.trim());
    cancelEdit();
  };
  const commitRename = (oldPath: string) => {
    const v = draft.trim();
    if (validate(v, oldPath)) return;
    if (v !== oldPath) onRenameFile?.(oldPath, v);
    cancelEdit();
  };

  const editRow = (onCommit: () => void, pad: React.CSSProperties) => (
    <div className="tree-edit" style={pad}>
      <input
        className="input"
        autoFocus
        value={draft}
        placeholder="src/util.py"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onCommit();
          else if (e.key === "Escape") cancelEdit();
        }}
        onBlur={cancelEdit}
      />
      {draft.trim() !== "" && draftError && <span className="tree-edit-err">{draftError}</span>}
    </div>
  );

  const render = (nodes: TreeNode[], depth: number): React.ReactNode =>
    nodes.map((node) => {
      const pad = { paddingLeft: 12 + depth * 14 };
      if (node.isDir) {
        const isCollapsed = collapsed.has(node.path);
        return (
          <div key={"d:" + node.path}>
            <div className="tree-row" style={pad} onClick={() => toggle(node.path)}>
              <span className="twig">{isCollapsed ? "▸" : "▾"}</span>
              <span className="fname">{node.name}/</span>
            </div>
            {!isCollapsed && render(node.children, depth + 1)}
          </div>
        );
      }

      if (renaming === node.path) {
        return <div key={"f:" + node.path}>{editRow(() => commitRename(node.path), pad)}</div>;
      }

      const isActive = node.path === activePath;
      const openMenu = (e: React.MouseEvent) => {
        e.preventDefault();
        e.stopPropagation();
        setMenu({ path: node.path, x: e.clientX, y: e.clientY });
      };
      return (
        <div
          key={"f:" + node.path}
          className={`tree-row ${isActive ? "active" : ""}`}
          style={pad}
          onClick={() => onOpen(node.path)}
          onContextMenu={editable ? openMenu : undefined}
        >
          <span className="twig" />
          <span className={`fname ${dirtyPaths.has(node.path) ? "dirty" : ""}`}>{node.name}</span>
          {editable && (
            <span className="kebab" title="File actions" onClick={openMenu}>⋯</span>
          )}
        </div>
      );
    });

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="title">Files</span>
      </div>
      <div className="panel-body">
        <div className="tree">
          {files.length === 0 && !adding && (
            <div className="empty"><span className="muted">No files yet</span></div>
          )}
          {render(tree, 0)}
          {editable &&
            (adding ? (
              editRow(commitAdd, { paddingLeft: 12 })
            ) : (
              <button className="tree-add" onClick={startAdd}>+ New file</button>
            ))}
        </div>
      </div>

      {menu && (
        <div
          className="ctx-menu"
          style={{ left: menu.x, top: menu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          {onRenameFile && (
            <button onClick={() => startRename(menu.path)}>Rename</button>
          )}
          {onDeleteFile && (
            <button
              className="danger"
              onClick={() => {
                const p = menu.path;
                setMenu(null);
                onDeleteFile(p);
              }}
            >
              Delete
            </button>
          )}
        </div>
      )}
    </div>
  );
}
