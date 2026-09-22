import React, { useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { FiX } from 'react-icons/fi'
import { cn } from '../../lib/utils'

/**
 * A dialog that stacks correctly when opened from inside another dialog.
 *
 * Every modal used to render at a fixed z-50, so a modal opened from within
 * another one sat at the same level as the outer backdrop — which covers the
 * whole viewport and therefore swallowed every click meant for the inner
 * dialog. Nested modals were visible and completely inert.
 *
 * Depth is tracked here rather than passed in by callers, because a component
 * generally cannot know whether something above it happens to be a modal.
 */

// Module-level: shared by every mounted dialog, which is exactly the scope
// "how many dialogs are currently open" needs.
let openModals = 0

/**
 * Join the shared dialog stack. Returns a z-index that sits above every
 * dialog opened before this one. `onClose` is read from a ref so a parent
 * re-render does not tear this dialog down and hand its slot to a child.
 */
export function useModalStack(open, onClose) {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const [depth, setDepth] = useState(0)

  useLayoutEffect(() => {
    if (!open) {
      setDepth(0)
      return undefined
    }

    openModals += 1
    const myDepth = openModals
    setDepth(myDepth)

    const onKey = (e) => {
      // Only the topmost dialog reacts, or one Escape would close the whole
      // stack at once.
      if (e.key === 'Escape' && myDepth === openModals) onCloseRef.current?.()
    }
    window.addEventListener('keydown', onKey)

    // Stop the page behind from scrolling while a dialog is open. Saving and
    // restoring the previous value keeps nesting correct: the inner dialog
    // restores 'hidden' (the outer is still open), and the outer restores ''.
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      openModals = Math.max(0, openModals - 1)
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [open])

  return 50 + Math.max(depth, 1) * 10
}

/** Full-screen overlay that portals to document.body and stacks above other dialogs. */
export function ModalOverlay({ open = true, onClose, children, className }) {
  const zIndex = useModalStack(open, onClose)
  if (!open) return null

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      style={{ zIndex }}
      className={cn(
        'fixed inset-0 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm',
        className
      )}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose?.()
      }}
    >
      {children}
    </div>,
    document.body
  )
}

export default function Modal({
  open,
  onClose,
  children,
  title,
  subtitle,
  className,
  panelClassName,
  bodyClassName,
  showClose = true,
}) {
  const zIndex = useModalStack(open, onClose)

  if (!open) return null

  // Rendered into <body> rather than in place. A modal opened from inside the
  // sticky header was trapped in that header's z-10 stacking context, so its own
  // z-index could not lift it above the page and main content painted straight
  // through the panel. Portalling makes a fixed, full-screen overlay behave the
  // same wherever it is declared.
  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      style={{ zIndex }}
      className={cn(
        'fixed inset-0 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm',
        className
      )}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose?.()
      }}
    >
      <div
        className={cn(
          'panel-float-lg hairline-top animate-rise-in relative flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden',
          panelClassName
        )}
      >
        {(title || showClose) && (
          <div className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-6 py-4">
            <div className="min-w-0">
              {title && (
                <h2 className="font-display text-lg font-medium tracking-tight text-ink">
                  {title}
                </h2>
              )}
              {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
            </div>
            {showClose && (
              <button
                onClick={onClose}
                aria-label="Close"
                className="-mr-1 shrink-0 rounded-lg p-1.5 text-muted transition hover:bg-white/[0.07] hover:text-ink"
              >
                <FiX className="h-4 w-4" />
              </button>
            )}
          </div>
        )}
        <div className={cn('min-h-0 flex-1 overflow-y-auto p-6', bodyClassName)}>{children}</div>
      </div>
    </div>,
    document.body
  )
}
