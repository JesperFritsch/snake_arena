// Placeholder. A real leaderboard needs a backend scoring/ranking endpoint
// (none exists yet): a definition of "score" (wins? avg survival_rank? best
// final_length?) plus an aggregate-across-users query. Wire to that when ready.
export function LeaderboardPage() {
  return (
    <div className="page-pad">
      <div className="lead">
        <h1>Leaderboard</h1>
        <p className="sub">The best snakes in the arena, ranked.</p>

        <div className="placeholder-card">
          <div className="big">coming soon</div>
          <div>
            Rankings appear here once the scoring endpoint is live. The page is
            scaffolded and ready to bind to it.
          </div>
        </div>
      </div>
    </div>
  );
}
