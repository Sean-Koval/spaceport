// Serial browser cases share only the immutable draft locator, never bearer state.
export class DraftWorkflowState {
  private draftId = '';
  get id() {
    if (!this.draftId) throw new Error('Draft ID has not been verified by the preceding workflow.');
    return this.draftId;
  }
  capture(url: string) {
    const match = /^\/drafts\/([A-Za-z0-9_-]+)\/edit$/.exec(new URL(url).pathname);
    if (!match) throw new Error('Draft ID is missing from the verified editor route.');
    this.draftId = match[1];
    return this.id;
  }
  path(view: 'edit' | 'review') {
    return `/drafts/${encodeURIComponent(this.id)}/${view}`;
  }
}
