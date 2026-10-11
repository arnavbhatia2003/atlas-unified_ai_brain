import * as React from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

import { cn } from '@/lib/utils'

/**
 * Lightweight modal rendered in a portal (escapes any overflow/stacking
 * context). Centered on desktop, bottom-sheet on mobile. Closes on Escape or
 * backdrop click. Respects prefers-reduced-motion via tw-animate utilities.
 */
function Modal({
  open,
  onClose,
  title,
  description,
  children,
  className,
}: {
  open: boolean
  onClose: () => void
  title?: React.ReactNode
  description?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  React.useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-end justify-center sm:items-center"
      role="dialog"
      aria-modal="true"
    >
      <div
        className="absolute inset-0 bg-black/70 motion-safe:animate-in motion-safe:fade-in"
        onClick={onClose}
      />
      <div
        className={cn(
          'relative z-10 flex max-h-[88vh] w-full flex-col border border-border bg-card',
          'sm:mx-4 sm:w-full sm:max-w-lg sm:max-h-[85vh]',
          'motion-safe:animate-in motion-safe:slide-in-from-bottom-4 sm:motion-safe:zoom-in-95 sm:motion-safe:slide-in-from-bottom-0',
          className,
        )}
      >
        {(title || description) && (
          <div className="flex items-start justify-between gap-4 px-5 pt-5">
            <div className="space-y-1">
              {title && (
                <h2 className="font-display text-lg uppercase tracking-tight">{title}</h2>
              )}
              {description && (
                <p className="text-sm text-muted-foreground">{description}</p>
              )}
            </div>
            <button
              onClick={onClose}
              aria-label="Close"
              className="-mr-1 -mt-1 flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="size-4" />
            </button>
          </div>
        )}
        {/* Body scrolls when content is taller than the viewport (long OKF bundles). */}
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-5">
          {children}
        </div>
      </div>
    </div>,
    document.body,
  )
}

export { Modal }
