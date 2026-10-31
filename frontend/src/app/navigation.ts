import { useCallback, useEffect, useMemo, useState } from 'react';
import { parentRoute, parseRoute, routePath, type Route } from './route';

export type Navigation = {
  route: Route;
  /** Stable identity of the current location; changes on every navigation including Back/Forward. */
  routeKey: string;
  go: (route: Route) => void;
  close: () => void;
};

/**
 * Binds the Route to the browser History. Navigation never touches Core state: it only changes the URL,
 * and opening a Project or Task does not start an agent.
 */
export function useNavigation(): Navigation {
  const [path, setPath] = useState(() => location.pathname);
  const route = useMemo(() => parseRoute(path), [path]);

  useEffect(() => {
    const onPop = () => setPath(location.pathname);
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const go = useCallback((next: Route) => {
    const target = routePath(next);
    if (target === location.pathname) return;
    history.pushState(null, '', target);
    setPath(target);
  }, []);

  const close = useCallback(() => go(parentRoute(parseRoute(location.pathname))), [go]);

  return { route, routeKey: routePath(route), go, close };
}
