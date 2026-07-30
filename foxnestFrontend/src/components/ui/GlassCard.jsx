import React from "react"

const GlassCard = ({ children, className = '', hover = true, ...props }) => {
  return (
    <div
      className={`
        bg-white/10 backdrop-blur-xl border border-white/20 rounded-2xl
        ${hover ? 'hover:bg-white/15 hover:border-white/30 transition-all duration-300' : ''}
        ${className}
      `}
      {...props}
    >
      {children}
    </div>
  )
}

export default GlassCard
