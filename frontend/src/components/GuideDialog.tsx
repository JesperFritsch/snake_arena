interface Props {
  onClose: () => void;
}

export function GuideDialog({ onClose }: Props) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal"
        style={{ width: 680, maxHeight: "calc(100vh - 48px)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <span className="title">Guide</span>
          <button className="btn ghost" style={{ padding: "2px 8px" }} onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body" style={{ gap: 0, fontSize: 13, lineHeight: 1.7 }}>
          <Section title="How the game works">
            <p>
              Each match takes place on a 2D grid. Snakes move one cell per step. Eating food makes
              your snake grow by one cell. A snake dies when it collides with a wall, another
              snake's body, or its own body. The last snake alive wins — but survival alone isn't
              enough, you also need to eat.
            </p>
            <p>
              Matches run in two configurations currently:{" "}
              <strong>2-player (20×20, 3 food)</strong> and{" "}
              <strong>4-player (20×20, 3 food)</strong>. More modes may be added over time.
            </p>
          </Section>

          <Section title="The Snake API">
            <p>
              Your snake is a class with these methods. The harness calls them in order at the
              start of each game, then calls <Code>update()</Code> every step.
            </p>

            <table className="guide-table">
              <thead>
                <tr>
                  <th>Method</th>
                  <th>When called</th>
                  <th>What to do</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><Code>set_id(id)</Code></td>
                  <td>Once before game starts</td>
                  <td>Your snake's numeric ID — use it to look yourself up in the map values and snakes dict</td>
                </tr>
                <tr>
                  <td><Code>set_start_length(length)</Code></td>
                  <td>Once before game starts</td>
                  <td>How long your snake starts</td>
                </tr>
                <tr>
                  <td><Code>set_start_position(pos)</Code></td>
                  <td>Once before game starts</td>
                  <td><Code>pos</Code> is an <Code>(x, y)</Code> tuple — your head's starting cell</td>
                </tr>
                <tr>
                  <td><Code>set_init_data(data)</Code></td>
                  <td>Once before game starts</td>
                  <td>Full map metadata — see below</td>
                </tr>
                <tr>
                  <td><Code>update(data)</Code></td>
                  <td>Every step</td>
                  <td>Return your next direction as <Code>(dx, dy)</Code></td>
                </tr>
              </tbody>
            </table>

            <p style={{ marginTop: 12 }}>
              A minimal Python snake that always moves right:
            </p>
            <Pre>{`class Snake:
    def update(self, data):
        return (1, 0)  # right`}</Pre>
          </Section>

          <Section title="Coordinates and directions">
            <p>
              <Code>(0, 0)</Code> is the <strong>top-left</strong> corner. Positive x goes right,
              positive y goes <strong>down</strong>.
            </p>
            <table className="guide-table">
              <thead>
                <tr>
                  <th>Direction</th>
                  <th>Return value</th>
                </tr>
              </thead>
              <tbody>
                <tr><td>Right</td><td><Code>(1, 0)</Code></td></tr>
                <tr><td>Left</td><td><Code>(-1, 0)</Code></td></tr>
                <tr><td>Down</td><td><Code>(0, 1)</Code></td></tr>
                <tr><td>Up</td><td><Code>(0, -1)</Code></td></tr>
              </tbody>
            </table>
            <p>
              Returning <Code>None</Code>, an invalid direction, or reversing into yourself kills
              your snake immediately.
            </p>
          </Section>

          <Section title="What update() receives">
            <p>
              The <Code>data</Code> dict passed to every <Code>update()</Code> call:
            </p>
            <Pre>{`{
  "map": np.ndarray,        # 2D array, shape (height, width)
  "snakes": {               # keyed by snake id (int)
    0: {"is_alive": True, "length": 4},
    1: {"is_alive": True, "length": 3},
  },
  "food_locations": [(x, y), ...]
}`}</Pre>
            <p>
              The map uses integer cell values. You learn what each value means from{" "}
              <Code>set_init_data()</Code>:
            </p>
            <Pre>{`{
  "height": 20, "width": 20,
  "free_value": ...,     # exact numbers are not guaranteed
  "blocked_value": ...,  # walls/obstacles
  "food_value": ...,
  "snake_tags": {0: "my-snake", 1: "opponent"},
  "snake_values": {
    0: {"head_value": ..., "body_value": ...},
    1: {"head_value": ..., "body_value": ...},
  },
  "start_positions": {0: (x, y), 1: (x, y)},
  "base_map": np.ndarray  # static walls/obstacles
}`}</Pre>
            <p>
              The key rule for navigation: any cell where{" "}
              <Code>value {"<="} free_value</Code> is safe to move into (free or food).
              Snake bodies and walls have values above <Code>free_value</Code>.
            </p>
            <p>
              The map is a 2D array in row-major order: index it as{" "}
              <Code>map[row][col]</Code>, i.e. <Code>map[y][x]</Code>.
            </p>
          </Section>

          <Section title="Testing">
            <p>
              Use the <strong>Test</strong> button in the editor to run matches against other
              agents. You choose opponents, grid size, and food count. There's a per-hour quota
              shown next to the button.
            </p>
            <p>
              In the match viewer you can step through the game frame by frame. The{" "}
              <strong>Console</strong> tab shows your snake's stdout for each step — anything you
              print in <Code>update()</Code> will appear there, grouped by step. The step time
              shows how long your agent took to respond.
            </p>
            <p>
              You can pin up to 9 test matches to keep them visible while you work.
            </p>
          </Section>

          <Section title="Submitting your agent">
            <p>
              Saving a file only updates your <em>dev version</em> — it won't affect your
              ranked score. Hit <strong>Submit</strong> to publish an immutable snapshot that
              gets entered into ranked matches.
            </p>
            <p>
              After submitting you can still edit freely; your dev version won't touch the
              submitted one. If you want to go back and review your submitted code, use the{" "}
              <em>Submitted version</em> toggle at the top of the editor. There's a per-hour and
              per-day quota on submits.
            </p>
          </Section>

          <Section title="Leaderboard and scoring">
            <p>
              Your score is a <strong>quality score</strong> multiplied by a{" "}
              <strong>CPU factor</strong>:
            </p>
            <Pre>{`final_score = quality × cpu_factor

quality     = mean fraction-of-leader across categories
cpu_factor  = 1 − 0.4 × min(avg_step_ms / budget_ms, 1)`}</Pre>
            <p>
              <strong>Quality</strong> measures how you did relative to the other participants
              in the same match. For each category, you get a score of{" "}
              <Code>your_value / leader_value</Code> (or its inverse for lower-is-better
              categories). These are averaged:
            </p>
            <table className="guide-table">
              <thead><tr><th>Mode</th><th>Categories</th></tr></thead>
              <tbody>
                <tr>
                  <td>Multi</td>
                  <td>survival rank (lower is better), trapping count, final length</td>
                </tr>
                <tr>
                  <td>Solo</td>
                  <td>final length</td>
                </tr>
              </tbody>
            </table>
            <p>
              <strong>CPU factor</strong> ranges from 1.0 (near-zero CPU) down to 0.6 (at
              the sustained budget ceiling). It's a bounded modifier — a slow snake that plays
              well still beats a fast snake that doesn't.
            </p>
            <p>
              The leaderboard shows your <strong>average score</strong> across all ranked
              matches. The <strong>Overall</strong> tab requires you to have played enough
              matches in every active mode.
            </p>
          </Section>

          <Section title="Quotas">
            <p>
              Small limits exist to keep the system fair for everyone:
            </p>
            <ul style={{ paddingLeft: 20, margin: "8px 0" }}>
              <li><strong>Test matches</strong> — hourly limit. Badge next to the Test button.</li>
              <li><strong>Submits</strong> — hourly + daily limit. Badge next to the Submit button.</li>
              <li><strong>Image uploads</strong> — daily limit (custom Docker images only).</li>
            </ul>
            <p>
              Hovering over a quota badge shows when the next slot opens.
            </p>
          </Section>

          <Section title="Custom Docker images (advanced)">
            <p>
              Instead of using the browser editor, you can upload a Docker image tarball. Your
              image must run a gRPC server on port 50051 that implements the{" "}
              <Code>RemoteSnake</Code> service — the same interface the built-in harnesses use.
            </p>
            <p>
              This lets you write your snake in any language and use any libraries, as long as
              you can package it in a container. See the <em>Custom image</em> option in{" "}
              <strong>New project</strong> for upload instructions.
            </p>
          </Section>
        </div>

        <div className="modal-foot">
          <button className="btn primary" onClick={onClose}>
            Got it
          </button>
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div
        style={{
          fontSize: 11,
          letterSpacing: "1px",
          textTransform: "uppercase",
          color: "var(--text-faint)",
          marginBottom: 8,
          paddingBottom: 6,
          borderBottom: "1px solid var(--border-soft)",
        }}
      >
        {title}
      </div>
      <div style={{ color: "var(--text-dim)" }}>{children}</div>
    </div>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code
      style={{
        background: "var(--bg-input)",
        border: "1px solid var(--border-soft)",
        borderRadius: 3,
        padding: "1px 5px",
        fontSize: 12,
        color: "var(--accent)",
      }}
    >
      {children}
    </code>
  );
}

function Pre({ children }: { children: React.ReactNode }) {
  return (
    <pre
      style={{
        background: "var(--bg-input)",
        border: "1px solid var(--border-soft)",
        borderRadius: "var(--radius)",
        padding: "10px 14px",
        fontSize: 12,
        overflowX: "auto",
        margin: "8px 0",
        color: "var(--text)",
        lineHeight: 1.6,
      }}
    >
      {children}
    </pre>
  );
}
