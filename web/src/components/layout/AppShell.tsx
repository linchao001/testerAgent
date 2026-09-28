import { NavLink, Outlet } from 'react-router-dom'
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

export function AppShell() {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b border-stone-200 bg-white">
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
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
