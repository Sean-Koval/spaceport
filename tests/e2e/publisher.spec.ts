import { DraftWorkflowState } from "../../publisher/e2e/draft-state";
import {
  test,
  expect,
  type Page,
  type BrowserContext,
} from "../../publisher/node_modules/@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const baseURL = process.env.GATEWAY_E2E_BASE_URL;
const tokenFile = process.env.GATEWAY_E2E_TOKEN_FILE;
type Tokens = Record<
  "demo-author" | "demo-publisher" | "demo-north" | "demo-south",
  string
>;
let tokens: Tokens;
const draftState = new DraftWorkflowState();
test.describe.configure({ mode: "serial" });
const requests: {
  method: string;
  origin: string;
  pathname: string;
  resource_type: string;
}[] = [];
const externalRequests: string[] = [];
test.afterAll(() => {
  const directory = process.env.GATEWAY_OPERATIONS_EVIDENCE_DIR;
  if (directory && baseURL) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    writeFileSync(
      join(directory, "publisher-network.json"),
      JSON.stringify(
        {
          base_origin: new URL(baseURL).origin,
          requests,
          external_requests: externalRequests,
          contains_auth_headers: false,
          contains_query_data: false,
        },
        null,
        2,
      ),
      { mode: 0o600 },
    );
  }
});
test.beforeAll(() => {
  if (!baseURL || !tokenFile)
    throw new Error(
      "Real publisher acceptance requires GATEWAY_E2E_BASE_URL and a private GATEWAY_E2E_TOKEN_FILE. No mocked gateway is substituted. See publisher/README.md.",
    );
  const url = new URL(baseURL);
  if (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))
    throw new Error(
      "Publisher acceptance is restricted to an explicitly configured local synthetic runtime.",
    );
  tokens = JSON.parse(readFileSync(tokenFile, "utf8"));
  if (
    ["demo-author", "demo-publisher", "demo-north", "demo-south"].some(
      (name) => typeof tokens[name as keyof Tokens] !== "string",
    )
  )
    throw new Error(
      "Four separately provisioned synthetic demo identities are required.",
    );
});
async function signIn(page: Page, role: keyof Tokens, path: string) {
  await page.goto(path);
  await startSession(page, role);
}
async function startSession(page: Page, role: keyof Tokens) {
  await expect(page.getByText(/Synthetic demo/)).toBeVisible();
  try {
    await page.getByLabel("Session bearer").fill(tokens[role]);
  } catch {
    throw new Error("Synthetic session entry failed.");
  }
  await page.getByRole("button", { name: "Start session" }).click();
}
async function localOnly(context: BrowserContext) {
  const external: string[] = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    requests.push({
      method: route.request().method(),
      origin: url.origin,
      pathname: url.pathname,
      resource_type: route.request().resourceType(),
    });
    if (url.origin === new URL(baseURL!).origin || url.protocol === "data:")
      await route.continue();
    else {
      external.push(`${url.protocol}//${url.hostname}`);
      externalRequests.push(`${url.protocol}//${url.hostname}`);
      await route.abort("blockedbyclient");
    }
  });
  return external;
}
test("clean bootstrap: author → exact fixture evidence → independent publisher → scoped consumers, narrow and keyboard use", async ({
  browser,
}) => {
  const author = await browser.newContext({ baseURL });
  const publisher = await browser.newContext({ baseURL });
  const north = await browser.newContext({
    baseURL,
    viewport: { width: 375, height: 812 },
  });
  const south = await browser.newContext({ baseURL });
  const unexpected = await Promise.all(
    [author, publisher, north, south].map(localOnly),
  );
  try {
    const a = await author.newPage();
    await signIn(a, "demo-author", "/drafts/new");
    await a
      .getByRole("button", { name: "Use synthetic revenue template" })
      .click();
    await expect(a.getByLabel("Approved binding revision")).not.toHaveValue("");
    await expect(a.getByLabel("Approved access policy")).not.toHaveValue("");
    await a.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(a).toHaveURL(/\/drafts\/[^/]+\/edit$/);
    const draftId = draftState.capture(a.url());
    await a.getByRole("link", { name: "Back to saved drafts" }).click();
    await expect(
      a.getByRole("heading", { name: "Saved drafts", exact: true }),
    ).toBeVisible();
    await a.reload();
    await startSession(a, "demo-author");
    await expect(
      a.getByRole("heading", { name: "Saved drafts", exact: true }),
    ).toBeVisible();
    const resume = a.getByRole("link", { name: "Resume editing", exact: true });
    await expect(resume).toHaveAttribute("href", `/drafts/${draftId}/edit`);
    await resume.click();
    await expect(a).toHaveURL(new RegExp(`/drafts/${draftId}/edit$`));
    await a.getByRole("button", { name: "Run tests", exact: true }).click();
    await expect(
      a
        .getByRole("status")
        .filter({ hasText: /Tests passed for saved revision/ }),
    ).toBeVisible({ timeout: 170000 });
    await a.getByRole("button", { name: "Review publication" }).click();
    await expect(
      a.getByRole("button", { name: "Publish", exact: true }),
    ).toBeDisabled();
    await expect(a.getByText("Expected access denial verified")).toBeVisible();
    const p = await publisher.newPage();
    await signIn(p, "demo-publisher", "/drafts");
    await expect(
      p.getByRole("heading", { name: "Saved drafts", exact: true }),
    ).toBeVisible();
    await expect(
      p.getByRole("link", { name: "Resume editing", exact: true }),
    ).toHaveCount(0);
    const review = p.getByRole("link", { name: "Review draft", exact: true });
    await expect(review).toHaveAttribute("href", `/drafts/${draftId}/review`);
    await review.click();
    await expect(
      p.getByRole("button", { name: "Publish", exact: true }),
    ).toBeEnabled();
    const pins = p.getByText("Authorized evidence pins");
    await pins.click();
    await expect(p.getByText("Tested digest", { exact: true })).toBeVisible();
    await p.getByRole("button", { name: "Publish", exact: true }).click();
    await expect(p.getByRole("dialog")).toBeVisible();
    await p.keyboard.press("Escape");
    await expect(p.getByRole("dialog")).toHaveCount(0);
    await p.getByRole("button", { name: "Publish", exact: true }).focus();
    await p.keyboard.press("Enter");
    await p.getByLabel("Transition reason").focus();
    await p.keyboard.type(
      "Reviewed synthetic fixture results and exact definition.",
    );
    await p.keyboard.press("Tab");
    await expect(
      p.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeFocused();
    await p.keyboard.press("Tab");
    await expect(
      p.getByRole("button", { name: "Confirm publication" }),
    ).toBeFocused();
    await p.keyboard.press("Enter");
    await expect(
      p.getByText("Published version 1.0.0", { exact: true }),
    ).toBeVisible();
    await p.getByRole("link", { name: "Back to saved drafts" }).click();
    await expect(
      p.getByText("No saved drafts available for this account.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(p.locator(`a[href="/drafts/${draftId}/review"]`)).toHaveCount(
      0,
    );
    const n = await north.newPage();
    await signIn(n, "demo-north", "/catalog");
    await n.getByRole("link", { name: "Daily revenue", exact: true }).focus();
    await n.keyboard.press("Enter");
    await expect(
      n.getByRole("heading", { name: "Daily revenue", exact: true }),
    ).toBeVisible();
    await n.getByRole("button", { name: "Use example 1" }).focus();
    await n.keyboard.press("Enter");
    // This permitted lower bound keeps all original known-answer fixture rows.
    // The UI must transmit real controls; the existing exact decimal/tenant
    // assertions below still reconcile the backend's unchanged expected rows.
    await n.getByRole("button", { name: "Add filter", exact: true }).click();
    await n.getByLabel("Filter 1 field", { exact: true }).selectOption("day");
    await n
      .getByLabel("Filter 1 operator", { exact: true })
      .selectOption("gte");
    await n
      .getByLabel("Filter 1 value (date)", { exact: true })
      .fill("2024-03-09");
    await n.getByRole("button", { name: "Add sort", exact: true }).click();
    await n.getByLabel("Sort 1 field", { exact: true }).selectOption("day");
    await n.getByLabel("Sort 1 direction", { exact: true }).selectOption("asc");
    await n.getByRole("button", { name: "Run product", exact: true }).focus();
    await n.keyboard.press("Enter");
    await expect(
      n.getByRole("table", { name: "Authorized result rows" }),
    ).toBeVisible();
    const northRows = n.getByRole("table", { name: "Authorized result rows" });
    await expect(northRows).toContainText("9007199254740993.01");
    await expect(northRows).toContainText("20.20");
    await expect(northRows).not.toContainText("100.00");
    await expect(n.getByText("Source loaded", { exact: true })).toBeVisible();
    await expect(
      n.getByText("Freshness: fresh", { exact: true }),
    ).toBeVisible();
    expect(
      await n.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    const s = await south.newPage();
    await signIn(s, "demo-south", "/products/revenue?version=1.0.0");
    await s.getByRole("button", { name: "Use example 1" }).click();
    await s.getByRole("button", { name: "Run product", exact: true }).click();
    const southRows = s.getByRole("table", { name: "Authorized result rows" });
    await expect(southRows).toContainText("100.00");
    await expect(southRows).toContainText("0.01");
    await expect(southRows).not.toContainText("9007199254740993.01");
    await expect(
      s.getByText(/Live verification not reported by this gateway/),
    ).toBeVisible();
    for (const page of [a, p, n, s]) {
      expect(
        await page.evaluate(() => ({
          local: localStorage.length,
          session: sessionStorage.length,
        })),
      ).toEqual({ local: 0, session: 0 });
      expect(
        await page
          .locator('script[src^="http"],link[rel="stylesheet"][href^="http"]')
          .count(),
      ).toBe(0);
    }
    expect(unexpected.flat()).toEqual([]);
  } finally {
    await Promise.all(
      [author, publisher, north, south].map((context) => context.close()),
    );
  }
});

async function saveNextVersion(page: Page, version: string) {
  await page.getByLabel("Version", { exact: true }).fill(version);
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect
    .poll(() => new URL(page.url()).pathname)
    .not.toBe(draftState.path("edit"));
  draftState.capture(page.url());
  await page.getByRole("button", { name: "Run tests", exact: true }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: /Tests passed for saved revision/ }),
  ).toBeVisible({ timeout: 170000 });
}
async function openPublish(page: Page, reason: string) {
  await expect(
    page.getByRole("button", { name: "Publish", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await page.getByLabel("Transition reason").fill(reason);
}
test("real concurrent save and publish conflicts require explicit head reload", async ({
  browser,
}) => {
  const author = await browser.newContext({ baseURL });
  const publisher = await browser.newContext({ baseURL });
  await Promise.all([author, publisher].map(localOnly));
  try {
    const left = await author.newPage();
    const right = await author.newPage();
    const oldId = draftState.id;
    await Promise.all([
      signIn(left, "demo-author", `/drafts/${oldId}/edit`),
      signIn(right, "demo-author", `/drafts/${oldId}/edit`),
    ]);
    for (const page of [left, right])
      await page.getByLabel("Version", { exact: true }).fill("1.0.1");
    await left
      .getByLabel("Description", { exact: true })
      .fill("Synthetic revenue, concurrent review A.");
    await right
      .getByLabel("Description", { exact: true })
      .fill("Synthetic revenue, concurrent review B.");
    await Promise.all([
      left.getByRole("button", { name: "Save draft", exact: true }).click(),
      right.getByRole("button", { name: "Save draft", exact: true }).click(),
    ]);
    await expect
      .poll(
        () =>
          [left, right].filter(
            (page) => !new URL(page.url()).pathname.includes(oldId),
          ).length,
      )
      .toBe(1);
    const winner = !new URL(left.url()).pathname.includes(oldId) ? left : right;
    const loser = winner === left ? right : left;
    draftState.capture(winner.url());
    await expect(loser.getByRole("alert")).toContainText(
      "This revision changed",
    );
    await loser
      .getByRole("button", { name: "Reload current revision" })
      .click();
    await expect(loser.getByRole("dialog")).toContainText(
      "Discard unsaved changes",
    );
    await loser.getByRole("button", { name: "Discard and reload" }).click();
    await expect(loser).toHaveURL(new RegExp(`/drafts/${draftState.id}/edit$`));
    await loser.getByRole("button", { name: "Run tests", exact: true }).click();
    await expect(
      loser
        .getByRole("status")
        .filter({ hasText: /Tests passed for saved revision/ }),
    ).toBeVisible({ timeout: 170000 });
    const p1 = await publisher.newPage();
    const p2 = await publisher.newPage();
    await Promise.all([
      signIn(p1, "demo-publisher", draftState.path("review")),
      signIn(p2, "demo-publisher", draftState.path("review")),
    ]);
    await Promise.all([
      openPublish(p1, "Concurrent reviewed publication A."),
      openPublish(p2, "Concurrent reviewed publication B."),
    ]);
    await Promise.all([
      p1.getByRole("button", { name: "Confirm publication" }).click(),
      p2.getByRole("button", { name: "Confirm publication" }).click(),
    ]);
    await expect
      .poll(
        async () =>
          Number(
            await p1
              .getByText("Published version 1.0.1", { exact: true })
              .count(),
          ) +
          Number(
            await p2
              .getByText("Published version 1.0.1", { exact: true })
              .count(),
          ),
      )
      .toBe(1);
    const blocked = (await p1
      .getByText("Published version 1.0.1", { exact: true })
      .count())
      ? p2
      : p1;
    await expect(blocked.getByRole("alert")).toContainText(
      "This revision changed",
    );
    await expect(
      blocked.getByRole("button", { name: "Publish", exact: true }),
    ).toBeDisabled();
  } finally {
    await Promise.all([author, publisher].map((context) => context.close()));
  }
});
test("a dropped real publication response stays uncertain without a duplicate write", async ({
  browser,
}) => {
  const author = await browser.newContext({ baseURL });
  const publisher = await browser.newContext({ baseURL });
  const consumer = await browser.newContext({ baseURL });
  await Promise.all([author, publisher, consumer].map(localOnly));
  try {
    const a = await author.newPage();
    await signIn(a, "demo-author", draftState.path("edit"));
    await saveNextVersion(a, "1.0.2");
    const p = await publisher.newPage();
    await signIn(p, "demo-publisher", draftState.path("review"));
    let writes = 0;
    await p.route("**/v1/drafts/*/publish", async (route) => {
      writes++;
      const url = new URL(route.request().url());
      requests.push({
        method: route.request().method(),
        origin: url.origin,
        pathname: url.pathname,
        resource_type: route.request().resourceType(),
      });
      // The genuine gateway commits first; only the browser's response is dropped.
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      await route.abort("failed");
    });
    await openPublish(
      p,
      "Reviewed exact evidence before controlled lost response.",
    );
    await p.getByRole("button", { name: "Confirm publication" }).click();
    await expect(
      p.getByText("Publication outcome not yet confirmed.", { exact: true }),
    ).toBeVisible();
    await expect(
      p.getByRole("button", { name: "Publish", exact: true }),
    ).toBeDisabled();
    await p.getByRole("button", { name: "Check publication outcome" }).click();
    // The denied describe clears the review; only the independent authorized consumer can establish the outcome.
    await expect(
      p.getByRole("heading", { name: "View unavailable" }),
    ).toBeVisible();
    await expect(p.getByText("Authorized evidence pins")).toHaveCount(0);
    expect(writes).toBe(1);
    const c = await consumer.newPage();
    await signIn(c, "demo-north", "/products/revenue?version=1.0.2");
    await expect(
      c.getByRole("heading", { name: "Daily revenue", exact: true }),
    ).toBeVisible();
    await c.getByRole("button", { name: "Use example 1" }).click();
    await c.getByRole("button", { name: "Run product", exact: true }).click();
    await expect(
      c.getByRole("table", { name: "Authorized result rows" }),
    ).toContainText("9007199254740993.01");
    expect(writes).toBe(1);
  } finally {
    await Promise.all(
      [author, publisher, consumer].map((context) => context.close()),
    );
  }
});
