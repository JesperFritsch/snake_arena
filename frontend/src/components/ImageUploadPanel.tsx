import { useRef, useState } from "react";
import { useApi, ApiError } from "../api/client";
import type { LanguageInfo, ProjectMeta } from "../api/types";

const MAX_MB = 500;
const MAX_BYTES = MAX_MB * 1024 * 1024;

interface Props {
  meta: ProjectMeta;
  languages: LanguageInfo[];
  onUploaded: (updated: ProjectMeta) => void;
}

export function ImageUploadPanel({ meta, languages, onUploaded }: Props) {
  const api = useApi();
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [harnessLang, setHarnessLang] = useState(languages[0]?.name ?? "");

  const upload = async (file: File) => {
    setError(null);
    if (file.size > MAX_BYTES) {
      setError(`File too large (max ${MAX_MB} MB)`);
      return;
    }
    setUploading(true);
    try {
      const updated = await api.uploadProjectImage(meta.id, file);
      onUploaded(updated);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setUploading(false);
    }
  };

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) void upload(file);
    e.target.value = "";
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) void upload(file);
  };

  const status = meta.dev_build_status;
  const hasImage = !!meta.dev_image_tag;

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="title">Custom image</span>
      </div>
      <div className="panel-body" style={{ padding: "24px 32px", overflowY: "auto" }}>
        <div style={{ maxWidth: 620 }}>

          {/* ---- Upload -------------------------------------------------- */}
          <section>
            <h3 style={{ marginTop: 0, marginBottom: 12 }}>Upload image</h3>
            <p style={{ marginTop: 0 }}>
              Build a Docker image that implements the <code>RemoteSnake</code> gRPC
              service on port&nbsp;<code>50051</code>, then export and upload it here.
            </p>

            <div className="upload-steps">
              <div className="upload-step">
                <span className="step-num">1</span>
                <div>
                  <strong>Get the proto file or an example harness</strong>
                  <div style={{ marginTop: 6, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                    <button className="btn ghost" onClick={() => void api.downloadProto()}>
                      sim_interface.proto
                    </button>
                    {languages.length > 0 && (
                      <>
                        <select
                          className="select"
                          value={harnessLang}
                          onChange={(e) => setHarnessLang(e.target.value)}
                          style={{ width: "auto" }}
                        >
                          {languages.map((l) => (
                            <option key={l.name} value={l.name}>{l.name}</option>
                          ))}
                        </select>
                        <button
                          className="btn ghost"
                          onClick={() => void api.downloadHarness(harnessLang)}
                        >
                          download harness
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>

              <div className="upload-step">
                <span className="step-num">2</span>
                <div>
                  <strong>Build and export your image</strong>
                  <div className="code-block">
                    <code>docker build -t my-snake .</code>
                    <code>docker save my-snake -o my-snake.tar</code>
                  </div>
                </div>
              </div>

              <div className="upload-step">
                <span className="step-num">3</span>
                <div>
                  <strong>Upload the tarball below</strong>
                  <p className="muted" style={{ marginBottom: 0 }}>
                    Max {MAX_MB}&nbsp;MB. Re-upload at any time to replace the current image.
                  </p>
                </div>
              </div>
            </div>

            <div
              className={`dropzone${dragOver ? " drag-over" : ""}${uploading ? " busy" : ""}`}
              onClick={() => !uploading && inputRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
            >
              <input
                ref={inputRef}
                type="file"
                accept=".tar,.tar.gz,.tgz,application/x-tar,application/gzip,application/x-gzip"
                style={{ display: "none" }}
                onChange={onFileChange}
              />
              {uploading ? (
                <span className="muted">Uploading…</span>
              ) : (
                <>
                  <span style={{ fontSize: 28, lineHeight: 1 }}>↑</span>
                  <span>Drop <code>.tar</code> or <code>.tar.gz</code> here, or click to browse</span>
                </>
              )}
            </div>

            {error && (
              <div style={{ color: "var(--red)", fontSize: 13, marginTop: 8 }}>{error}</div>
            )}
            {hasImage && !error && (
              <div style={{ marginTop: 12, fontSize: 13 }}>
                <span style={{ color: "#3fb950" }}>✓ Image uploaded</span>
                {status === "built"   && <span className="muted"> — run a test match to validate it</span>}
                {status === "ready"   && <span className="muted"> — validated and ready to submit</span>}
                {status === "crashed" && <span style={{ color: "var(--red)" }}> — agent crashed during testing; fix and re-upload</span>}
              </div>
            )}
          </section>

          <hr style={{ margin: "32px 0", borderColor: "var(--border)" }} />

          {/* ---- Interface docs ------------------------------------------ */}
          <section>
            <h3 style={{ marginTop: 0, marginBottom: 12 }}>Interface reference</h3>
            <p>
              Your image must run a gRPC server on port <code>50051</code> implementing the
              <code> RemoteSnake</code> service from <code>sim_interface.proto</code>.
              The arena calls these RPCs in order for each game:
            </p>

            <table className="ref-table">
              <tbody>
                <tr>
                  <td><code>SetId(SnakeId)</code></td>
                  <td>Your snake's integer ID in this game. Called once before the game starts.</td>
                </tr>
                <tr>
                  <td><code>SetStartLength(StartLength)</code></td>
                  <td>Initial body length. Called once before the game starts.</td>
                </tr>
                <tr>
                  <td><code>SetStartPosition(StartPosition)</code></td>
                  <td>Starting head position <code>{"{"} x, y {"}"}</code>. Called once before the game starts.</td>
                </tr>
                <tr>
                  <td><code>SetInitData(EnvInitData)</code></td>
                  <td>
                    Full grid config: dimensions, cell sentinel values, all snakes' IDs/tags/colours,
                    and the static base map (walls). Called once before the game starts.
                  </td>
                </tr>
                <tr>
                  <td><code>Update(stream EnvData)<br/>→ stream UpdateResponse</code></td>
                  <td>
                    Bidirectional stream — one message per step. Each <code>EnvData</code> contains the
                    current grid, live snake states, and food positions. Respond with
                    an <code>UpdateResponse</code> whose <code>direction</code> field is a
                    <code> Coord {"{"} x, y {"}"}</code>:
                    <div className="code-block" style={{ marginTop: 6 }}>
                      <code>{"{ x:  1, y:  0 }"}  →  right</code>
                      <code>{"{ x: -1, y:  0 }"}  →  left</code>
                      <code>{"{ x:  0, y:  1 }"}  →  down</code>
                      <code>{"{ x:  0, y: -1 }"}  →  up</code>
                    </div>
                    Row&nbsp;0 is the top of the grid; <em>y increases downward</em>.
                  </td>
                </tr>
                <tr>
                  <td><code>Reset(Empty)</code></td>
                  <td>Not used</td>
                </tr>
                <tr>
                  <td><code>Kill(Empty)</code></td>
                  <td>Not used</td>
                </tr>
              </tbody>
            </table>

            <h4 style={{ marginTop: 24 }}>Step logs and <code>---STEP_END---</code></h4>
            <p>
              Anything your process writes to <strong>stdout</strong> is captured and shown in the
              match viewer as per-step logs. To keep each step's output together, print the marker
              <code> ---STEP_END---</code> on its own line after your step logic — this tells the
              arena where one step's output ends and the next begins.
            </p>
            <div className="code-block">
              <code>{"# python example"}</code>
              <code>{"print(f'step {step_n}: chose right')"}</code>
              <code>{"print('---STEP_END---')"}</code>
            </div>
            <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
              Omitting the marker is fine — logs will just appear unsplit. The gRPC response is sent
              separately and is not affected by stdout output.
            </p>
          </section>

        </div>
      </div>
    </div>
  );
}
