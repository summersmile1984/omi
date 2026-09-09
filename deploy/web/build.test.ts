import { describe, expect, test } from "bun:test";
import { createRequire } from "node:module";
import {
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import {
  publicEnvironment,
  injectPublicEnvironment,
} from "./public-environment";
import {
  confinedPath,
  emptyOutput,
  rewriteBrandMetadata,
  rewriteMcpUrl,
  stageSources,
} from "./source-stage";
import { generateWebAssets, snapshotWebAssets } from "./brand-assets.mjs";
import { writeFixtureAssets } from "../../scripts/brand/raster/fixture.mjs";
import { PNG } from "../../scripts/brand/raster/png.mjs";
import { rewritePresentation } from "./presentation";

const profile = {
  name: "cloudflare.local",
  target: "cloudflare",
  stage: "local",
  identity_provider: "better_auth",
  api_base_url: "http://127.0.0.1:8787/service/",
  auth_base_url: "http://127.0.0.1:8788",
  web_base_url: "http://localhost:3000",
  mcp_base_url: "http://127.0.0.1:9000/mcp",
  share_base_url: "http://127.0.0.1:8787/share",
  objects_base_url: "http://127.0.0.1:8787/objects",
  auth_callback_scheme: "fixture-dev",
  capabilities: { push_provider: "webhook" },
};

const presentation = {
  tagline: "Private notebook",
  support_email: "help@fixture.invalid",
  links: {
    website: "https://web.fixture.invalid",
    download: "https://api.fixture.invalid/v2/desktop/download/latest",
    docs: "https://docs.fixture.invalid",
    help: "https://help.fixture.invalid",
    feedback: "https://feedback.fixture.invalid",
    community: "",
    privacy: "https://web.fixture.invalid/privacy",
    terms: "https://web.fixture.invalid/terms",
  },
};

describe("the shared Web build boundary", () => {
  test("executes presentation literals safely while preserving data and model prompts", () => {
    const brand = {
      ...presentation,
      brand_id: "harbor",
      product_name: 'Harbor "<& ${notCode}`',
    };
    const source = `const title = 'Omi - Your AI Companion';
      const app = appName + ' Available on Omi.';
      const url = \`https://omi.me/apps/\${id}\`;
      const docs = 'https://docs.omi.me/doc/developer/MCP';
      return {title, app, url, docs};`;
    const output = rewritePresentation(
      source,
      "src/components/home/HomePage.tsx",
      brand
    );
    const run = new Function("appName", "id", output);
    expect(run("Omi user notes", "one")).toEqual({
      title: brand.product_name + " - Private notebook",
      app: "Omi user notes Available on " + brand.product_name + ".",
      url: "https://web.fixture.invalid/apps/one",
      docs: presentation.links.docs,
    });
    expect(rewritePresentation(source, "src/lib/geminiLive.ts", brand)).toBe(
      source
    );
    expect(
      rewritePresentation(source, "src/components/home/HomePage.tsx", {
        ...brand,
        brand_id: "omi-upstream",
      })
    ).toBe(source);
    const jsx = `return <section><p>Hi! I&apos;m Omi</p><a aria-label="Omi" href="http://discord.omi.me">Discord</a><a href="https://macos.omi.me/">Download</a></section>`;
    const rewritten = rewritePresentation(
      jsx,
      "src/components/home/HomePage.tsx",
      brand
    );
    const compiled = new Bun.Transpiler({
      loader: "tsx",
      tsconfig: { compilerOptions: { jsx: "react", jsxFactory: "h" } },
    }).transformSync(rewritten);
    const render = new Function("h", compiled);
    const tree = render(
      (tag: string, props: unknown, ...children: unknown[]) => ({
        tag,
        props,
        children,
      })
    );
    expect(tree.children[0].children.join("")).toBe(
      "Hi! I'm " + brand.product_name
    );
    expect(tree.children[1]).toBeNull();
    expect(tree.children[2].props.href).toBe(presentation.links.download);
    const support = rewritePresentation(
      "return 'team@basedhardware.com';",
      "src/components/fair-use/CaseStatusView.tsx",
      brand
    );
    expect(new Function(support)()).toBe(brand.support_email);
    const example = rewritePresentation(
      "return 'function createOmiApp() {';",
      "src/components/marketplace/DeveloperBanner.tsx",
      brand
    );
    expect(new Function(example)()).toBe("function createApp() {");
    const welcome = rewritePresentation(
      "return <p>Error Tracking: We capture errors to improve stability</p>",
      "src/components/ui/BetaWelcomeModal.tsx",
      brand
    );
    const welcomeTree = new Function(
      "h",
      new Bun.Transpiler({
        loader: "tsx",
        tsconfig: { compilerOptions: { jsx: "react", jsxFactory: "h" } },
      }).transformSync(welcome)
    )((tag: string, props: unknown, ...children: unknown[]) =>
      children.join("")
    );
    expect(welcomeTree).toBe(
      "Report an issue: Contact support when something is not working"
    );
  });
  test("executes branded generated metadata without rewriting API origins or user app descriptions", () => {
    const ts = createRequire(
      resolve(import.meta.dir, "../../web/app/package.json")
    )("typescript");
    const source = `const api = 'https://api.omi.me';
      const title = pathname === '/login' ? 'Sign In to Omi' : 'Omi - Your AI Companion';
      const description = 'Omi - Your AI companion that turns thoughts into action.';
      return { api, title, description, app: app.description + ' Available on Omi, the AI-powered wearable platform.' };`;
    const rewritten = rewriteBrandMetadata(
      source,
      'Harbor "<&',
      "懂你的随身AI伴侣",
      ts
    );
    const render = new Function("pathname", "app", rewritten);
    expect(render("/login", { description: "My Omi notes" })).toEqual({
      api: "https://api.omi.me",
      title: 'Sign In to Harbor "<&',
      description: 'Harbor "<& - 懂你的随身AI伴侣',
      app: 'My Omi notes Available on Harbor "<&, the AI-powered wearable platform.',
    });
    expect(render("/home", { description: "" }).title).toBe(
      'Harbor "<& - 懂你的随身AI伴侣'
    );
    const legacy = new Function(
      "pathname",
      "app",
      rewriteBrandMetadata(source, "Harbor", "", ts)
    );
    expect(legacy("/home", { description: "" }).description).toBe("Harbor");
    expect(() =>
      rewriteBrandMetadata(
        source.replace("Sign In to Omi", "New login title"),
        "Harbor",
        "Companion",
        ts
      )
    ).toThrow("metadata owner changed");
  });
  test("replaces public brand images through the shared raster owner and preserves private-manifest snapshots", async () => {
    const temp = await mkdtemp(resolve(tmpdir(), "web-brand-contract-"));
    try {
      writeFixtureAssets(temp);
      const refs = {
        icon_master: "assets/harbor-icon_master.png",
        logo_light: "assets/harbor-logo_light.png",
      };
      const input = { root: temp, refs };
      const publicDirectory = resolve(temp, "public");
      await mkdir(publicDirectory);
      await writeFile(resolve(publicDirectory, "logo.png"), "upstream logo");
      await writeFile(
        resolve(publicDirectory, "omi-white.webp"),
        "upstream wordmark"
      );
      const original = await readFile(resolve(temp, refs.logo_light));
      const assets = generateWebAssets("harbor", input, publicDirectory);
      expect(assets.mode).toBe("manifest");
      expect(
        await Bun.file(resolve(publicDirectory, "omi-white.webp")).exists()
      ).toBe(false);
      expect(Object.keys(assets.outputs).sort()).toEqual([
        "favicon.png",
        "logo.png",
      ]);
      const logoBytes = await readFile(resolve(publicDirectory, "logo.png"));
      const logo = PNG.sync.read(logoBytes);
      expect([logo.width, logo.height]).toEqual([512, 512]);
      expect([
        ...logo.data.subarray((256 * 512 + 256) * 4, (256 * 512 + 256) * 4 + 4),
      ]).toEqual([255, 255, 255, 255]);
      expect(logo.data[3]).toBe(0);
      expect(
        PNG.sync.read(await readFile(resolve(publicDirectory, "favicon.png")))
          .width
      ).toBe(64);
      expect(assets.outputs["logo.png"].sha256).toBe(
        new Bun.CryptoHasher("sha256").update(logoBytes).digest("hex")
      );
      expect(await readFile(resolve(temp, refs.logo_light))).toEqual(original);
      const frozen = resolve(temp, "inputs");
      snapshotWebAssets("harbor", input, frozen);
      await writeFile(resolve(temp, refs.logo_light), "changed source");
      const second = generateWebAssets(
        "harbor",
        { root: frozen, refs },
        resolve(temp, "second")
      );
      expect(second).toEqual(assets);
      expect(await readFile(resolve(frozen, refs.logo_light))).toEqual(
        original
      );
      expect(() => generateWebAssets("harbor", input, publicDirectory)).toThrow(
        "static PNG"
      );
      expect(await readFile(resolve(publicDirectory, "logo.png"))).toEqual(
        logoBytes
      );
      await writeFile(
        resolve(publicDirectory, "omi-white.webp"),
        "upstream wordmark"
      );
      expect(
        generateWebAssets("omi-upstream", undefined, publicDirectory).mode
      ).toBe("upstream");
      expect(
        await readFile(resolve(publicDirectory, "omi-white.webp"), "utf8")
      ).toBe("upstream wordmark");
      expect(await readFile(resolve(publicDirectory, "logo.png"))).toEqual(
        logoBytes
      );
    } finally {
      await rm(temp, { recursive: true, force: true });
    }
  });

  test("projects the real Eddy manifest asset directory without adding local paths to public environment", () => {
    const result = Bun.spawnSync(
      [
        resolve(import.meta.dir, "../../backend/.venv/bin/python"),
        resolve(import.meta.dir, "profile_input.py"),
        "--brand",
        "eddy",
        "--target",
        "cloudflare",
        "--stage",
        "production",
      ],
      { stdout: "pipe", stderr: "pipe" }
    );
    expect(result.exitCode).toBe(0);
    const input = JSON.parse(result.stdout.toString());
    expect(input.asset_input.root).toBe(
      resolve(import.meta.dir, "../../brand/eddy")
    );
    expect(input.asset_input.refs.logo_light).toBe("assets/logo-light.png");
    expect(input.links.download).toBe(
      input.profile.api_base_url.replace(/\/$/, "") +
        "/v2/desktop/download/latest"
    );
    expect(JSON.stringify(publicEnvironment(input))).not.toContain(
      input.asset_input.root
    );
  });
  test("projects one validated profile without leaking secrets or dropping API mount paths", () => {
    const values = publicEnvironment({
      ...presentation,
      links: {
        ...presentation.links,
        private_token: "do-not-ship",
      } as typeof presentation.links,
      product_name: "Fixture",
      profile: { ...profile, database_password: "do-not-ship" },
    });
    expect(values.NEXT_PUBLIC_API_BASE_URL).toBe(
      "http://127.0.0.1:8787/service"
    );
    expect(values.NEXT_PUBLIC_WS_BASE_URL).toBe("ws://127.0.0.1:8787/service");
    expect(JSON.stringify(values)).not.toContain("do-not-ship");
    expect(JSON.parse(values.NEXT_PUBLIC_OMI_PRESENTATION_JSON)).toEqual(
      presentation
    );
    expect(() =>
      publicEnvironment({
        ...presentation,
        links: { ...presentation.links, help: "javascript:alert(1)" },
        product_name: "Fixture",
        profile,
      })
    ).toThrow("invalid help");
    expect(JSON.parse(values.NEXT_PUBLIC_OMI_PROFILE_JSON).mcp_base_url).toBe(
      profile.mcp_base_url
    );
    expect(
      injectPublicEnvironment(
        'globalThis.process = { env: {} };\nconsole.log("client");',
        values
      )
    ).toContain("NEXT_PUBLIC_OMI_PROFILE_JSON");
    expect(() =>
      injectPublicEnvironment("new upstream banner", values)
    ).toThrow("banner changed");
    expect(() =>
      publicEnvironment({
        ...presentation,
        product_name: "Fixture",
        profile: { ...profile, auth_base_url: profile.api_base_url },
      })
    ).toThrow("must be an origin");
    expect(() =>
      publicEnvironment({
        ...presentation,
        product_name: "Fixture",
        profile: { ...profile, mcp_base_url: "" },
      })
    ).toThrow("requires mcp_base_url");
  });

  test("changes the single MCP expression structurally, while refusing upstream drift", () => {
    const ts = createRequire(
      resolve(import.meta.dir, "../../web/app/package.json")
    )("typescript");
    const source =
      "'use client';\nexport function Page() { const mcpServerUrl = `${process.env.NEXT_PUBLIC_API_BASE_URL || 'https://api.omi.me'}/v1/mcp/sse`; return mcpServerUrl; }";
    const output = rewriteMcpUrl(source, ts);
    expect(output.startsWith("'use client';\nimport")).toBe(true);
    expect(output).toContain("const mcpServerUrl = profileMcpServerUrl()");
    expect(output).not.toContain("https://api.omi.me");
    expect(() =>
      rewriteMcpUrl(source.replace("mcpServerUrl", "mcpEndpoint"), ts)
    ).toThrow("expected exactly one");
    expect(() =>
      rewriteMcpUrl(
        source.replace("https://api.omi.me", "https://new.example"),
        ts
      )
    ).toThrow("source contract changed");
  });

  test("stages exact source replacements with no source writes, env copying or output aliasing", async () => {
    const temp = await mkdtemp(resolve(tmpdir(), "web-stage-contract-"));
    const web = resolve(temp, "web");
    const stage = resolve(temp, "stage");
    try {
      await mkdir(resolve(web, "src"), { recursive: true });
      await mkdir(resolve(web, "fork"));
      await mkdir(resolve(web, "node_modules"));
      await writeFile(resolve(web, "src/firebase.ts"), "old Firebase source");
      await writeFile(
        resolve(web, "fork/firebase.ts"),
        "new Better Auth facade"
      );
      await writeFile(resolve(web, "fork/share.ts"), "new public share route");
      await writeFile(resolve(web, ".env.local"), "PRIVATE=do-not-copy");
      const rows = await stageSources(web, stage, {
        schema_version: 1,
        files: { "src/firebase.ts": "fork/firebase.ts" },
        additions: { "src/app/share.ts": "fork/share.ts" },
      });
      expect(rows).toHaveLength(2);
      expect(rows.map((row) => row.mode)).toEqual(["replace", "add"]);
      expect(await readFile(resolve(stage, "src/firebase.ts"), "utf8")).toBe(
        "new Better Auth facade"
      );
      expect(await readFile(resolve(web, "src/firebase.ts"), "utf8")).toBe(
        "old Firebase source"
      );
      expect(await readFile(resolve(stage, "src/app/share.ts"), "utf8")).toBe(
        "new public share route"
      );
      await expect(
        stageSources(web, resolve(temp, "collision-stage"), {
          schema_version: 1,
          files: {},
          additions: { "src/firebase.ts": "fork/share.ts" },
        })
      ).rejects.toThrow("replace an existing source");
      expect(await Bun.file(resolve(stage, ".env.local")).exists()).toBe(false);
      expect(() => confinedPath(web, "../escape.ts")).toThrow(
        "inside its source root"
      );
      await symlink(web, resolve(temp, "alias"));
      await expect(
        emptyOutput(resolve(temp, "alias/build"), web)
      ).rejects.toThrow("outside the upstream");
      const alias = resolve(temp, "outside-alias");
      await mkdir(resolve(temp, "outside"));
      await symlink(resolve(temp, "outside"), alias);
      expect(await emptyOutput(resolve(alias, "build"), web)).toBe(
        await realpath(resolve(temp, "outside/build"))
      );
    } finally {
      await rm(temp, { recursive: true, force: true });
    }
  });
});
