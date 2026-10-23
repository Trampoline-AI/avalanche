import type { SkillMetadata } from "./GraphCanvas";
import { Markdown } from "./Markdown";

export function SkillContent({ skill }: { skill: SkillMetadata }) {
  return (
    <div className="grid min-w-0 gap-6 p-6 pr-16">
      <Markdown full>{skill.instructions}</Markdown>
      {(["packages", "modules"] as const).map((kind) =>
        skill[kind].length > 0 ? (
          <section key={kind} aria-label={kind === "packages" ? "Packages" : "Modules"}>
            <h3 className="inspector-section-title">
              {kind === "packages" ? "Packages" : "Modules"}
            </h3>
            <ul className="m-0 grid list-none gap-2 p-0">
              {skill[kind].map((name, index) => (
                <li key={`${index}:${name}`} className="min-w-0">
                  <code className="text-xs text-secondary [overflow-wrap:anywhere]">
                    {name}
                  </code>
                </li>
              ))}
            </ul>
          </section>
        ) : null,
      )}
    </div>
  );
}
