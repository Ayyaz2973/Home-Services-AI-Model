const FORWARDED_REQUEST_HEADERS = new Set([
  "accept",
  "authorization",
  "content-type",
  "x-admin-key",
  "x-phone",
  "x-pin",
]);

const FORWARDED_RESPONSE_HEADERS = [
  "cache-control",
  "content-disposition",
  "content-type",
  "x-content-type-options",
];

module.exports = async function handler(req, res) {
  const backendUrl = process.env.BACKEND_URL;
  if (!backendUrl) {
    res.statusCode = 503;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ detail: "The portal backend is not configured yet." }));
    return;
  }

  let target;
  try {
    const backend = new URL(backendUrl);
    if (backend.protocol !== "https:") {
      throw new Error("BACKEND_URL must use HTTPS");
    }
    target = new URL(req.url, backend);
    if (target.origin !== backend.origin) {
      throw new Error("Invalid backend request target");
    }
  } catch (error) {
    console.error("Invalid BACKEND_URL configuration:", error);
    res.statusCode = 500;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ detail: "The portal backend URL is invalid." }));
    return;
  }

  const headers = new Headers();
  for (const [name, value] of Object.entries(req.headers)) {
    if (FORWARDED_REQUEST_HEADERS.has(name.toLowerCase()) && typeof value === "string") {
      headers.set(name, value);
    }
  }

  const options = { method: req.method, headers, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    options.body = req;
    options.duplex = "half";
  }

  let upstream;
  try {
    upstream = await fetch(target, options);
  } catch (error) {
    console.error("Portal backend request failed:", error);
    res.statusCode = 502;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ detail: "The portal backend could not be reached." }));
    return;
  }

  res.statusCode = upstream.status;
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) res.setHeader(name, value);
  }

  if (req.method === "HEAD" || !upstream.body) {
    res.end();
    return;
  }

  res.end(Buffer.from(await upstream.arrayBuffer()));
};

module.exports.config = {
  api: {
    bodyParser: false,
  },
};
