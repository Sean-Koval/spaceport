/** Inert presentation only. Never fetch metadata URLs or interpret supplied markup. */
export function safeHttps(url: string): boolean {
  if (/\s|[\u0000-\u001f\u007f]/.test(url)) return false;
  try {
    const value = new URL(url);
    return (
      value.protocol === 'https:' &&
      !!value.hostname &&
      !value.username &&
      !value.password &&
      ![...value.searchParams.keys()].some((key) =>
        /^(password|token|access_token|api_key|secret|authorization)$/i.test(key),
      )
    );
  } catch {
    return false;
  }
}
export function SafeLinks({ links }: { links: { label: string; url: string }[] }) {
  return (
    <ul className="links">
      {links
        .filter((link) => safeHttps(link.url))
        .map((link, i) => (
          <li key={i}>
            <a
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              referrerPolicy="no-referrer"
            >
              {link.label}
            </a>
          </li>
        ))}
    </ul>
  );
}
export function ConnectorStatus() {
  return (
    <section className="panel support" aria-labelledby="connector-status">
      <h2 id="connector-status">Connector readiness</h2>
      <p>Live verification not reported by this gateway.</p>
      <ul>
        <li>Synthetic PostgreSQL / Cube: configured bindings do not prove live verification.</li>
        <li>
          BigQuery: live execution unavailable. Mock contract evidence is build-time evidence only.
        </li>
        <li>Domo: unavailable (Phase 1b).</li>
      </ul>
      <p className="muted">
        Configured, mock-tested, live-verified and unavailable are separate facts. Approved choices
        below never certify a source connection.
      </p>
    </section>
  );
}
export function Value({ value }: { value: unknown }) {
  return (
    <pre className="typed-value">
      {value === undefined ? 'Not provided in this view' : JSON.stringify(value, null, 2)}
    </pre>
  );
}
export function Rows({
  rows,
  caption,
}: {
  rows: readonly Record<string, unknown>[];
  caption: string;
}) {
  const fields = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  if (!rows.length) return <p>{caption}: zero rows.</p>;
  return (
    <div className="table-scroll" role="region" aria-label={caption} tabIndex={0}>
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Row</th>
            {fields.map((field) => (
              <th scope="col" key={field}>
                {field}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <th scope="row">{index + 1}</th>
              {fields.map((field) => (
                <td key={field}>
                  <Value value={row[field]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
