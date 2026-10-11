import { cn } from '@/lib/utils'

/** A slim progress bar. `tone` picks the fill color; defaults to the accent. */
function Progress({
  value,
  className,
  tone = 'primary',
}: {
  value: number
  className?: string
  tone?: 'primary' | 'verified'
}) {
  const clamped = Math.max(0, Math.min(100, value))
  const fill = tone === 'verified' ? 'bg-verified' : 'bg-primary'
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(clamped)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn(
        'h-1.5 w-full overflow-hidden bg-secondary',
        className,
      )}
    >
      <div
        className={cn('h-full transition-[width] duration-500', fill)}
        style={{
          width: `${clamped}%`,
          transitionTimingFunction: 'var(--ease-out-quint)',
        }}
      />
    </div>
  )
}

export { Progress }
