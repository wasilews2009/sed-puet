/**
 * Каркас: посилання «До змісту», шапка з навігацією за ролями і лічильниками черг, підвал з нагадуванням про HITL.
 * Після кожного переходу фокус переводиться на <main>, а заголовок вкладки оновлюється (WCAG 2.4.2, 2.4.3).
 */
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { documentsApi } from "../api/endpoints";
import { navigation } from "../domain/access";
import { cx } from "../components/ui";
import { useApp } from "./context";

export function Layout() {
  const { me, dict, logout, threshold } = useApp();
  const items = navigation(me, dict);
  const location = useLocation();
  const main = useRef<HTMLElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const first = useRef(true);

  const inbox = useQuery({ queryKey: ["inbox"], queryFn: documentsApi.inbox, enabled: dict.modules.routing, refetchInterval: 60_000 });
  const review = useQuery({ queryKey: ["review-queue"], queryFn: documentsApi.reviewQueue, enabled: me.roles.length > 0, refetchInterval: 60_000 });
  const counts = { inbox: inbox.data?.length ?? 0, review: review.data?.length ?? 0 };

  useEffect(() => {
    setMenuOpen(false);
    if (first.current) {
      first.current = false;
      return;
    }
    main.current?.focus({ preventScroll: false });
  }, [location.pathname]);

  const roles = me.roles.map((r) => r.role_label + (r.org_unit_name ? ` (${r.org_unit_name})` : "")).join(", ");

  return (
    <div className="flex min-h-screen flex-col">
      <a href="#main" onClick={(e) => { e.preventDefault(); main.current?.focus(); }}
        className="sr-only focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:text-brand-700 focus:shadow">
        Перейти до змісту
      </a>
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <NavLink to="/" className="flex items-center gap-2 text-slate-900 no-underline hover:no-underline dark:text-slate-100">
            <span aria-hidden="true" className="grid h-9 w-9 place-items-center rounded-md bg-brand-700 text-sm font-bold text-white">СЕД</span>
            <span className="leading-tight">
              <span className="block font-semibold">СЕД ПУЕТ</span>
              <span className="block text-xs text-slate-600 dark:text-slate-400">документообіг з ШІ-асистентом</span>
            </span>
          </NavLink>
          <div className="ml-auto flex items-center gap-3">
            <div className="text-right leading-tight">
              <div className="text-sm font-semibold">{me.display_name}</div>
              <div className="max-w-xs truncate text-xs text-slate-600 dark:text-slate-400" title={roles}>{roles || "ролей не призначено"}</div>
            </div>
            <button type="button" onClick={logout} className="text-sm text-brand-700 underline-offset-2 hover:underline dark:text-brand-100">Вийти</button>
          </div>
          <button type="button" className="rounded border border-slate-300 px-3 py-1 text-sm md:hidden"
            aria-expanded={menuOpen} aria-controls="main-nav" onClick={() => setMenuOpen((v) => !v)}>
            Меню
          </button>
          <nav id="main-nav" aria-label="Розділи" className={cx("w-full md:block", menuOpen ? "block" : "hidden")}>
            <ul className="flex flex-col gap-1 md:flex-row md:flex-wrap">
              {items.map((it) => {
                const n = it.badge ? counts[it.badge] : 0;
                return (
                  <li key={it.key}>
                    <NavLink to={it.to} end={it.to === "/"}
                      className={({ isActive }) => cx(
                        "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium text-slate-700 no-underline hover:bg-slate-100 hover:no-underline dark:text-slate-200 dark:hover:bg-slate-800",
                        isActive && "bg-brand-50 text-brand-800 dark:bg-slate-800 dark:text-white",
                      )}>
                      {it.label}
                      {n > 0 && (
                        <span className="rounded-full bg-brand-700 px-1.5 text-xs text-white" aria-label={`${n} очікують`}>{n}</span>
                      )}
                    </NavLink>
                  </li>
                );
              })}
            </ul>
          </nav>
        </div>
      </header>
      {me.roles.length === 0 && (
        <div className="mx-auto mt-4 w-full max-w-7xl px-4">
          <div className="rounded-md border-l-4 border-amber-400 bg-amber-50 px-4 py-3 text-sm dark:bg-amber-950">
            Вам ще не призначено жодної ролі в СЕД. Ви бачите лише публічні документи; зверніться до адміністратора, щоб отримати доступ до документів підрозділу.
          </div>
        </div>
      )}
      <main id="main" ref={main} tabIndex={-1} className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 outline-none">
        <Outlet />
      </main>
      <footer className="border-t border-slate-200 bg-white py-3 text-center text-xs text-slate-600 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
        ШІ є асистентом: реквізити і зауваження потребують перевірки людиною (поріг впевненості {Math.round(threshold * 100)}%). Юридичної сили документ набуває лише після накладання КЕП посадовою особою.
      </footer>
    </div>
  );
}

/** Заголовок вкладки браузера для поточної сторінки. */
export function useTitle(title: string) {
  useEffect(() => {
    document.title = `${title} — СЕД ПУЕТ`;
  }, [title]);
}
