import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { ErrorBanner } from '../common/ErrorBanner'
import { ReconnectBanner } from '../common/ReconnectBanner'

const NAV: { to: string; label: string; end?: boolean }[] = [
  { to: '/', label: '会话', end: true },
  { to: '/confirm', label: '确认' },
  { to: '/workbench', label: '工作台' },
  { to: '/debug', label: '调试' },
  { to: '/workspaces', label: '工作区' },
  { to: '/settings', label: '设置' },
]

function isChatRoute(pathname: string): boolean {
  return (
    pathname === '/' ||
    pathname === '/session' ||
    pathname === '/chat'
  )
}

export function AppShell() {
  const { pathname } = useLocation()
  const chat = isChatRoute(pathname)

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <header className="shrink-0 border-b border-stone-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3">
          <div className="text-base font-semibold tracking-tight text-stone-900">
            TesterAgent
          </div>
          <nav className="flex flex-wrap gap-1 text-sm">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  [
                    'rounded px-2.5 py-1',
                    isActive
                      ? 'bg-stone-900 text-white'
                      : 'text-stone-600 hover:bg-stone-100',
                  ].join(' ')
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <ReconnectBanner />
      <ErrorBanner />
      <main
        className={
          chat
            ? 'mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col px-4'
            : 'mx-auto min-h-0 w-full max-w-6xl flex-1 overflow-y-auto px-4 py-6'
        }
      >
        <Outlet />
      </main>
    </div>
  )
}
