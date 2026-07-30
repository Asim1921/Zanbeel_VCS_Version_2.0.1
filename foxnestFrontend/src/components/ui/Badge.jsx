import React from "react"

const Badge = ({ children, variant = 'default', className = '' }) => {
  const variants = {
    default: 'bg-white/20 text-white',
    success: 'bg-green-500/20 text-green-100 border border-green-400/30',
    warning: 'bg-yellow-500/20 text-yellow-100 border border-yellow-400/30',
    danger: 'bg-red-500/20 text-red-100 border border-red-400/30',
    info: 'bg-blue-500/20 text-blue-100 border border-blue-400/30'
  }
  
  return (
    <span className={`
      inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium backdrop-blur-sm
      ${variants[variant]}
      ${className}
    `}>
      {children}
    </span>
  )
}

export default Badge
