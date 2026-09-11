import React, { useState } from 'react'
import { FiSend, FiShield, FiFilter, FiGitBranch, FiPackage, FiBookOpen } from 'react-icons/fi'
import Modal from '../ui/Modal'
import WebhooksPanel from '../settings/WebhooksPanel'
import StatusChecksPanel from './StatusChecksPanel'
import ServerHooksPanel from './ServerHooksPanel'
import BranchManagerPanel from './BranchManagerPanel'
import ReleasesPanel from './ReleasesPanel'
import DocsPanel from './DocsPanel'
import { cn } from '../../lib/utils'

/**
 * Everything that belongs to one repository, in one place: its branches, its
 * releases, its generated documentation, what it tells the outside world
 * (webhooks), what the outside world tells it (status checks), and what it
 * refuses to accept (pre-receive hooks).
 *
 * These live on the repository rather than in global Settings because that is
 * where their blast radius is — a hook here can reject every push to this
 * repository and nothing beyond it.
 */

const TABS = [
  { id: 'branches', label: 'Branches', icon: FiGitBranch },
  { id: 'releases', label: 'Releases', icon: FiPackage },
  { id: 'docs', label: 'Docs', icon: FiBookOpen },
  { id: 'webhooks', label: 'Webhooks', icon: FiSend },
  { id: 'checks', label: 'Status checks', icon: FiShield },
  { id: 'hooks', label: 'Push rules', icon: FiFilter },
]

export default function AutomationModal({ repo, headCommitId, canManage, open, onClose }) {
  const [tab, setTab] = useState('branches')

  if (!repo) return null

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Manage — ${repo.name}`}
      subtitle="Branches, releases, docs, integrations and push policy for this repository."
      panelClassName="max-w-4xl"
    >
      <div className="mb-5 flex flex-wrap gap-2">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={cn(
              'flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-[13px] transition-colors',
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

      <div className="max-h-[65vh] overflow-y-auto pr-1">
        {tab === 'branches' && <BranchManagerPanel repoId={repo.id} canManage={canManage} />}
        {tab === 'releases' && <ReleasesPanel repoId={repo.id} canWrite={canManage} />}
        {tab === 'docs' && <DocsPanel repoId={repo.id} canWrite={canManage} />}
        {tab === 'webhooks' && <WebhooksPanel repositoryId={repo.id} />}
        {tab === 'checks' && (
          <StatusChecksPanel repoId={repo.id} commitId={headCommitId} canManage={canManage} />
        )}
        {tab === 'hooks' && <ServerHooksPanel repoId={repo.id} canManage={canManage} />}
      </div>
    </Modal>
  )
}
