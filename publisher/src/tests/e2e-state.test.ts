import { expect, it } from 'vitest';
import { DraftWorkflowState } from '../../e2e/draft-state';
it('rejects a serial workflow route before a verified draft is captured', () => {
  expect(() => new DraftWorkflowState().path('edit')).toThrow('Draft ID');
});
it('shares the first verified immutable draft ID with subsequent serial cases', () => {
  const state = new DraftWorkflowState();
  expect(state.capture('http://localhost/drafts/first-draft/edit')).toBe('first-draft');
  expect(state.path('edit')).toBe('/drafts/first-draft/edit');
  expect(state.path('review')).toBe('/drafts/first-draft/review');
  state.capture('http://localhost/drafts/saved-next/edit');
  expect(state.path('review')).toBe('/drafts/saved-next/review');
});
it.each(['/drafts//edit', '/drafts/new', '/catalog', '/drafts/%2F/edit'])(
  'rejects invalid receipt route %s',
  (path) => {
    expect(() => new DraftWorkflowState().capture(`http://localhost${path}`)).toThrow('Draft ID');
  },
);
