(() => {
  "use strict";

  const form = document.getElementById("search-form");
  const query = document.getElementById("search-query");
  const scope = document.getElementById("search-scope");
  const status = document.getElementById("search-status");
  const table = document.getElementById("search-table");
  const results = document.getElementById("search-results");
  const retry = document.getElementById("search-retry");
  const more = document.getElementById("search-more");
  const pageSize = 50;
  let indexPromise;
  let timer;
  let revision = 0;
  let matches = [];
  let shown = 0;
  let shards = {};

  function loadIndex() {
    if (!indexPromise) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 15000);
      // Pin the format this page understands; reuse one request for all searches.
      indexPromise = fetch("version/1/generated/index.json", { signal: controller.signal })
        .then(response => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json();
        })
        .then(data => ({
          shards: data.shards,
          entries: data.entries.map(entry => ({
            entry,
            description: (entry.description || "").toLowerCase(),
            sha1: entry.sha1.toLowerCase(),
            sha256: entry.sha256.toLowerCase(),
          })),
        }))
        .catch(error => {
          indexPromise = undefined;
          throw error;
        })
        .finally(() => clearTimeout(timeout));
    }
    return indexPromise;
  }

  function downloadURL(template, entry) {
    if (typeof template !== "string") return null;
    try {
      const url = new URL(template
        .replaceAll("{sha1}", entry.sha1)
        .replaceAll("{sha256}", entry.sha256));
      return ["https:", "http:"].includes(url.protocol) ? url.href : null;
    } catch {
      return null;
    }
  }

  function renderMore() {
    const fragment = document.createDocumentFragment();
    const end = Math.min(shown + pageSize, matches.length);
    for (; shown < end; shown++) {
      const { entry } = matches[shown];
      const item = document.createElement("tr");
      const title = document.createElement("td");
      title.textContent = entry.description || "No description";
      item.append(title);
      for (const hash of [entry.sha1, entry.sha256]) {
        const cell = document.createElement("td");
        const code = document.createElement("code");
        code.textContent = hash;
        code.title = hash;
        cell.append(code);
        item.append(cell);
      }
      const links = document.createElement("td");
      for (const shard of entry.shards || []) {
        const url = downloadURL(shards?.[shard]?.url, entry);
        if (!url) continue;
        const link = document.createElement("a");
        link.href = url;
        link.textContent = shard;
        link.title = `Download from ${shard}`;
        links.append(link);
      }
      item.append(links);
      fragment.append(item);
    }
    results.append(fragment);
    table.hidden = matches.length === 0;
    more.hidden = shown >= matches.length;
    status.textContent = matches.length
      ? `Showing ${shown} of ${matches.length} ${matches.length === 1 ? "match" : "matches"}.`
      : "No matches.";
  }

  async function search(request) {
    const text = query.value.trim().toLowerCase();
    const selectedScope = scope.value;
    results.replaceChildren();
    table.hidden = true;
    matches = [];
    shown = 0;
    if (!text) {
      status.textContent = "Enter a description or hash to search.";
      return;
    }
    status.textContent = "Loading search index…";
    try {
      const data = await loadIndex();
      // Input invalidates pending work immediately, including while loading.
      if (request !== revision) return;
      shards = data.shards;
      const terms = text.split(/\s+/);
      matches = data.entries.filter(row => terms.every(term =>
        (selectedScope !== "hashes" && row.description.includes(term)) ||
        (selectedScope !== "descriptions" && (row.sha1.includes(term) || row.sha256.includes(term)))
      ));
      renderMore();
    } catch {
      if (request !== revision) return;
      status.textContent = "Could not load the search index. Retry or browse the sitemap below.";
      retry.hidden = false;
    }
  }

  function scheduleSearch(delay = 100) {
    const request = ++revision;
    clearTimeout(timer);
    more.hidden = true;
    retry.hidden = true;
    timer = setTimeout(() => search(request), delay);
  }

  query.addEventListener("input", () => scheduleSearch());
  scope.addEventListener("change", () => scheduleSearch(0));
  form.addEventListener("submit", event => {
    event.preventDefault();
    scheduleSearch(0);
  });
  retry.addEventListener("click", () => scheduleSearch(0));
  more.addEventListener("click", renderMore);
  document.getElementById("search").hidden = false;
  query.focus({ preventScroll: true });
  if (query.value.trim()) scheduleSearch(0);
})();
