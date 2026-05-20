import { useState } from "react";
import { buildTree, type TreeNode } from "../lib/editor";
import type { ProjectFile } from "../api/types";

interface Props {
  files: ProjectFile[];
  activePath: string | null;
  dirtyPaths: Set<string>;
  onOpen: (path: string) => void;
  onAddFile: () => void;
  onDeleteFile: (path: string) => void;
}

export function FileTree({
  files,
  activePath,
  dirtyPaths,
  onOpen,
  onAddFile,
  onDeleteFile,
}: Props) {
  const tree = buildTree(files);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const toggle = (path: string) =>
    setCollapsed((c) => {
      const next = new Set(c);
      next.has(path) ? next.delete(path) : next.add(path);
      return next;
    });

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
      const isActive = node.path === activePath;
      return (
        <div
          key={"f:" + node.path}
          className={`tree-row ${isActive ? "active" : ""}`}
          style={pad}
          onClick={() => onOpen(node.path)}
        >
          <span className="twig" />
          <span className={`fname ${dirtyPaths.has(node.path) ? "dirty" : ""}`}>
            {node.name}
          </span>
          <span
            className="del"
            title="Delete file"
            onClick={(e) => {
              e.stopPropagation();
              onDeleteFile(node.path);
            }}
          >
            ✕
          </span>
        </div>
      );
    });

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="title">Files</span>
        <span className="spacer" />
        <button className="btn ghost" style={{ padding: "3px 8px" }} onClick={onAddFile}>
          + New
        </button>
      </div>
      <div className="panel-body">
        {files.length === 0 ? (
          <div className="empty">
            <span className="muted">No files yet</span>
          </div>
        ) : (
          <div className="tree">{render(tree, 0)}</div>
        )}
      </div>
    </div>
  );
}
