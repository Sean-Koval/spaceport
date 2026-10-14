import createClient from 'openapi-fetch';
import type { paths, components } from './generated/api';
import { matchesSchema, record } from './form-schema';
export type Model<K extends keyof components['schemas']> = components['schemas'][K];
const messages: Record<string, string> = {
  UNAUTHENTICATED: 'Your session has expired. Sign in again.',
  NOT_FOUND_OR_FORBIDDEN: 'This item is unavailable to this account.',
  CONFLICT: 'This revision changed. Reload and review the current version before continuing.',
  INVALID_INPUT: 'The gateway could not validate these values. Your edits are preserved.',
  RESOURCE_LIMIT: 'A size or rate limit was reached. Reduce the request or try again later.',
  UPSTREAM_UNAVAILABLE: 'The gateway or source is temporarily unavailable.',
  BINDING_DRIFT: 'The approved binding needs review.',
  EXECUTION_TIMEOUT: 'The execution reached its time limit.',
  FRESHNESS_UNKNOWN: 'Source freshness could not be verified.',
  STALE_DATA: 'The source data does not meet this product’s freshness requirement.',
};
export class GatewayError extends Error {
  constructor(
    public status: number,
    public code: string,
    public requestId?: string,
  ) {
    super(messages[code] ?? 'The request could not be completed safely.');
    this.name = 'GatewayError';
  }
}
export class GatewayClient {
  private active = true;
  private controller = new AbortController();
  private http: ReturnType<typeof createClient<paths>>;
  constructor(bearer: string, onUnauthorized: () => void, fetcher: typeof fetch = fetch) {
    this.http = createClient<paths>({
      baseUrl: window.location.origin,
      fetch: async (request) => {
        if (!this.active) throw new DOMException('Session ended', 'AbortError');
        const headers = new Headers(request.headers);
        headers.set('Authorization', `Bearer ${bearer}`);
        headers.set('Accept', 'application/json');
        const response = await fetcher(
          new Request(request, {
            headers,
            signal: AbortSignal.any([request.signal, this.controller.signal]),
            cache: 'no-store',
            credentials: 'omit',
          }),
        );
        if (!this.active) throw new DOMException('Session ended', 'AbortError');
        if (response.status === 401) {
          this.dispose();
          onUnauthorized();
        }
        return response;
      },
    });
  }
  dispose() {
    this.active = false;
    this.controller.abort();
  }
  private async checked<T>(
    promise: Promise<{ data?: T; error?: unknown; response: Response }>,
    schema?: string,
  ): Promise<T> {
    const result = await promise;
    if (!result.response.ok) {
      const body = record(result.error) ? result.error : {};
      const code =
        typeof body.code === 'string' && body.code in messages ? body.code : 'UPSTREAM_UNAVAILABLE';
      const requestId =
        typeof body.request_id === 'string' && /^[A-Za-z0-9_.-]{1,128}$/.test(body.request_id)
          ? body.request_id
          : undefined;
      throw new GatewayError(result.response.status, code, requestId);
    }
    if (!this.active) throw new DOMException('Session ended', 'AbortError');
    if (result.data === undefined || (schema && !matchesSchema(result.data, schema)))
      throw new GatewayError(503, 'UPSTREAM_UNAVAILABLE');
    return result.data;
  }
  catalog(q = '', cursor?: string, signal?: AbortSignal) {
    return this.checked(
      this.http.GET('/v1/products', { params: { query: { q, cursor, limit: 50 } }, signal }),
      'CatalogPage',
    );
  }
  product(id: string, version?: string, signal?: AbortSignal) {
    return this.checked(
      this.http.GET('/v1/products/{product_id}', {
        params: { path: { product_id: id }, query: { version } },
        signal,
      }),
      'ProductDescription',
    );
  }
  draft(id: string, signal?: AbortSignal) {
    return this.checked(
      this.http.GET('/v1/drafts/{draft_id}', { params: { path: { draft_id: id } }, signal }),
      'DraftView',
    );
  }
  diff(id: string, signal?: AbortSignal) {
    return this.checked(
      this.http.GET('/v1/drafts/{draft_id}/diff', { params: { path: { draft_id: id } }, signal }),
      'VersionDiff',
    );
  }
  testRun(id: string, runId: string, signal?: AbortSignal) {
    return this.checked(
      this.http.GET('/v1/drafts/{draft_id}/tests/{run_id}', {
        params: { path: { draft_id: id, run_id: runId } },
        signal,
      }),
      'TestRunView',
    );
  }
  bindings(draft_id?: string, cursor?: string, signal?: AbortSignal) {
    return this.checked(
      this.http.GET('/v1/bindings', { params: { query: { draft_id, cursor, limit: 50 } }, signal }),
      'BindingChoicePage',
    );
  }
  policies(draft_id?: string, cursor?: string, signal?: AbortSignal) {
    return this.checked(
      this.http.GET('/v1/policies', { params: { query: { draft_id, cursor, limit: 50 } }, signal }),
      'PolicyChoicePage',
    );
  }
  saveDraft(body: Model<'DraftWrite'>, id?: string) {
    return id
      ? this.checked(
          this.http.PUT('/v1/drafts/{draft_id}', { params: { path: { draft_id: id } }, body }),
          'DraftReceipt',
        )
      : this.checked(this.http.POST('/v1/drafts', { body }), 'DraftReceipt');
  }
  testDraft(id: string) {
    return this.checked(
      this.http.POST('/v1/drafts/{draft_id}/tests', { params: { path: { draft_id: id } } }),
      'TestRunView',
    );
  }
  publish(id: string, body: Model<'PublishRequest'>) {
    return this.checked(
      this.http.POST('/v1/drafts/{draft_id}/publish', { params: { path: { draft_id: id } }, body }),
      'PublicationReceipt',
    );
  }
  lifecycle(id: string, body: Model<'LifecycleRequest'>) {
    return this.checked(
      this.http.POST('/v1/products/{product_id}/lifecycle', {
        params: { path: { product_id: id } },
        body,
      }),
      'Changed',
    );
  }
  execute(body: Model<'QueryRequest'>) {
    return this.checked(this.http.POST('/v1/executions', { body }), 'ExecutionView');
  }
  async execution(id: string, cursor?: string, signal?: AbortSignal) {
    const value = await this.checked(
      this.http.GET('/v1/executions/{execution_id}', {
        params: { path: { execution_id: id }, query: { cursor } },
        signal,
      }),
    );
    if (!matchesSchema(value, 'ResultEnvelope') && !matchesSchema(value, 'ExecutionView'))
      throw new GatewayError(503, 'UPSTREAM_UNAVAILABLE');
    return value;
  }
}
