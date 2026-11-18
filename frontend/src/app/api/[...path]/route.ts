import { NextRequest, NextResponse } from "next/server";

const allowed = new Map([
  ["auth/login", "POST"],
  ["auth/logout", "POST"],
  ["auth/me", "GET"],
  ["system/status", "GET"],
  ["system/events", "GET"],
  ["exchanges", "GET"],
  ["market-data/status", "GET"],
  ["opportunities", "GET"],
  ["opportunities/diagnostics", "GET"],
  ["opportunities/budgets", "GET"],
  ["settings/defaults", "GET"],
  ["reports/contract", "GET"],
  ["paper/sessions", "GET,POST"],
  ["paper/trades", "GET"],
  ["strategies", "GET"],
  ["terminal/overview", "GET"],
  ["terminal/health", "GET"],
  ["analytics/strategy-lab/compare", "POST"],
  ["settings/terminal", "GET,PUT"],
  ["reports/export", "GET"],
  ["reports/exports", "POST"],
  ["reports/exports/current", "GET"],
  ["paper/funding/catalog", "GET"],
  ["paper/funding/coverage", "GET"],
  ["paper/funding/proposal", "POST"],
  ["paper/funding/compare", "POST"],
  ["paper/funding/experiments", "POST"],
  ["paper/funding/cohort-plan", "POST"],
  ["paper/funding/cohorts", "POST"],
  ["paper/replay", "POST"],
  ["notifications/status", "GET"],
  ["notifications/history", "GET"],
  ["notifications/preview", "GET"],
  ["notifications/pairing/challenge", "POST"],
  ["notifications/pairing/approve", "POST"],
  ["notifications/pairing/revoke", "POST"],
  ["notifications/test", "POST"],
  ["notifications/pause", "POST"],
  ["notifications/resume", "POST"],
  ["ai/status", "GET"],
  ["ai/explain", "POST"],
]);
async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const path = (await context.params).path.join("/");
  const dynamicMethod =
    /^reports\/exports\/[0-9a-f-]{36}(\/download)?$/.test(path) ||
    /^exchanges\/[a-z0-9_-]+\/(markets|quality)$/.test(path) ||
    /^markets\/[0-9a-f-]{36}\/book$/.test(path) ||
    /^opportunities\/[0-9a-f-]{36}$/.test(path) ||
    /^opportunities\/[0-9a-f-]{36}\/size-diagnostics$/.test(path) ||
    /^paper\/sessions\/[0-9a-f-]{36}\/(portfolio|metrics|daily)$/.test(path) ||
    /^paper\/trades\/[0-9a-f-]{36}$/.test(path) ||
    /^analytics\/sessions\/[0-9a-f-]{36}(\/series)?$/.test(path)
      ? "GET"
      : /^reports\/exports\/[0-9a-f-]{36}\/cancel$/.test(path) ||
          /^markets\/[0-9a-f-]{36}\/confirm$/.test(path) ||
          /^opportunities\/[0-9a-f-]{36}\/recheck$/.test(path)
        ? "POST"
        : /^notifications\/[0-9a-f-]{36}\/(acknowledge|cancel)$/.test(path)
          ? "POST"
          : /^paper\/sessions\/[0-9a-f-]{36}\/(pause|resume|archive|rebalance(?:\/preview)?)$/.test(
                path,
              )
            ? "POST"
            : /^paper\/sessions\/[0-9a-f-]{36}\/strategy$/.test(path)
              ? "PUT"
              : undefined;
  const method = allowed.get(path) || dynamicMethod;
  if (!method) return NextResponse.json({ detail: "Endpoint not found" }, { status: 404 });
  if (!method.split(",").includes(request.method))
    return NextResponse.json({ detail: "Method not allowed" }, { status: 405 });
  // Production may terminate TLS at the host proxy. Trust only a configured canonical
  // URL, never a client-supplied forwarded host or protocol.
  let expectedOrigin = `${request.nextUrl.protocol}//${request.headers.get("host")}`;
  if (process.env.PUBLIC_APP_URL) {
    try {
      const canonical = new URL(process.env.PUBLIC_APP_URL);
      if (
        !["https:", "http:"].includes(canonical.protocol) ||
        canonical.username ||
        canonical.password ||
        canonical.pathname !== "/" ||
        canonical.search ||
        canonical.hash
      )
        throw new Error("Invalid canonical URL");
      expectedOrigin = canonical.origin;
    } catch {
      return NextResponse.json(
        { detail: "Application origin is not configured correctly" },
        { status: 503 },
      );
    }
  }
  if (
    ["POST", "PUT"].includes(request.method) &&
    request.headers.get("origin") !== expectedOrigin
  ) {
    return NextResponse.json({ detail: "Same-origin request required" }, { status: 403 });
  }
  const maximumBody =
    path.startsWith("paper/funding/") || path === "settings/terminal" ? 32768 : 8192;
  if (Number(request.headers.get("content-length") || 0) > maximumBody) {
    return NextResponse.json({ detail: "Request too large" }, { status: 413 });
  }
  const body = ["POST", "PUT"].includes(request.method) ? await request.text() : undefined;
  if (body && new TextEncoder().encode(body).byteLength > maximumBody)
    return NextResponse.json({ detail: "Request too large" }, { status: 413 });
  try {
    const upstream = await fetch(
      `${process.env.BACKEND_URL || "http://127.0.0.1:8000"}/api/v1/${path}${request.nextUrl.search}`,
      {
        method: request.method,
        headers: {
          "Content-Type": "application/json",
          Cookie: request.headers.get("cookie") || "",
          ...(request.headers.get("origin") ? { Origin: request.headers.get("origin")! } : {}),
        },
        body,
        cache: "no-store",
        // Generation is a background job. A completed archive can take minutes to
        // download on mobile: do not abort its body after the ordinary JSON timeout.
        signal: /reports\/exports\/[0-9a-f-]{36}\/download$/.test(path)
          ? request.signal
          : AbortSignal.timeout(
              path === "reports/export"
                ? 120000
                : /^(paper\/funding\/(proposal|compare))$/.test(path)
                  ? 30000
                  : 10000,
            ),
        redirect: "error",
      },
    );
    const headers = new Headers({
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    });
    for (const cookie of upstream.headers.getSetCookie()) headers.append("Set-Cookie", cookie);
    const retryAfter = upstream.headers.get("retry-after");
    if (retryAfter) headers.set("Retry-After", retryAfter);
    if (path === "opportunities") {
      const timings = upstream.headers.get("server-timing");
      if (timings) headers.set("Server-Timing", timings);
    }
    if (
      (path === "reports/export" || /^reports\/exports\/[0-9a-f-]{36}\/download$/.test(path)) &&
      upstream.ok
    ) {
      headers.set("Content-Type", "application/zip");
      const disposition = upstream.headers.get("Content-Disposition") || "";
      headers.set(
        "Content-Disposition",
        /^attachment; filename="quantix-(analysis|everything)[-0-9a-zTZ.]+\.zip"$/i.test(
          disposition,
        )
          ? disposition
          : 'attachment; filename="quantix-analysis.zip"',
      );
      const digest = upstream.headers.get("X-Content-SHA256");
      if (digest && /^[a-f0-9]{64}$/i.test(digest)) headers.set("X-Content-SHA256", digest);
      const length = upstream.headers.get("Content-Length");
      if (length && /^\d+$/.test(length)) headers.set("Content-Length", length);
    }
    // Forward the stream directly; a report ZIP is never buffered as a text/blob copy.
    return new NextResponse(upstream.body, { status: upstream.status, headers });
  } catch {
    return NextResponse.json(
      { detail: "Backend unavailable. Check System Health and service logs." },
      { status: 503 },
    );
  }
}
export { proxy as GET, proxy as POST, proxy as PUT };
