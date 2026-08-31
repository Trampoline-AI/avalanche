import { readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const DECLARATION_DIRECTORY = resolve(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const JAVASCRIPT_EXTENSION = /\.(?:[cm]?js)$/;
const RELATIVE_MODULE_SPECIFIER =
  /(\b(?:from|import|require)\s*(?:\(\s*)?)(["'])(\.{1,2}\/[^"'\\\r\n]*?)\2/g;
const STYLE_DECLARATION_IMPORT = /^[ \t]*import[ \t]+["']\.\/style\.css["'];?[ \t]*\r?\n/gm;

function withJavaScriptExtension(specifier) {
  const suffixIndex = specifier.search(/[?#]/);
  const modulePath = suffixIndex === -1 ? specifier : specifier.slice(0, suffixIndex);
  const suffix = suffixIndex === -1 ? "" : specifier.slice(suffixIndex);

  if (JAVASCRIPT_EXTENSION.test(modulePath)) return specifier;

  const resolvedModulePath = modulePath.endsWith("/")
    ? `${modulePath}index.js`
    : `${modulePath}.js`;
  return `${resolvedModulePath}${suffix}`;
}

function rewriteDeclaration(contents) {
  return contents
    .replace(STYLE_DECLARATION_IMPORT, "")
    .replace(RELATIVE_MODULE_SPECIFIER, (_match, prefix, quote, specifier) => {
      return `${prefix}${quote}${withJavaScriptExtension(specifier)}${quote}`;
    });
}

async function rewriteDeclarations(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const declarationPath = join(directory, entry.name);
    if (entry.isDirectory()) {
      await rewriteDeclarations(declarationPath);
      continue;
    }
    if (!entry.isFile() || !entry.name.endsWith(".d.ts")) continue;

    const contents = await readFile(declarationPath, "utf8");
    const rewritten = rewriteDeclaration(contents);
    if (rewritten !== contents) await writeFile(declarationPath, rewritten);
  }
}

await rewriteDeclarations(DECLARATION_DIRECTORY);
