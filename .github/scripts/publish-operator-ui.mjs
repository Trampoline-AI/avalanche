import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const [version, tag, archive] = process.argv.slice(2);
if (!version || !["latest", "next"].includes(tag) || !archive) {
  throw new Error(
    "Usage: node publish-operator-ui.mjs VERSION latest|next ARCHIVE",
  );
}

const spec = `@trampoline-ai/operator-ui@${version}`;
const result = spawnSync("npm", ["view", spec, "dist.integrity", "--json"], {
  encoding: "utf8",
});
if (result.error) throw result.error;
if (result.status === 0) {
  const integrity = `sha512-${createHash("sha512").update(readFileSync(archive)).digest("base64")}`;
  if (JSON.parse(result.stdout) !== integrity) {
    throw new Error(
      `${spec} already exists with different archive contents; refusing to skip it.`,
    );
  }
  console.log(
    `${spec} is already published with matching integrity; skipping upload.`,
  );
} else {
  const error = JSON.parse(result.stdout).error;
  if (error.code !== "E404") {
    throw new Error(`Cannot check ${spec}: ${result.stderr}`);
  }
  const publish = spawnSync("npm", ["publish", archive, "--tag", tag], {
    stdio: "inherit",
  });
  if (publish.error) throw publish.error;
  if (publish.status !== 0) {
    throw new Error(`Publishing ${spec} failed (status ${publish.status}).`);
  }
}
