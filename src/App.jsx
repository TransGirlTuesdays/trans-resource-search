import { useEffect, useMemo, useState } from "react";
import "./App.css";

function normalize(value) {
  return String(value || "").toLowerCase().trim();
}

function includesText(value, query) {
  return normalize(value).includes(normalize(query));
}

function sourceSearchText(source) {
  const candidatePageText = (source.candidate_pages || [])
  .map(page => [
    page.title,
    page.url,
    page.notes,
    ...(page.topics || [])
  ].join(" "))
  .join(" ");

  return [
    source.name,
    source.base_url,
    source.notes,
    source.trust_level,
    source.review_status,
    source.last_checked,
    ...(source.region || []),
    ...(source.topic || []),
    candidatePageText
  ].join(" ");
}

function sourceMatches(source, filters) {
  const searchText = sourceSearchText(source);

  const matchesQuery =
  !filters.query || includesText(searchText, filters.query);

  const matchesTopic =
  !filters.topic || (source.topic || []).includes(filters.topic);

  const matchesRegion =
  !filters.region || (source.region || []).includes(filters.region);

  return matchesQuery && matchesTopic && matchesRegion;
}

function getHostname(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function ResultCard({ source }) {
  const candidatePages = source.candidate_pages || [];

  return (
    <article className="result-card">
    <div className="result-header">
    <div>
    <h2>{source.name}</h2>
    <p className="domain">{getHostname(source.base_url)}</p>
    </div>

    <span className="badge">{source.review_status || "Trusted"}</span>
    </div>

    <div className="meta-grid">
    <p>
    <strong>Region:</strong>{" "}
    {(source.region || []).length ? source.region.join(", ") : "Not specified"}
    </p>

    <p>
    <strong>Topics:</strong>{" "}
    {(source.topic || []).length ? source.topic.join(", ") : "Not specified"}
    </p>

    {source.last_checked && (
      <p>
      <strong>Last checked:</strong> {source.last_checked}
      </p>
    )}
    </div>

    {source.notes && <p className="notes">{source.notes}</p>}

    {source.base_url && (
      <a
      className="main-link"
      href={source.base_url}
      target="_blank"
      rel="noreferrer"
      >
      Visit main site
      </a>
    )}

    {candidatePages.length > 0 && (
      <div className="pages">
      <h3>Relevant pages</h3>

      <ul>
      {candidatePages.slice(0, 6).map(page => (
        <li key={page.id || page.url}>
        <a href={page.url} target="_blank" rel="noreferrer">
        {page.title || page.url}
        </a>

        {page.topics?.length > 0 && (
          <span className="page-topics">
          {" "}
          — {page.topics.join(", ")}
          </span>
        )}
        </li>
      ))}
      </ul>
      </div>
    )}
    </article>
  );
}

export default function App() {
  const [sources, setSources] = useState([]);
  const [loadError, setLoadError] = useState("");
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState("");
  const [region, setRegion] = useState("");

  useEffect(() => {
    fetch("/sources.json")
    .then(response => {
      if (!response.ok) {
        throw new Error("Could not load sources.json");
      }

      return response.json();
    })
    .then(setSources)
    .catch(error => {
      console.error(error);
      setLoadError("Could not load the resource database.");
    });
  }, []);

  const topics = useMemo(() => {
    return [...new Set(sources.flatMap(source => source.topic || []))]
    .filter(Boolean)
    .sort();
  }, [sources]);

  const regions = useMemo(() => {
    return [...new Set(sources.flatMap(source => source.region || []))]
    .filter(Boolean)
    .sort();
  }, [sources]);

  const results = useMemo(() => {
    return sources.filter(source =>
    sourceMatches(source, { query, topic, region })
    );
  }, [sources, query, topic, region]);

  function clearFilters() {
    setQuery("");
    setTopic("");
    setRegion("");
  }

  return (
    <main className="app">
    <section className="hero">
    <p className="eyebrow">Trustworthy trans resources</p>

    <h1>Search reviewed trans-supportive sources</h1>

    <p className="intro">
    Find legal, healthcare, school, safety, and community resources from
    a curated list of reviewed sources. This site links to external
    resources and does not provide medical or legal advice.
    </p>

    <div className="safety-box">
    <strong>Safety note:</strong> For emergencies or immediate danger,
    contact local emergency services or a crisis service in your area.
    </div>
    </section>

    <section className="search-panel" aria-label="Search filters">
    <label>
    Search
    <input
    value={query}
    onChange={event => setQuery(event.target.value)}
    placeholder="Try: healthcare, passport, youth, asylum, school..."
    />
    </label>

    <div className="filters">
    <label>
    Topic
    <select value={topic} onChange={event => setTopic(event.target.value)}>
    <option value="">All topics</option>
    {topics.map(item => (
      <option key={item} value={item}>
      {item}
      </option>
    ))}
    </select>
    </label>

    <label>
    Region
    <select value={region} onChange={event => setRegion(event.target.value)}>
    <option value="">All regions</option>
    {regions.map(item => (
      <option key={item} value={item}>
      {item}
      </option>
    ))}
    </select>
    </label>
    </div>

    <button className="clear-button" type="button" onClick={clearFilters}>
    Clear filters
    </button>
    </section>

    {loadError && <p className="error">{loadError}</p>}

    <section className="results-summary">
    <p>
    Showing <strong>{results.length}</strong> of{" "}
    <strong>{sources.length}</strong> trusted sources.
    </p>
    </section>

    <section className="results" aria-label="Search results">
    {results.map(source => (
      <ResultCard key={source.id || source.base_url} source={source} />
    ))}

    {!loadError && results.length === 0 && (
      <div className="empty-state">
      <h2>No matching resources found</h2>
      <p>
      Try a broader search term, remove a filter, or search by region.
      </p>
      </div>
    )}
    </section>
    </main>
  );
}
