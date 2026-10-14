import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SafeLinks, ConnectorStatus, Value } from '../metadata';

describe('inert authorized metadata', () => {
  it('renders typed values exactly without executing markup', () => {
    render(
      <>
        <Value value={'<script data-injected>alert(1)</script>'} />
        <Value value={null} />
        <Value value="" />
        <Value value="9007199254741023.31" />
        <Value value={false} />
        <Value value={0} />
      </>,
    );
    expect(screen.getByText('"<script data-injected>alert(1)</script>"')).toBeVisible();
    for (const text of ['null', '""', '"9007199254741023.31"', 'false', '0'])
      expect(screen.getByText(text)).toBeVisible();
    expect(document.querySelector('script[data-injected]')).toBeNull();
  });
  it('links only credential-free HTTPS with safe new-tab attributes', () => {
    const unsafe = [
      'javascript:alert(1)',
      'data:text/html,hi',
      'http://example.com',
      'https://a:b@example.com',
      'https://example.com/?token=private',
      'https://example.com/?API_KEY=secret',
      'https://example.com/\npath',
      'not a URL',
    ];
    render(
      <SafeLinks
        links={[
          { label: 'Docs', url: 'https://example.com/docs' },
          ...unsafe.map((url, i) => ({ label: `Unsafe ${i}`, url })),
        ]}
      />,
    );
    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'Docs' })).toHaveAttribute(
      'rel',
      'noopener noreferrer',
    );
    expect(screen.getByRole('link', { name: 'Docs' })).toHaveAttribute(
      'referrerpolicy',
      'no-referrer',
    );
  });
  it('does not present mocked or configured connectors as live verification', () => {
    render(<ConnectorStatus />);
    expect(screen.getByText(/Live verification not reported by this gateway/)).toBeVisible();
    expect(screen.getByText(/BigQuery: live execution unavailable/)).toBeVisible();
    expect(screen.getByText(/Domo: unavailable/)).toBeVisible();
    expect(screen.queryByRole('button', { name: /connect/i })).toBeNull();
  });
});
