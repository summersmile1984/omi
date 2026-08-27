import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import YAML from "yaml";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const routeManifest = YAML.parse(await readFile(resolve(root, "manifests/routes.yaml"), "utf8"));
const resourceManifest = YAML.parse(await readFile(resolve(root, "manifests/resources.yaml"), "utf8"));
const edgeSource = await readFile(resolve(root, "workers/edge/index.ts"), "utf8");

const requiredRouteFields = ["method", "path", "owner", "runtime", "target_runtime", "auth_authority", "protocol"];
const allowedTargetRuntimes = new Set(["typescript-worker", "python-worker", "realtime-do", "external-api", "legacy", "blocked"]);
const routes = routeManifest?.routes;
if (!Array.isArray(routes) || routes.length === 0) throw new Error("routes.yaml must contain at least one route");

const duplicateKeys = new Set();
for (const route of routes) {
  for (const field of requiredRouteFields) {
    if (typeof route[field] !== "string" || route[field].length === 0) {
      throw new Error(`route is missing required string field: ${field}`);
    }
  }
  const key = `${route.method} ${route.path}`;
  if (duplicateKeys.has(key)) throw new Error(`duplicate route: ${key}`);
  duplicateKeys.add(key);
  if (!allowedTargetRuntimes.has(route.target_runtime)) {
    throw new Error(`unsupported target_runtime for ${key}: ${route.target_runtime}`);
  }
  const routeHint = route.path === "/*" ? "/" : route.path.replace(/\*$/, "");
  const pathSegments = route.path.split("/").filter(Boolean);
  const edgeHints = [routeHint];
  if (!route.path.endsWith("*") && pathSegments.length > 1) {
    edgeHints.push(`/${pathSegments.slice(0, -1).join("/")}/`);
  }
  if (!edgeHints.some((hint) => edgeSource.includes(hint))) {
    throw new Error(`route is not represented in edge routing code: ${key}`);
  }
}

const resources = resourceManifest?.resources;
if (!Array.isArray(resources) || resources.length === 0) throw new Error("resources.yaml must contain resources");
for (const resource of resources) {
  if (!resource.kind || !resource.name || resource.environment !== "staging") {
    throw new Error(`resource must have kind/name and staging environment: ${JSON.stringify(resource)}`);
  }
  if (!resource.name.startsWith("omi-cf-")) throw new Error(`resource is outside the Cloudflare namespace: ${resource.name}`);
}

console.log(`Manifest validation passed: ${routes.length} routes, ${resources.length} staging resources.`);
