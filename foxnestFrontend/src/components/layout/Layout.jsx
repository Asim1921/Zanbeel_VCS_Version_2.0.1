import React, { useEffect, useState } from 'react'
import Header from './Header'
import Sidebar from './Sidebar'
import DotBackground from '../aceternity/DotBackground'
import { cn } from '../../lib/utils'

const Layout = ({ children, activeTab, setActiveTab, currentUser, onLogout }) => {
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    if (typeof window === 'undefined') return true
    return window.innerWidth >= 1024
  })

  // Desktop: remember preference; Mobile: always drawer (closed by default on small)
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)')
    const apply = () => {
      if (mq.matches) {
        // entering desktop — expand if previously unknown
        setSidebarOpen((prev) => (prev === false ? true : prev))
      } else {
        setSidebarOpen(false)
      }
    }
    apply()
    mq.addEventListener('change', apply)
    return () => mq.removeEventListener('change', apply)
  }, [])

  return (
    <div className="app-canvas relative min-h-dvh">
      <DotBackground className="opacity-80" />

      <Sidebar
        isOpen={sidebarOpen}
        setIsOpen={setSidebarOpen}
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        currentUser={currentUser}
      />

      <div
        className={cn(
          'relative z-10 min-h-dvh min-w-0 transition-[margin] duration-300 ease-out',
          'ml-0',
          sidebarOpen ? 'lg:ml-[288px]' : 'lg:ml-[84px]'
        )}
      >
        <Header
          sidebarOpen={sidebarOpen}
          setSidebarOpen={setSidebarOpen}
          activeTab={activeTab}
          setActiveTab={setActiveTab}
          currentUser={currentUser}
          onLogout={onLogout}
        />

        <main className="min-w-0 px-3 pb-6 pt-3 sm:px-4 md:px-6 md:pb-8 md:pt-4">
          <div className="mx-auto w-full min-w-0 max-w-7xl">{children}</div>
        </main>
      </div>

      {/* Mobile overlay */}
      <div
        className={cn(
          'fixed inset-0 z-20 bg-black/60 backdrop-blur-sm transition-opacity duration-300 lg:hidden',
          sidebarOpen ? 'opacity-100' : 'pointer-events-none opacity-0'
        )}
        onClick={() => setSidebarOpen(false)}
        aria-hidden={!sidebarOpen}
      />
    </div>
  )
}

export default Layout
