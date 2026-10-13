import React from 'react'
import PageHeader from '../components/ui/PageHeader'
import ActivityBrowser from '../components/ActivityBrowser'

/**
 * The activity log: who did what, to which repository, when, and how they were signed in.
 *
 * Everyone sees what happened in the repositories they can read, plus their own
 * account's events; administrators see everything and can export it. Filters are in
 * the address, so a filtered view can be shared.
 */
export default function Activity({ isAdmin = false }) {
  return (
    <div className="p-6">
      <PageHeader
        eyebrow="Audit"
        title="Activity"
        subtitle={
          isAdmin
            ? 'Everything that happened on this server — who did it, to which repository, when, and with which credential.'
            : 'What happened in the repositories you can read, and on your own account.'
        }
      />
      <ActivityBrowser useUrl isAdmin={isAdmin} />
    </div>
  )
}
