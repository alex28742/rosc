// Pre-proxy HTTP Host guard for the Vite dev and preview servers (P010.2).
// Vite's proxy rewrites Host before the backend sees it, so the ORIGINAL authority must be checked here.
// This is local gateway hygiene against DNS rebinding / malformed authorities. It is NOT authentication.
// Mirrors the strict semantics of rosc.app.host_name (P010.1) but is deliberately narrower: only the literal
// loopback names are accepted, never arbitrary IPv6 spellings.

const AUTHORITY = /^(?:(?:localhost|127\.0\.0\.1)|\[::1\])(?::([0-9]{1,5}))?$/i;

export function isAllowedHostAuthority(value: unknown): boolean {
  if (typeof value !== 'string') return false;
  const match = AUTHORITY.exec(value);
  if (!match) return false;
  if (match[1] !== undefined) {
    const port = Number(match[1]);
    if (!(port >= 1 && port <= 65535)) return false;
  }
  return true;
}

/** Exactly one Host header on the wire (rawHeaders keeps duplicates that `headers.host` collapses). */
export function hostHeaderValues(rawHeaders: readonly string[]): string[] {
  const values: string[] = [];
  for (let i = 0; i + 1 < rawHeaders.length; i += 2) {
    if (rawHeaders[i].toLowerCase() === 'host') values.push(rawHeaders[i + 1]);
  }
  return values;
}

export interface GuardRequest { headers: Record<string, string | string[] | undefined>; rawHeaders: readonly string[] }
export interface GuardResponse {
  statusCode: number;
  setHeader(name: string, value: string): unknown;
  end(body?: string): unknown;
}

export function isAllowedRequestHost(req: GuardRequest): boolean {
  const hosts = hostHeaderValues(req.rawHeaders);
  return hosts.length === 1 && isAllowedHostAuthority(hosts[0]);
}

export function hostGuard(req: GuardRequest, res: GuardResponse, next: () => void): void {
  if (isAllowedRequestHost(req)) {
    next();
    return;
  }
  res.statusCode = 403;
  res.setHeader('Content-Type', 'application/json');
  res.end('{"detail":"Host is not allowed"}');
}

export function hostGuardPlugin() {
  return {
    name: 'rosc-original-host-guard',
    // Registered directly (not in a returned post-hook) so it runs before Vite's own middlewares, incl. the /api proxy.
    configureServer(server: { middlewares: { use(fn: typeof hostGuard): unknown } }) {
      server.middlewares.use(hostGuard);
    },
    configurePreviewServer(server: { middlewares: { use(fn: typeof hostGuard): unknown } }) {
      server.middlewares.use(hostGuard);
    },
  };
}
