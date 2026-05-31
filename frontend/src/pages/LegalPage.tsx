import { Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface LegalPageProps {
  title: string;
  doc: string;
}

export function LegalPage({ title, doc }: LegalPageProps) {
  return (
    <div className="legal-shell">
      <header className="legal-topbar">
        <Link to="/" className="brand">
          <span className="mark">▰</span>
          <span>Snake Arena</span>
        </Link>
        <nav className="legal-nav">
          <Link to="/terms">Terms</Link>
          <Link to="/privacy">Privacy</Link>
          <Link to="/acceptable-use">Acceptable Use</Link>
        </nav>
      </header>
      <main className="legal-main">
        <article className="legal-article">
          <h1>{title}</h1>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{doc}</ReactMarkdown>
        </article>
      </main>
    </div>
  );
}
