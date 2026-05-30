const fs = require("fs");
const yaml = require("js-yaml");

const INPUT_FILE = "sources.yml";
const OUTPUT_FILE = "public/sources.json";

function toArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function normalizeSource(source, index) {
  return {
    id: source.id || `source-${index + 1}`,
    name: source.name || "Unnamed source",
    base_url: source.base_url || source.url || "",
    region: toArray(source.region),
    topic: toArray(source.topic || source.topics),
    trust_level: source.trust_level || "",
    review_status: source.review_status || "",
    notes: source.notes || "",
    last_checked: source.last_checked || "",
    candidate_pages: toArray(source.candidate_pages).map((page, pageIndex) => {
      if (typeof page === "string") {
        return {
          id: `${index + 1}-${pageIndex + 1}`,
          title: page,
          url: page,
          topics: []
        };
      }

      return {
        id: page.id || `${index + 1}-${pageIndex + 1}`,
        title: page.title || page.name || page.url || "Untitled page",
        url: page.url || "",
        topics: toArray(page.topic || page.topics),
        notes: page.notes || ""
      };
    })
  };
}

function main() {
  if (!fs.existsSync(INPUT_FILE)) {
    console.error(`Could not find ${INPUT_FILE}`);
    process.exit(1);
  }

  const raw = fs.readFileSync(INPUT_FILE, "utf8");
  const parsed = yaml.load(raw);

  const sourceList = Array.isArray(parsed)
    ? parsed
    : parsed.sources || parsed.resources || [];

  if (!Array.isArray(sourceList)) {
    console.error("Your YAML should be a list, or contain a top-level 'sources:' list.");
    process.exit(1);
  }

  const normalized = sourceList.map(normalizeSource);

  const trusted = normalized.filter(source => {
    const status = String(source.review_status).toLowerCase();
    return status === "trustworthy" || status === "trusted" || status === "approved";
  });

  fs.mkdirSync("public", { recursive: true });
  fs.writeFileSync(OUTPUT_FILE, JSON.stringify(trusted, null, 2));

  console.log(`Converted ${normalized.length} sources.`);
  console.log(`Wrote ${trusted.length} trusted sources to ${OUTPUT_FILE}.`);
}

main();
