import { useCallback, useEffect, useState } from "react";

export type ThemeChoice = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "llmwiki:theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

const isChoice = (value: unknown): value is ThemeChoice => value === "light" || value === "dark" || value === "system";

/** 저장된 선택. index.html의 부트 스크립트와 같은 키를 읽는다. */
export function storedTheme(): ThemeChoice {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return isChoice(value) ? value : "system";
  } catch {
    return "system";
  }
}

/**
 * 테마 선택을 html[data-theme]에 즉시 반영한다. "system"은 속성을 지우고
 * prefers-color-scheme에 맡긴다 — 참조 콘솔과 같은 3상태 모델이다.
 * effect로 미루면 자식 컴포넌트의 effect가 먼저 돌면서 아직 바뀌지 않은
 * CSS 변수를 읽어 한 박자 늦은 값을 쓰게 된다.
 */
function applyChoice(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.dataset.theme = choice;
}

export function useTheme(): {choice: ThemeChoice; resolved: ResolvedTheme; setChoice: (next: ThemeChoice) => void} {
  const [choice, setChoiceState] = useState<ThemeChoice>(storedTheme);
  const [systemDark, setSystemDark] = useState(() => matchMedia(DARK_QUERY).matches);

  useEffect(() => {
    const media = matchMedia(DARK_QUERY);
    const handler = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    media.addEventListener("change", handler);
    return () => media.removeEventListener("change", handler);
  }, []);

  /* 첫 마운트 동기화. 이후 변경은 setChoice가 즉시 반영한다. */
  useEffect(() => applyChoice(choice), []); // eslint-disable-line react-hooks/exhaustive-deps

  const resolved: ResolvedTheme = choice === "system" ? (systemDark ? "dark" : "light") : choice;

  useEffect(() => {
    const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    if (meta) meta.content = resolved === "dark" ? "#1a1b1f" : "#f4f5f7";
  }, [resolved]);

  const setChoice = useCallback((next: ThemeChoice) => {
    applyChoice(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* 저장에 실패해도 이번 세션의 테마는 유지된다 */
    }
    setChoiceState(next);
  }, []);
  return {choice, resolved, setChoice};
}
