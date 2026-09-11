import React from 'react'
import { motion } from 'motion/react'
import { cn } from '../../lib/utils'

export default function FadeContent({
  children,
  className,
  delay = 0,
  duration = 0.45,
  y = 12,
}) {
  return (
    <motion.div
      className={cn(className)}
      initial={{ opacity: 0, y }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration, delay, ease: 'easeOut' }}
    >
      {children}
    </motion.div>
  )
}
