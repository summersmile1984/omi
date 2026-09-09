import { readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import type { WebPresentation } from "../../web/app/src/lib/fork/web-profile";

const ts = createRequire(
  resolve(import.meta.dirname, "../../web/app/package.json")
)("typescript");
export type PresentationInput = WebPresentation & {
  brand_id: string;
  product_name: string;
};

// Reviewed presentation owners only. Model prompts, API clients, protocol keys,
// analytics names and user/provider payloads are outside this transform.
export const presentationFiles = [
  "src/components/home/HomePage.tsx",
  "src/components/home/GoalComposer.tsx",
  "src/components/layout/Sidebar.tsx",
  "src/components/layout/MarketplaceHeader.tsx",
  "src/components/chat/ChatTranscript.tsx",
  "src/components/chat/ChatPanel.tsx",
  "src/components/chat/AppSelector.tsx",
  "src/components/chat/RecordingStage.tsx",
  "src/components/auth/ProtectedRoute.tsx",
  "src/components/marketplace/AppList.tsx",
  "src/components/marketplace/DeveloperBanner.tsx",
  "src/components/settings/SettingsPage.tsx",
  "src/components/settings/PlansSheet.tsx",
  "src/components/ui/BetaWelcomeModal.tsx",
  "src/components/ui/BetaRibbon.tsx",
  "src/components/apps/AppDisabledNotice.tsx",
  "src/components/apps/AppsExplorer.tsx",
  "src/components/notifications/NotificationPermissionBanner.tsx",
  "src/components/fair-use/FairUseStatus.tsx",
  "src/components/fair-use/CaseStatusView.tsx",
  "src/components/seo/JsonLd.tsx",
  "src/app/(public)/apps/page.tsx",
  "src/app/(public)/apps/[id]/page.tsx",
  "src/app/(public)/apps/category/[category]/page.tsx",
  "src/app/robots.ts",
  "src/app/sitemap.ts",
] as const;

function literal(value: string, brand: PresentationInput): string {
  const { product_name: name, links } = brand;
  if (value === "Omi - Your AI Companion")
    return brand.tagline ? `${name} - ${brand.tagline}` : name;
  if (value === "/omi-white.webp") return "/logo.png";
  if (value === "team@basedhardware.com") return brand.support_email;
  if (value === "function createOmiApp() {") return "function createApp() {";
  if (value.includes("shadow-[inset_0_0_20px_rgba(168,85,247,0.4)]"))
    return value.replace(
      "shadow-[inset_0_0_20px_rgba(168,85,247,0.4)]",
      "shadow-[inset_0_0_20px_rgba(255,255,255,0.12)]"
    );
  if (
    [
      "https://macos.omi.me",
      "https://macos.omi.me/",
      "https://omi.me/download",
      "https://onelink.to/rbsrxc",
    ].includes(value)
  )
    return links.download;
  if (["http://discord.omi.me", "https://discord.omi.me"].includes(value))
    return links.community;
  if (value.startsWith("https://feedback.omi.me")) return links.feedback;
  if (value.startsWith("https://help.omi.me")) return links.help;
  if (value.startsWith("https://docs.omi.me")) return links.docs;
  if (
    value === "https://omi.me/privacy" ||
    value === "https://www.omi.me/pages/privacy"
  )
    return links.privacy;
  if (value === "https://www.omi.me/pages/terms") return links.terms;
  if (/^https:\/\/(?:www\.)?omi\.me(?:\/|$)/.test(value))
    return value.replace(
      /^https:\/\/(?:www\.)?omi\.me/,
      links.website.replace(/\/$/, "")
    );
  if (value === "feedback.omi.me") return "Feedback";
  if (["Omi is 10X better on macOS", "Take Omi with you"].includes(value))
    return `Download ${name} for macOS`;
  if (["Try Omi on macOS", "Try Omi on your phone"].includes(value))
    return "Continue on your Mac";
  return value.replace(/\bOmi\b/g, () => name);
}

const templateText = (value: string) =>
  value.replace(/\\/g, "\\\\").replace(/`/g, "\\`").replace(/\$\{/g, "\\${");

/** Execute the same syntax-aware transform in staging and component tests. */
export function rewritePresentation(
  source: string,
  filename: string,
  brand: PresentationInput
): string {
  if (
    brand.brand_id === "omi-upstream" ||
    !presentationFiles.includes(filename as any)
  )
    return source;
  const file = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX
  );
  const edits: { start: number; end: number; text: string }[] = [];
  const edit = (node: any, text: string) =>
    edits.push({ start: node.getStart(file), end: node.end, text });
  const visit = (node: any) => {
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
      const opening = ts.isJsxElement(node) ? node.openingElement : node;
      const attributes = opening.attributes.properties;
      const href = attributes.find(
        (p: any) => ts.isJsxAttribute(p) && p.name.text === "href"
      )?.initializer;
      if (
        href &&
        ts.isStringLiteral(href) &&
        /^(?:http|https):\/\/discord\.omi\.me$/.test(href.text) &&
        !brand.links.community
      ) {
        edit(
          node,
          ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent)
            ? "{null}"
            : "null"
        );
        return;
      }
      const src = attributes.find(
        (p: any) => ts.isJsxAttribute(p) && p.name.text === "src"
      )?.initializer;
      if (
        filename.endsWith("/Sidebar.tsx") &&
        ts.isJsxSelfClosingElement(node) &&
        src?.text === "/omi-white.webp"
      ) {
        edit(
          node,
          `<><Image src="/logo.png" alt={${JSON.stringify(
            brand.product_name
          )}} width={28} height={28} className="h-7 w-7 object-contain" />{showText && <span className="font-semibold text-text-primary">{${JSON.stringify(
            brand.product_name
          )}}</span>}</>`
        );
        return;
      }
    }
    if (ts.isJsxText(node)) {
      // Retain JSX entity decoding and whitespace semantics around each label.
      const raw = node.getFullText(file);
      const text = raw
        .replace(/\bOmi\b/g, () => `{${JSON.stringify(brand.product_name)}}`)
        .replace(/feedback\.omi\.me/g, "Feedback");
      const display = filename.endsWith("/BetaWelcomeModal.tsx")
        ? text
            .replace(/Error Tracking/g, "Report an issue")
            .replace(
              /We capture errors to improve stability/g,
              "Contact support when something is not working"
            )
        : text;
      if (display !== raw)
        edits.push({ start: node.pos, end: node.end, text: display });
      return;
    }
    if (ts.isStringLiteral(node) || ts.isTemplateLiteralToken(node)) {
      const value = literal(node.text, brand);
      if (value !== node.text) {
        let text = JSON.stringify(value);
        if (ts.isJsxAttribute(node.parent)) text = `{${text}}`;
        else if (node.kind === ts.SyntaxKind.NoSubstitutionTemplateLiteral)
          text = "`" + templateText(value) + "`";
        else if (node.kind === ts.SyntaxKind.TemplateHead)
          text = "`" + templateText(value) + "${";
        else if (node.kind === ts.SyntaxKind.TemplateMiddle)
          text = "}" + templateText(value) + "${";
        else if (node.kind === ts.SyntaxKind.TemplateTail)
          text = "}" + templateText(value) + "`";
        edit(node, text);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  for (const change of edits.sort((a, b) => b.start - a.start))
    source =
      source.slice(0, change.start) + change.text + source.slice(change.end);
  return source;
}

export async function applyPresentation(
  stage: string,
  brand: PresentationInput
) {
  const applied: { source: string; sha256: string }[] = [];
  for (const path of presentationFiles) {
    const original = await readFile(resolve(stage, path), "utf8");
    const output = rewritePresentation(original, path, brand);
    if (output !== original) {
      await writeFile(resolve(stage, path), output);
      applied.push({
        source: path,
        sha256: new Bun.CryptoHasher("sha256").update(output).digest("hex"),
      });
    }
  }
  return applied;
}
