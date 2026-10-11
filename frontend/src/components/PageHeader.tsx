import { cn } from '@/lib/utils'

/**
 * Standard page header: just the main title, with an optional action slot on
 * the right. `eyebrow` / `description` are accepted for backwards compatibility
 * but no longer rendered (pages show only the title, per design direction).
 */
export function PageHeader({
  title,
  action,
  className,
}: {
  eyebrow?: React.ReactNode
  title: React.ReactNode
  description?: React.ReactNode
  action?: React.ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'mb-8 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between',
        className,
      )}
    >
      <div className="min-w-0">
        {/* Console tag row + display title. eyebrow/description props stay
            accepted for backwards compatibility but are not rendered. */}
        <p className="mb-2 text-[10px] font-medium uppercase tracking-[0.26em] text-muted-foreground">
          [ ATLAS<span className="text-primary">//OPS</span> ]
        </p>
        <h1 className="font-display text-2xl uppercase leading-none tracking-tight sm:text-[1.75rem]">
          {title}
        </h1>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}
