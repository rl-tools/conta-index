const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web/search.js"), "utf8");

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.value = "";
  }
  append(...children) {
    for (const child of children) {
      this.children.push(...(child.tag === "fragment" ? child.children : [child]));
    }
  }
  replaceChildren() { this.children = []; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute() {}
  removeAttribute() {}
  focus() {}
}

async function render(index) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const sorts = Object.fromEntries(["downloads", "size", "first_indexed_at"].map(column => {
    const button = new Element("button");
    button.dataset.sort = column;
    button.parentElement = new Element();
    button.querySelector = () => new Element("span");
    return [column, button];
  }));
  let finish;
  const rendered = new Promise(resolve => { finish = resolve; });
  Object.defineProperty(element("search-status"), "textContent", {
    set(value) { if (value.startsWith("Showing")) finish(); },
  });
  element("search-scope").value = "all";
  vm.runInNewContext(source, {
    document: {
      getElementById: element,
      querySelectorAll: () => Object.values(sorts),
      createElement: tag => new Element(tag),
      createDocumentFragment: () => new Element("fragment"),
    },
    fetch: async url => {
      assert.equal(url, "version/1/generated/index.json");
      return { ok: true, json: async () => index };
    },
    URL, AbortController, setTimeout, clearTimeout, Intl,
  });
  element("search-query").value = "blob";
  element("search-form").listeners.submit({ preventDefault() {} });
  await rendered;
  return { rows: () => element("search-results").children, sort: column => sorts[column].listeners.click() };
}

test("size maps control download availability, skip unknown formats, and preserve URL queries", { timeout: 2000 }, async () => {
  const sha1 = "a".repeat(40);
  const sha256 = "b".repeat(64);
  const page = await render({
    shards: {
      primary: { url: "https://example.test/data/{sha1}?download=1#blob" },
      mirror: { url: "https://mirror.test/{sha256}" },
      invalid: { url: "javascript:alert('{sha1}')" },
    },
    entries: [{ sha1, sha256, description: "Example blob", shards: [
      { id: "primary", size: { future: 70, gzip: 80, gz: 100, raw: 1024 } },
      { id: "mirror", size: { raw: 1024, future: 60 } },
      { id: "invalid", size: { raw: 1024, gz: 100 } },
    ] }],
  });
  const links = page.rows()[0].children[4].children;
  assert.deepEqual(links.map(link => [link.textContent, link.href]), [
    ["primary (1.00 KiB)", `https://example.test/data/${sha1}?download=1#blob`],
    ["primary (.gz, 100 B)", `https://example.test/data/${sha1}.gz?download=1#blob`],
    ["mirror (1.00 KiB)", `https://mirror.test/${sha256}`],
  ]);
  assert.equal(links[0].title, "Download raw from primary: 1024 bytes");
  assert.equal(links[1].title, "Download .gz from primary: 100 bytes");
});

test("download sorting uses shard IDs from location objects", { timeout: 2000 }, async () => {
  const page = await render({
    shards: { alpha: { url: "https://a.test/{sha1}" }, zulu: { url: "https://z.test/{sha1}" } },
    entries: [
      { sha1: "a".repeat(40), sha256: "a".repeat(64), description: "Zulu blob", shards: [{ id: "zulu", size: { raw: 200 } }] },
      { sha1: "b".repeat(40), sha256: "b".repeat(64), description: "Alpha blob", shards: [{ id: "alpha", size: { raw: 1000, gz: 400 } }] },
    ],
  });
  page.sort("downloads");
  assert.deepEqual(page.rows().map(row => row.children[0].textContent), ["Alpha blob", "Zulu blob"]);
});

test("file sizes format binary units and retain exact bytes in tooltips", { timeout: 2000 }, async () => {
  const sizes = [0, 1, 1023, 1024, 1260, 1024 ** 2, 1024 ** 3, 1024 ** 4, 1024 ** 5];
  const page = await render({
    shards: {},
    entries: sizes.map((size, i) => ({
      sha1: String(i).repeat(40), sha256: String(i).repeat(64), description: "Size blob",
      shards: [{ id: "primary", size: { raw: size } }],
    })),
  });
  const cells = page.rows().map(row => row.children[3]);
  assert.deepEqual(cells.map(cell => cell.textContent), [
    "0 B", "1 B", "1023 B", "1.00 KiB", "1.23 KiB", "1.00 MiB", "1.00 GiB", "1.00 TiB", "1.00 PiB",
  ]);
  assert.deepEqual(cells.map(cell => cell.title), sizes.map(size => `${size} ${size === 1 ? "byte" : "bytes"}`));
});

test("file size sorting is numeric", { timeout: 2000 }, async () => {
  const sizes = [1000, 200, 0];
  const page = await render({
    shards: {},
    entries: sizes.map((size, i) => ({
      sha1: String(i).repeat(40), sha256: String(i).repeat(64), description: `Size blob ${i}`,
      shards: [{ id: "primary", size: { raw: size } }],
    })),
  });
  page.sort("size");
  assert.deepEqual(page.rows().map(row => row.children[3].textContent), ["0 B", "200 B", "1000 B"]);
  page.sort("size");
  assert.deepEqual(page.rows().map(row => row.children[3].textContent), ["1000 B", "200 B", "0 B"]);
});

test("each shard shows its own compressed size and zero-byte downloads remain available", { timeout: 2000 }, async () => {
  const page = await render({
    shards: {
      primary: { url: "https://primary.test/{sha1}" },
      mirror: { url: "https://mirror.test/{sha1}" },
    },
    entries: [{
      sha1: "a".repeat(40), sha256: "a".repeat(64), description: "Empty blob",
      shards: [{ id: "primary", size: { raw: 0, gz: 20 } }, { id: "mirror", size: { raw: 0, gz: 36 } }],
    }],
  });
  assert.deepEqual(page.rows()[0].children[4].children.map(link => [link.textContent, link.title]), [
    ["primary (0 B)", "Download raw from primary: 0 bytes"],
    ["primary (.gz, 20 B)", "Download .gz from primary: 20 bytes"],
    ["mirror (0 B)", "Download raw from mirror: 0 bytes"],
    ["mirror (.gz, 36 B)", "Download .gz from mirror: 36 bytes"],
  ]);
});

test("first indexed dates retain their full timestamp and sort chronologically", { timeout: 2000 }, async () => {
  const page = await render({
    shards: {},
    entries: [
      { sha1: "a".repeat(40), sha256: "a".repeat(64), description: "New blob", first_indexed_at: "2026-10-11T16:12:03Z", shards: [{ id: "primary", size: { raw: 1 } }] },
      { sha1: "b".repeat(40), sha256: "b".repeat(64), description: "Old blob", first_indexed_at: "2026-10-10T23:12:03Z", shards: [{ id: "primary", size: { raw: 1 } }] },
    ],
  });
  const date = page.rows()[0].children[5].children[0];
  assert.equal(date.tag, "time");
  assert.equal(date.textContent, "2026-10-11");
  assert.equal(date.dateTime, "2026-10-11T16:12:03Z");
  assert.equal(date.title, "2026-10-11T16:12:03Z");
  page.sort("first_indexed_at");
  assert.deepEqual(page.rows().map(row => row.children[0].textContent), ["Old blob", "New blob"]);
});
