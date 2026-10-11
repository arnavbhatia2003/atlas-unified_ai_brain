import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'

import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center gap-1.5 border border-transparent px-2 py-0.5 font-mono text-[10px] font-medium uppercase tracking-[0.14em] whitespace-nowrap',
  {
    variants: {
      variant: {
        neutral: 'bg-secondary text-secondary-foreground',
        outline: 'border border-border text-muted-foreground',
        verified: 'bg-verified-soft text-verified',
        review: 'bg-review-soft text-review',
        linked: 'bg-linked-soft text-linked',
        minor: 'bg-minor-soft text-minor',
        critical: 'bg-critical-soft text-critical',
        primary: 'bg-primary/12 text-primary',
      },
    },
    defaultVariants: { variant: 'neutral' },
  },
)

function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<'span'> & VariantProps<typeof badgeVariants>) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props} />
  )
}

/** A small status dot + label, e.g. "● critical". */
function StatusDot({
  className,
  tone = 'primary',
  ...props
}: React.ComponentProps<'span'> & {
  tone?: 'primary' | 'verified' | 'review' | 'linked' | 'minor' | 'critical'
}) {
  const color: Record<string, string> = {
    primary: 'bg-primary',
    verified: 'bg-verified',
    review: 'bg-review',
    linked: 'bg-linked',
    minor: 'bg-minor',
    critical: 'bg-critical',
  }
  return (
    <span
      className={cn('inline-block size-2', color[tone], className)}
      {...props}
    />
  )
}

export { Badge, badgeVariants, StatusDot }
