import React, { useEffect, useMemo, useState } from 'react'
import { motion } from 'motion/react'
import { cn } from '../../lib/utils'

export default function BlurText({
  text = '',
  className,
  delay = 40,
  animateBy = 'words',
}) {
  const units = useMemo(
    () => (animateBy === 'chars' ? text.split('') : text.split(' ')),
    [text, animateBy]
  )
  const [ready, setReady] = useState(false)

  useEffect(() => {
    setReady(true)
  }, [])

  return (
    <span className={cn('inline-flex flex-wrap', className)}>
      {units.map((unit, i) => (
        <motion.span
          key={`${unit}-${i}`}
          initial={{ opacity: 0, filter: 'blur(8px)', y: 8 }}
          animate={ready ? { opacity: 1, filter: 'blur(0px)', y: 0 } : undefined}
          transition={{ duration: 0.45, delay: (i * delay) / 1000, ease: 'easeOut' }}
          className="mr-[0.28em] inline-block last:mr-0"
        >
          {unit === ' ' ? '\u00A0' : unit}
        </motion.span>
      ))}
    </span>
  )
}
