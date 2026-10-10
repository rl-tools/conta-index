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
  const sort = new Element("button");
  sort.dataset.sort = "downloads";
  sort.parentElement = new Element();
  sort.querySelector = () => new Element("span");
  let finish;
  const rendered = new Promise(resolve => { finish = resolve; });
  Object.defineProperty(element("search-status"), "textContent", {
    set(value) { if (value.startsWith("Showing")) finish(); },
  });
  element("search-scope").value = "all";
  vm.runInNewContext(source, {
    document: {
      getElementById: element,
      querySelectorAll: () => [sort],
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
  return { rows: () => element("search-results").children, sort };
}

test("v1 location objects render raw and advertised gzip links with URL queries preserved", { timeout: 2000 }, async () => {
  const sha1 = "a".repeat(40);
  const sha256 = "b".repeat(64);
  const page = await render({
    shards: {
      primary: { url: "https://example.test/data/{sha1}?download=1#blob" },
      mirror: { url: "https://mirror.test/{sha256}" },
      legacy: { url: "https://legacy.test/{sha1}" },
      invalid: { url: "javascript:alert('{sha1}')" },
    },
    entries: [{ sha1, sha256, description: "Example blob", shards: [
      { id: "primary", gzip: true }, { id: "mirror", gzip: false },
      { id: "legacy" }, { id: "invalid", gzip: true },
    ] }],
  });
  const links = page.rows()[0].children[3].children;
  assert.deepEqual(links.map(link => [link.textContent, link.href]), [
    ["primary", `https://example.test/data/${sha1}?download=1#blob`],
    ["primary (.gz)", `https://example.test/data/${sha1}.gz?download=1#blob`],
    ["mirror", `https://mirror.test/${sha256}`],
    ["legacy", `https://legacy.test/${sha1}`],
  ]);
});

test("download sorting uses shard IDs from location objects", { timeout: 2000 }, async () => {
  const page = await render({
    shards: { alpha: { url: "https://a.test/{sha1}" }, zulu: { url: "https://z.test/{sha1}" } },
    entries: [
      { sha1: "a".repeat(40), sha256: "a".repeat(64), description: "Zulu blob", shards: [{ id: "zulu", gzip: false }] },
      { sha1: "b".repeat(40), sha256: "b".repeat(64), description: "Alpha blob", shards: [{ id: "alpha", gzip: true }] },
    ],
  });
  page.sort.listeners.click();
  assert.deepEqual(page.rows().map(row => row.children[0].textContent), ["Alpha blob", "Zulu blob"]);
});
