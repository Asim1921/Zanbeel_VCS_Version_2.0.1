import React, { useState } from 'react'
import { FiKey, FiSend, FiTerminal } from 'react-icons/fi'
import PageHeader from '../components/ui/PageHeader'
import AccessTokensPanel from '../components/settings/AccessTokensPanel'
import WebhooksPanel from '../components/settings/WebhooksPanel'
import SSHKeysPanel from '../components/settings/SSHKeysPanel'
import { cn } from '../lib/utils'

/**
 * Personal and server-wide settings.
 *
 * Repository-scoped automation lives on the repository itself, since that is
 * where its blast radius is. What appears here is either the signed-in user's
 * own credentials or configuration that spans every repository.
 */

export default function Settings({ isAdmin = false }) {
  const tabs = [
    { id: 'tokens', label: 'Access tokens', icon: FiKey },
    { id: 'ssh', label: 'SSH keys', icon: FiTerminal },
    ...(isAdmin ? [{ id: 'webhooks', label: 'Global webhooks', icon: FiSend }] : []),
  ]

  const [tab, setTab] = useState('tokens')

  return (
    <div className="p-6">
      <PageHeader
        eyebrow="Your account"
        title="Settings"
        subtitle="Credentials for the CLI, build agents and anything else that talks to Zanbeel on your behalf."
      />

      <div className="mb-6 flex flex-wrap gap-2">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={cn(
              'flex items-center gap-2 rounded-full border px-4 py-1.5 text-sm transition-colors',
              tab === id
                ? 'border-accent/50 bg-accent/10 text-ink'
                : 'border-border bg-white/[0.02] text-ink-soft hover:border-border-strong hover:text-ink'
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </div>

      {tab === 'tokens' && <AccessTokensPanel isAdmin={isAdmin} />}
      {tab === 'ssh' && <SSHKeysPanel />}
      {tab === 'webhooks' && isAdmin && <WebhooksPanel repositoryId={null} />}
    </div>
  )
}
