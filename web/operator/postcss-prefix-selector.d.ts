declare module "postcss-prefix-selector" {
  import type { Plugin, Rule } from "postcss";

  interface PrefixSelectorOptions {
    prefix: string;
    exclude?: Array<string | RegExp>;
    transform?: (
      prefix: string,
      selector: string,
      prefixedSelector: string,
      filePath: string,
      rule: Rule,
    ) => string;
  }

  export default function prefixer(options: PrefixSelectorOptions): Plugin;
}
