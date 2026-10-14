import { render, screen, within } from '@testing-library/react';
import { it, expect } from 'vitest';
import userEvent from '@testing-library/user-event';
import { FixtureChecks, DefinitionDiff, StaleEvidence } from '../pages/Review';
import { diff, run } from './review-fixtures';
it('orders failed checks first, preserves exact authorized values and distinguishes absent tolerances', async () => {
  render(
    <FixtureChecks
      run={{
        ...run,
        status: 'failed',
        checks: [run.checks[0], { ...run.checks[1], fixture_id: 'failure', passed: false }],
      }}
    />,
  );
  const checks = screen.getAllByRole('article');
  expect(within(checks[0]).getByRole('heading')).toHaveTextContent('failure');
  expect(screen.getAllByText('"9007199254741023.31"')).toHaveLength(2);
  expect(screen.getAllByText(/not provided in this view/i).length).toBeGreaterThan(0);
  await userEvent.click(within(checks[1]).getByText('Tolerances and watermark evidence'));
  expect(within(checks[1]).getByText('Fixture reference watermark')).toBeVisible();
  expect(within(checks[1]).getByText('Observed source-load watermark')).toBeVisible();
});
it('labels verified permission-negative checks as expected denials', () => {
  render(<FixtureChecks run={run} />);
  expect(screen.getByText('Expected access denial verified')).toBeVisible();
});
it('renders server differences without confusing absent, null, empty or ordered arrays', () => {
  render(
    <DefinitionDiff
      diff={{
        ...diff,
        changes: [
          { path: '/name', kind: 'changed', before: '', after: null },
          {
            path: '/description',
            kind: 'removed',
            before: '<script data-injected>bad</script>',
            after: null,
          },
          { path: '/inputs/a~1b', kind: 'added', before: null, after: ['second', 'first'] },
        ],
      }}
    />,
  );
  expect(screen.getByText('Not present')).toBeVisible();
  expect(screen.getByText('Removed')).toBeVisible();
  expect(screen.getByText('""')).toBeVisible();
  expect(screen.getByText('null')).toBeVisible();
  expect(screen.getByText('inputs / a/b')).toBeVisible();
  expect(screen.getByText(/"second"/)).toHaveTextContent('"second", "first"');
  expect(document.querySelector('script[data-injected]')).toBeNull();
});
it('shows every exact stale reason and blocks unknown reasons generically', () => {
  render(
    <StaleEvidence
      reasons={['missing', 'draft', 'binding', 'policy', 'fixtures', 'failed', 'future-reason']}
    />,
  );
  expect(screen.getAllByRole('listitem')).toHaveLength(7);
  expect(
    screen.getByText(/approved fixture configuration or recorded execution evidence/),
  ).toBeVisible();
  expect(screen.getByText('The latest test run did not pass.')).toBeVisible();
  expect(screen.getByText('Evidence could not be verified.')).toBeVisible();
  expect(screen.queryByText(/fixture was changed/i)).toBeNull();
});

it('does not label omitted fixture values as an empty result', () => {
  const { actual: _actual, ...check } = run.checks[0];
  render(<FixtureChecks run={{ ...run, checks: [check as (typeof run.checks)[0]] }} />);
  expect(screen.getByText('Actual values not provided in this view.')).toBeVisible();
  expect(screen.queryByText('allowed: actual values: zero rows.')).toBeNull();
});
