import { useEffect, useState } from 'react'
import { Link, NavLink } from 'react-router-dom'
import {
  Boxes,
  ClipboardCheck,
  Home,
  LogOut,
  MessageSquareText,
  PanelLeft,
  Plug,
  Share2,
  SquareStack,
  type LucideIcon,
} from 'lucide-react'

import { cn } from '@/lib/utils'

interface NavItem {
  to: string
  label: string
  short: string
  icon: LucideIcon
}

const PRIMARY: NavItem[] = [
  { to: '/home', label: 'Home', short: 'Home', icon: Home },
  { to: '/graph', label: 'Knowledge graph', short: 'Graph', icon: Share2 },
  { to: '/ask', label: 'Ask Copilot', short: 'Ask', icon: MessageSquareText },
  { to: '/workflows', label: 'Workflows', short: 'Work', icon: SquareStack },
]

const SECONDARY: NavItem[] = [
  { to: '/connectors', label: 'Connectors', short: 'Sources', icon: Plug },
  { to: '/assets', label: 'Assets', short: 'Assets', icon: Boxes },
  { to: '/review', label: 'Review queue', short: 'Review', icon: ClipboardCheck },
]

function Brand() {
  return (
    <div className="flex select-none items-center gap-2.5">
      <span
        aria-hidden
        className="flex size-6 shrink-0 items-center justify-center bg-primary font-display text-xs leading-none text-primary-foreground"
      >
        A
      </span>
      <span className="leading-none">
        <span className="block font-display text-base uppercase tracking-tight">
          Atlas
        </span>
        <span className="mt-1 block text-[9px] uppercase tracking-[0.24em] text-muted-foreground">
          Asset Intelligence
        </span>
      </span>
    </div>
  )
}

function SideNavLink({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const Icon = item.icon
  return (
    <NavLink
      to={item.to}
      title={collapsed ? item.label : undefined}
      className={({ isActive }) =>
        cn(
          'group relative flex items-center text-xs font-medium uppercase tracking-[0.14em] transition-colors',
          collapsed ? 'justify-center px-0 py-2.5' : 'gap-3 px-3 py-2.5',
          isActive
            ? 'bg-secondary text-foreground'
            : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
        )
      }
    >
      {({ isActive }) => (
        <>
          <span
            className={cn(
              'absolute left-0 top-1/2 h-5 w-1 -translate-y-1/2 bg-primary transition-opacity',
              isActive ? 'opacity-100' : 'opacity-0',
            )}
            aria-hidden
          />
          <Icon
            className={cn('size-4.5 shrink-0', isActive && 'text-primary')}
            aria-hidden
          />
          {!collapsed && item.label}
        </>
      )}
    </NavLink>
  )
}

function BottomTab({ item }: { item: NavItem }) {
  const Icon = item.icon
  return (
    <NavLink
      to={item.to}
      className={({ isActive }) =>
        cn(
          'flex flex-1 flex-col items-center justify-center gap-1 py-1.5 text-[10px] font-medium uppercase tracking-wider transition-colors',
          isActive ? 'text-primary' : 'text-muted-foreground',
        )
      }
    >
      <Icon className="size-5" aria-hidden />
      {item.short}
    </NavLink>
  )
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (typeof window === 'undefined') return false
    return localStorage.getItem('atlas.sidebar.collapsed') === '1'
  })
  useEffect(() => {
    localStorage.setItem('atlas.sidebar.collapsed', collapsed ? '1' : '0')
  }, [collapsed])

  return (
    <div
      className="min-h-svh"
      style={{ ['--sidebar-w' as string]: collapsed ? '4.5rem' : '16rem' }}
    >
      {/* Desktop sidebar — a solid instrument panel, collapsible to a rail */}
      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-30 hidden flex-col gap-6 border-r border-border bg-card py-5 transition-[width,padding] duration-200 ease-out md:flex',
          collapsed ? 'w-[4.5rem] px-2' : 'w-64 px-4',
        )}
      >
        <div
          className={cn(
            'flex items-center',
            collapsed ? 'justify-center' : 'justify-between px-2',
          )}
        >
          {!collapsed && <Brand />}
          <button
            onClick={() => setCollapsed((c) => !c)}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            className="flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <PanelLeft className="size-4.5" />
          </button>
        </div>
        <nav className="flex flex-col gap-1">
          {PRIMARY.map((item) => (
            <SideNavLink key={item.to} item={item} collapsed={collapsed} />
          ))}
        </nav>
        <div className="flex flex-col gap-1">
          {!collapsed && (
            <p className="px-3 pb-1 text-[10px] font-medium uppercase tracking-[0.24em] text-muted-foreground">
              Records
            </p>
          )}
          {SECONDARY.map((item) => (
            <SideNavLink key={item.to} item={item} collapsed={collapsed} />
          ))}
        </div>

        {/* Back to the public landing page */}
        <div className="mt-auto flex flex-col gap-1">
          <Link
            to="/"
            title={collapsed ? 'Exit to landing page' : undefined}
            className={cn(
              'group flex items-center text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground',
              collapsed ? 'justify-center px-0 py-2.5' : 'gap-3 px-3 py-2.5',
            )}
          >
            <LogOut className="size-4.5 shrink-0" aria-hidden />
            {!collapsed && 'Exit to landing'}
          </Link>
        </div>
      </aside>

      {/* Mobile top bar */}
      <header className="sticky top-0 z-30 flex items-center border-b border-border bg-background px-4 py-3 md:hidden">
        <Brand />
      </header>

      {/* Main content — margin follows the sidebar width via the CSS var */}
      <main className="px-4 pb-24 pt-6 transition-[margin] duration-200 ease-out md:ml-[var(--sidebar-w)] md:px-8 md:pb-10 lg:px-12">
        <div className="mx-auto w-full max-w-6xl">{children}</div>
      </main>

      {/* Mobile bottom tab bar */}
      <nav className="fixed inset-x-0 bottom-0 z-30 flex items-stretch gap-1 border-t border-border bg-background px-2 py-1.5 md:hidden">
        {PRIMARY.map((item) => (
          <BottomTab key={item.to} item={item} />
        ))}
      </nav>
    </div>
  )
}
