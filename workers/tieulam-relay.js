// ════════════════════════════════════════════════════════════════════════════
// tieulam-relay — TieuLam Relay Worker v5
// Global safety net, CF Cache, lazy discovery, cron warm, never 502
// ════════════════════════════════════════════════════════════════════════════

const WORKER_VERSION = "v6-relay-stream";
const TIEULAM_FRONTS = [
  "https://sv2.tieulam.info",
  "https://sv1.tieulam1.live",
];

const TIEULAM_HDR = {
  "Content-Type": "application/json",
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  Referer: "https://sv2.tieulam.info/",
  Origin: "https://sv2.tieulam.info",
  Accept: "application/json, text/plain, */*",
};

const API_DISC_MS = 3_600_000;
const DATA_FRESH_MS = 1_800_000;
const KV_STALE_MS = 7_200_000;
const PROBE_TIMEOUT = 5_000;
const FETCH_TIMEOUT = 12_000;
const CF_CACHE_KEY = "https://tieulam-relay-internal/matches-v5-relay";

let _apiBase = "";
let _apiDiscoveredAt = 0;
let _lastGoodData = [];
let _lastGoodTs = 0;
let _lastFetchOk = false;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,X-Relay-Token,Authorization",
};

const STREAM_HDR = {
  "User-Agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
  Referer: "https://sv2.tieulam.info/",
  Origin: "https://sv2.tieulam.info",
  Accept: "*/*",
};

const STREAM_ALLOWED = ["asynccdn.com", "lilive1.eu.cc", "lilive3.eu.cc"];

function allowedStreamUrl(u) {
  try {
    const h = new URL(u).hostname.toLowerCase();
    return STREAM_ALLOWED.some((x) => h.includes(x));
  } catch (_) {
    return false;
  }
}

async function fetchLiveUrls(env, matchId) {
  const base = await discoverApiBase(env, false);
  const r = await fetch(`${base}/match/${matchId}/live`, {
    headers: TIEULAM_HDR,
    signal: AbortSignal.timeout(FETCH_TIMEOUT),
  });
  if (!r.ok) return null;
  const data = await r.json();
  return {
    hd1: data.hd_1 || "",
    hd2: data.hd_2 || "",
    hd3: data.hd_3 || "",
    source: data.source || "",
  };
}

function viCandidates(live) {
  const out = [];
  const seen = new Set();
  for (const u of [live.hd1, live.hd2, live.hd3]) {
    if (u && !seen.has(u)) {
      seen.add(u);
      out.push(u);
    }
  }
  return out;
}

async function fetchStreamUpstream(url) {
  return fetch(url, {
    headers: STREAM_HDR,
    cf: { cacheTtl: 0 },
    signal: AbortSignal.timeout(FETCH_TIMEOUT),
  });
}

function rewriteM3u8(text, upstreamUrl, workerOrigin) {
  const base = upstreamUrl.replace(/\/[^/]*$/, "/");
  const proxy = (raw) => {
    const abs = raw.startsWith("http") ? raw : new URL(raw.trim(), base).href;
    return `${workerOrigin}/stream/proxy?u=${encodeURIComponent(abs)}`;
  };
  return (
    text
      .split("\n")
      .map((line) => {
        const s = line.trim();
        if (!s) return line;
        if (s.startsWith("#")) {
          const m = s.match(/URI="([^"]+)"/);
          if (m) return line.replace(m[0], `URI="${proxy(m[1])}"`);
          return line;
        }
        return proxy(s);
      })
      .join("\n") + "\n"
  );
}

async function handleStreamMatch(req, env, matchId) {
  const live = await fetchLiveUrls(env, matchId);
  if (!live) {
    return new Response("Live API failed", { status: 502, headers: CORS });
  }

  const workerOrigin = new URL(req.url).origin;
  for (const url of viCandidates(live)) {
    const r = await fetchStreamUpstream(url);
    if (!r.ok) continue;
    const body = await r.text();
    if (!body.includes("#EXTM3U")) continue;
    const rewritten = rewriteM3u8(body, url, workerOrigin);
    return new Response(rewritten, {
      status: 200,
      headers: {
        ...CORS,
        "Content-Type": "application/vnd.apple.mpegurl",
        "Cache-Control": "no-cache",
      },
    });
  }

  if (live.source) {
    return Response.redirect(live.source, 302);
  }
  return new Response("No stream", { status: 502, headers: CORS });
}

async function handleStreamProxy(req, url) {
  const raw = url.searchParams.get("u") || "";
  if (!raw.startsWith("http") || !allowedStreamUrl(raw)) {
    return new Response("Bad URL", { status: 400, headers: CORS });
  }

  const r = await fetchStreamUpstream(raw);
  if (!r.ok) {
    return new Response(`Upstream ${r.status}`, { status: 502, headers: CORS });
  }

  const ct = r.headers.get("Content-Type") || "";
  const body = await r.arrayBuffer();
  const workerOrigin = new URL(req.url).origin;
  const head = new TextDecoder().decode(body.slice(0, 256));
  const isM3u8 =
    ct.includes("mpegurl") || raw.endsWith(".m3u8") || head.includes("#EXTM3U");

  if (isM3u8) {
    const text = new TextDecoder().decode(body);
    const rewritten = rewriteM3u8(text, raw, workerOrigin);
    return new Response(rewritten, {
      headers: {
        ...CORS,
        "Content-Type": "application/vnd.apple.mpegurl",
        "Cache-Control": "no-cache",
      },
    });
  }

  let mt = ct || "application/octet-stream";
  if (raw.endsWith(".ts")) mt = "video/mp2t";
  return new Response(body, {
    headers: { ...CORS, "Content-Type": mt, "Cache-Control": "no-cache" },
  });
}

async function handleStreamRequest(req, url, env) {
  const path = url.pathname;

  const m = path.match(/^\/stream\/match\/([^/.]+)\.m3u8$/);
  if (m) return handleStreamMatch(req, env, m[1]);

  if (path === "/stream/proxy") return handleStreamProxy(req, url);

  if (path === "/stream/test") {
    const testUrl = url.searchParams.get("u") || "";
    if (!testUrl) {
      return jsonResp({ error: "missing u param" }, 400);
    }
    const r = await fetchStreamUpstream(testUrl);
    const body = await r.text();
    return jsonResp({
      ok: r.ok,
      status: r.status,
      is_m3u8: body.includes("#EXTM3U"),
      preview: body.slice(0, 120),
      worker: WORKER_VERSION,
    });
  }

  return new Response("Not found", { status: 404, headers: CORS });
}

function buildDomains() {
  const vnMs = Date.now() + 7 * 3_600_000;
  const domains = [];
  for (let i = 0; i <= 7; i++) {
    const d = new Date(vnMs + i * 86_400_000);
    domains.push(fmtDomain(d));
  }
  for (let i = 1; i <= 14; i++) {
    const d = new Date(vnMs - i * 86_400_000);
    domains.push(fmtDomain(d));
  }
  return [...new Set(domains)];
}

function fmtDomain(d) {
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const yyyy = d.getUTCFullYear();
  return `https://api.tlap${dd}${mm}${yyyy}.com`;
}

function jsonResp(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

async function cacheGet() {
  try {
    const cached = await caches.default.match(new Request(CF_CACHE_KEY));
    if (!cached) return null;
    const body = await cached.json();
    if (body?.data) return body;
  } catch (_) {}
  return null;
}

async function cachePut(data, apiBase) {
  try {
    const body = JSON.stringify({ ts: Date.now(), data, api_base: apiBase });
    await caches.default.put(
      new Request(CF_CACHE_KEY),
      new Response(body, {
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": `public, max-age=${Math.floor(KV_STALE_MS / 1000)}`,
        },
      })
    );
  } catch (_) {}
}

function stalePayload(extra = {}) {
  if (_lastGoodData.length) {
    return {
      data: _lastGoodData,
      count: _lastGoodData.length,
      cached: true,
      stale: true,
      ...extra,
    };
  }
  return null;
}

async function discoverFromFrontend() {
  for (const front of TIEULAM_FRONTS) {
    try {
      const html = await fetch(front, { signal: AbortSignal.timeout(6000) }).then((r) => r.text());
      for (const m of html.matchAll(/src="(\/assets\/[^"]+\.js)"/g)) {
        try {
          const js = await fetch(front + m[1], { signal: AbortSignal.timeout(10000) }).then((r) =>
            r.text()
          );
          const hit =
            js.match(/create\(\{baseURL:"(https:\/\/api\.tlap[^"]+)"\}/) ||
            js.match(/"(https:\/\/api\.tlap[\w]+\.com)"/);
          if (hit) return hit[1];
        } catch (_) {}
      }
    } catch (_) {}
  }
  return null;
}

async function probeDomains(domains, limit = 3) {
  const candidates = domains.slice(0, limit);
  const controllers = candidates.map(() => new AbortController());
  const probes = candidates.map((base, i) =>
    fetch(base + "/matches/graph", {
      method: "POST",
      headers: TIEULAM_HDR,
      body: JSON.stringify({ queries: [], limit: 1, page: 1 }),
      signal: controllers[i].signal,
    }).then((r) => {
      if (r.ok || r.status === 422) {
        controllers.forEach((c, j) => j !== i && c.abort());
        return base;
      }
      throw new Error(`HTTP ${r.status}`);
    })
  );
  try {
    return await Promise.any(probes);
  } catch (_) {
    return null;
  }
}

async function discoverApiBase(env, force = false) {
  if (env.TIEULAM_API_OVERRIDE) {
    _apiBase = env.TIEULAM_API_OVERRIDE.replace(/\/$/, "");
    _apiDiscoveredAt = Date.now();
    return _apiBase;
  }

  if (!_apiBase) _apiBase = buildDomains()[0];
  if (!force && Date.now() - _apiDiscoveredAt < API_DISC_MS) return _apiBase;

  const fromFront = await discoverFromFrontend();
  if (fromFront) {
    _apiBase = fromFront;
    _apiDiscoveredAt = Date.now();
    return _apiBase;
  }

  const found = await probeDomains(buildDomains(), 3);
  if (found) {
    _apiBase = found;
    _apiDiscoveredAt = Date.now();
    return _apiBase;
  }

  _apiDiscoveredAt = Date.now() - API_DISC_MS + 300_000;
  return _apiBase;
}

async function fetchMatches(reqBody, env, forceDiscover = false) {
  const base = await discoverApiBase(env, forceDiscover);
  const r = await fetch(base + "/matches/graph", {
    method: "POST",
    headers: TIEULAM_HDR,
    body: JSON.stringify(reqBody),
    signal: AbortSignal.timeout(FETCH_TIMEOUT),
  });

  if (!r.ok) {
    _apiDiscoveredAt = 0;
    return { ok: false, status: r.status, base };
  }

  const d = await r.json();
  const data = Array.isArray(d) ? d : d.data || d.matches || [];
  _lastGoodData = data;
  _lastGoodTs = Date.now();
  _lastFetchOk = true;
  await cachePut(data, base);
  return { ok: true, data, base };
}

async function getMatchesResponse(req, env) {
  if (_lastGoodData.length && Date.now() - _lastGoodTs < DATA_FRESH_MS) {
    return jsonResp({ data: _lastGoodData, count: _lastGoodData.length, cached: true, worker: WORKER_VERSION });
  }

  const cfCache = await cacheGet();
  if (cfCache?.data?.length && Date.now() - cfCache.ts < KV_STALE_MS) {
    _lastGoodData = cfCache.data;
    _lastGoodTs = Date.now();
    return jsonResp({
      data: cfCache.data,
      count: cfCache.data.length,
      cached: true,
      from_cf_cache: true,
      api_base: cfCache.api_base,
      worker: WORKER_VERSION,
    });
  }

  let reqBody = { queries: [], limit: 50, page: 1 };
  try {
    reqBody = await req.clone().json();
  } catch (_) {}

  let result = await fetchMatches(reqBody, env, false);
  if (!result.ok) {
    result = await fetchMatches(reqBody, env, true);
  }

  if (result.ok) {
    return jsonResp({
      data: result.data,
      count: result.data.length,
      api_base: result.base,
      worker: WORKER_VERSION,
    });
  }

  const mem = stalePayload({ upstream_status: result.status, api_base: result.base });
  if (mem) return jsonResp({ ...mem, worker: WORKER_VERSION });

  if (cfCache?.data?.length) {
    return jsonResp({
      data: cfCache.data,
      count: cfCache.data.length,
      cached: true,
      stale: true,
      from_cf_cache: true,
      upstream_status: result.status,
      worker: WORKER_VERSION,
    });
  }

  return jsonResp({
    data: [],
    count: 0,
    error: `upstream_${result.status}`,
    api_base: result.base,
    worker: WORKER_VERSION,
  });
}

async function handleRequest(req, env) {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

  const url = new URL(req.url);
  const path = url.pathname;

  // Public stream proxy — IPTV clients cannot send relay token
  if (path.startsWith("/stream/")) {
    return handleStreamRequest(req, url, env);
  }

  const secret = env.RELAY_SECRET || "";
  const token = req.headers.get("X-Relay-Token") || url.searchParams.get("token") || "";
  if (secret && token !== secret) return jsonResp({ error: "Unauthorized" }, 401);

  if (path === "/healthz" || path === "/test-env") {
    const domains = buildDomains();
    const probeResults = await Promise.allSettled(
      domains.slice(0, 3).map((base) =>
        fetch(base + "/matches/graph", {
          method: "POST",
          headers: TIEULAM_HDR,
          body: JSON.stringify({ queries: [], limit: 1, page: 1 }),
          signal: AbortSignal.timeout(PROBE_TIMEOUT),
        }).then((r) => ({ base, status: r.status, ok: r.ok || r.status === 422 }))
      )
    );
    const cfCache = await cacheGet();
    return jsonResp({
      ok: true,
      worker: WORKER_VERSION,
      env: {
        relay_secret_set: !!secret,
        relay_secret_len: secret.length,
        tieulam_api_override: !!env.TIEULAM_API_OVERRIDE,
      },
      last_fetch_ok: _lastFetchOk,
      domains_today_first: domains.slice(0, 3),
      probe_results: probeResults.map((p) =>
        p.status === "fulfilled" ? p.value : { error: String(p.reason?.message || p.reason) }
      ),
      memory_cache_size: _lastGoodData.length,
      cf_cache_ts: cfCache?.ts ?? null,
      cf_cache_size: cfCache?.data?.length ?? 0,
      current_api_base: _apiBase,
    });
  }

  if (path === "/status") {
    const cfCache = await cacheGet();
    return jsonResp({
      worker: WORKER_VERSION,
      api_base: _apiBase,
      discovered_at: _apiDiscoveredAt,
      last_fetch_ok: _lastFetchOk,
      memory_cache: _lastGoodData.length,
      cf_cache_age_ms: cfCache ? Date.now() - cfCache.ts : null,
      cf_cache_size: cfCache?.data?.length ?? 0,
    });
  }

  return getMatchesResponse(req, env);
}

async function safeResponse(req, env) {
  try {
    return await handleRequest(req, env);
  } catch (err) {
    _apiDiscoveredAt = 0;
    const mem = stalePayload({ error: err.message });
    if (mem) return jsonResp({ ...mem, worker: WORKER_VERSION });
    const cfCache = await cacheGet();
    if (cfCache?.data?.length) {
      return jsonResp({
        data: cfCache.data,
        count: cfCache.data.length,
        cached: true,
        stale: true,
        from_cf_cache: true,
        error: err.message,
        worker: WORKER_VERSION,
      });
    }
    return jsonResp({ data: [], count: 0, error: err.message, worker: WORKER_VERSION });
  }
}

export default {
  async fetch(req, env) {
    return safeResponse(req, env);
  },
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        try {
          await fetchMatches({ queries: [], limit: 50, page: 1 }, env, false);
        } catch (_) {}
      })()
    );
  },
};
