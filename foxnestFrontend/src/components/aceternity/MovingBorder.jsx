import React from 'react'
import { motion } from 'motion/react'
import { cn } from '../../lib/utils'

/**
 * A button whose border has a light travelling around it — used for the single
 * most important action on a screen, never more than one at a time.
 *
 * The sweep is the accent fading into the panel colour, so it reads as light
 * moving over an edge rather than as a second brand colour.
 */
export default function MovingBorder({
  children,
  className,
  containerClassName,
  borderRadius = '9999px',
  duration = 3.5,
  as: Component = 'button',
  ...props
}) {
  return (
    <Component
      className={cn(
        'group relative inline-flex overflow-hidden p-[1.5px] text-sm font-medium',
        'transition-transform duration-200 active:scale-[0.99]',
        containerClassName
      )}
      style={{ borderRadius }}
      {...props}
    >
      <div className="absolute inset-0" style={{ borderRadius }}>
        <motion.div
          className="absolute inset-[-100%] size-[200%]"
          style={{
            background:
              'conic-gradient(from 0deg, transparent 0deg, #6fd3ff 60deg, #3b9dff 120deg, transparent 210deg)',
          }}
          animate={{ rotate: 360 }}
          transition={{ duration, repeat: Infinity, ease: 'linear' }}
        />
      </div>
      <div
        className={cn(
          'relative z-10 flex w-full items-center justify-center gap-2 bg-accent px-6 py-3',
          'font-semibold text-[#04070e] transition-[filter] duration-200 group-hover:brightness-110',
          className
        )}
        style={{ borderRadius: `calc(${borderRadius} - 1.5px)` }}
      >
        {children}
      </div>
    </Component>
  )
}
