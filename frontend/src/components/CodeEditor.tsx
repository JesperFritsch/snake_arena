import CodeMirror from "@uiw/react-codemirror";
import { tokyoNight } from "@uiw/codemirror-theme-tokyo-night";
import { languageFor } from "../lib/editor";

interface Props {
  path: string | null;
  value: string;
  readOnly?: boolean;
  onChange: (value: string) => void;
}

export function CodeEditor({ path, value, readOnly, onChange }: Props) {
  if (!path) {
    return (
      <div className="empty">
        <span className="big">no file open</span>
        <span>Pick a file from the panel, or create a new one.</span>
      </div>
    );
  }

  return (
    <div className="editor-host">
      <CodeMirror
        value={value}
        theme={tokyoNight}
        extensions={languageFor(path)}
        readOnly={readOnly}
        onChange={onChange}
        height="100%"
        basicSetup={{
          lineNumbers: true,
          highlightActiveLine: true,
          foldGutter: true,
          autocompletion: true,
          tabSize: 4,
        }}
      />
    </div>
  );
}
