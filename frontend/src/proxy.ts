import { NextRequest, NextResponse } from "next/server";

/** Stamp the actual local destination; an incoming client header cannot override it. */
export function proxy(request: NextRequest) {
  const headers = new Headers(request.headers);
  headers.set("x-quantix-return-path", `${request.nextUrl.pathname}${request.nextUrl.search}`);
  return NextResponse.next({ request: { headers } });
}

export const config = { matcher: ["/((?!api|_next|brand|favicon.ico).*)"] };
