import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { cpp } from "@codemirror/lang-cpp";
import { rust } from "@codemirror/lang-rust";
import { java } from "@codemirror/lang-java";
import { StreamLanguage } from "@codemirror/language";
import { go } from "@codemirror/legacy-modes/mode/go";
import type { Extension } from "@codemirror/state";
import type { LanguageInfo, ProjectFile } from "../api/types";

/** Format a language name with its version, e.g. "python (3.12)". */
export function fmtLang(name: string, languages: LanguageInfo[]): string {
  const version = languages.find((l) => l.name === name)?.version;
  return version ? `${name} (${version})` : name;
}

/** Pick a CodeMirror language extension from a file path. */
export function languageFor(path: string): Extension[] {
  const ext = path.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "py":
      return [python()];
    case "js":
    case "jsx":
    case "mjs":
    case "cjs":
      return [javascript()];
    case "ts":
    case "tsx":
      return [javascript({ typescript: true, jsx: ext === "tsx" })];
    case "c":
    case "h":
    case "cpp":
    case "cc":
    case "hpp":
      return [cpp()];
    case "rs":
      return [rust()];
    case "go":
      return [StreamLanguage.define(go)];
    case "java":
      return [java()];
    case "toml":
      return [];
    default:
      return [];
  }
}

// ---- file tree ------------------------------------------------------------

export interface TreeNode {
  name: string;
  path: string; // full path for files; dir path for folders
  isDir: boolean;
  children: TreeNode[];
}

/** Build a nested folder tree from a flat list of files. Folders sort first. */
export function buildTree(files: ProjectFile[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", isDir: true, children: [] };

  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    let node = root;
    parts.forEach((part, i) => {
      const isLast = i === parts.length - 1;
      const childPath = parts.slice(0, i + 1).join("/");
      let child = node.children.find((c) => c.name === part && c.isDir !== isLast);
      if (!child) {
        child = { name: part, path: childPath, isDir: !isLast, children: [] };
        node.children.push(child);
      }
      node = child;
    });
  }

  const sortRec = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    nodes.forEach((n) => n.isDir && sortRec(n.children));
  };
  sortRec(root.children);
  return root.children;
}
