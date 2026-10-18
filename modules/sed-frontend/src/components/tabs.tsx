/** Вкладки за шаблоном WAI-ARIA Tabs: стрілки ←/→, Home/End, roving tabindex. */
import { useRef, type ReactNode } from "react";
import { cx } from "./ui";

export interface TabDef {
  key: string;
  label: string;
  badge?: number;
}

export function Tabs({ tabs, current, onChange, idBase }: { tabs: TabDef[]; current: string; onChange: (k: string) => void; idBase: string }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const move = (i: number) => {
    const n = (i + tabs.length) % tabs.length;
    refs.current[n]?.focus();
    onChange(tabs[n]!.key);
  };
  return (
    <div role="tablist" aria-label="Розділи картки" className="flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-800">
      {tabs.map((t, i) => {
        const active = t.key === current;
        return (
          <button
            key={t.key}
            ref={(el) => { refs.current[i] = el; }}
            type="button"
            role="tab"
            id={`${idBase}-tab-${t.key}`}
            aria-selected={active}
            aria-controls={`${idBase}-panel`}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(t.key)}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight") move(i + 1);
              else if (e.key === "ArrowLeft") move(i - 1);
              else if (e.key === "Home") move(0);
              else if (e.key === "End") move(tabs.length - 1);
              else return;
              e.preventDefault();
            }}
            className={cx(
              "-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium",
              active ? "border-brand-700 text-brand-800 dark:text-white" : "border-transparent text-slate-600 hover:text-slate-900 dark:text-slate-400",
            )}
          >
            {t.label}
            {!!t.badge && <span className="rounded-full bg-amber-500 px-1.5 text-xs text-white">{t.badge}</span>}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ idBase, current, children }: { idBase: string; current: string; children: ReactNode }) {
  return (
    <div role="tabpanel" id={`${idBase}-panel`} aria-labelledby={`${idBase}-tab-${current}`} tabIndex={0} className="pt-4 focus:outline-none">
      {children}
    </div>
  );
}
