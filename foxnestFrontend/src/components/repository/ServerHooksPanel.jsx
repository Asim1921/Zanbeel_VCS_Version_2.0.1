import React, { useCallback, useEffect, useState } from 'react'
import {
  FiFilter,
  FiPlus,
  FiTrash2,
  FiAlertTriangle,
  FiRefreshCw,
  FiPlay,
  FiCheckCircle,
  FiXCircle,
} from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Modal from '../ui/Modal'
import Input from '../ui/Input'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'

/**
 * Pre-receive push rules.
 *
 * Presented as named fields rather than a raw JSON box, because the point of
 * declarative rules is that someone can read them back and know what they do.
 * The dry-run is prominent for the same reason a hook is dangerous: a rule that
 * does not mean what its author thought starts rejecting colleagues' work.
 */

const RULE_FIELDS = [
  {
    key: 'commit_message_min_length',
    label: 'Minimum message length',
    type: 'number',
    placeholder: 'e.g. 20',
    help: 'Reject commits whose message is shorter than this.',
  },
  {
    key: 'commit_message_pattern',
    label: 'Message must match (regex)',
    type: 'text',
    placeholder: '^\\[(FIX|FEAT)\\]',
    help: 'Enforce a ticket prefix or a conventional-commit format.',
  },
  {
    key: 'forbidden_paths',
    label: 'Forbidden paths',
    type: 'list',
    placeholder: '*.env, secrets/**, *.pem',
    help: 'Gitignore-style globs that may never be pushed.',
  },
  {
    key: 'protected_paths',
    label: 'Protected paths',
    type: 'list',
    placeholder: 'deploy/*, generated/**',
    help: 'Only the users below may change these. Leave the users empty to freeze them entirely.',
  },
  {
    key: 'protected_paths_allowed_users',
    label: 'Who may change protected paths',
    type: 'list',
    placeholder: 'release-bot, alice',
    help: '',
  },
  {
    key: 'max_file_bytes',
    label: 'Max file size (bytes)',
    type: 'number',
    placeholder: 'e.g. 1048576',
    help: 'Stricter than the server-wide limit, for this repository only.',
  },
  {
    key: 'forbidden_content',
    label: 'Forbidden content (regexes)',
    type: 'list',
    placeholder: 'AKIA[0-9A-Z]{16}, BEGIN RSA PRIVATE KEY',
    help: 'Scanned inside text files — catches committed secrets. Binary files are skipped.',
  },
  {
    key: 'external_url',
    label: 'External policy service',
    type: 'text',
    placeholder: 'https://policy.example.com/pre-receive',
    help: 'POSTed the commit message and file paths (never file contents). Must answer {"allow": true}. An unreachable service rejects the push.',
  },
]

const LIST_KEYS = new Set(
  RULE_FIELDS.filter((f) => f.type === 'list').map((f) => f.key)
)

function summarise(config) {
  const parts = []
  if (config.commit_message_min_length)
    parts.push(`message ≥ ${config.commit_message_min_length} chars`)
  if (config.commit_message_pattern) parts.push(`message matches ${config.commit_message_pattern}`)
  if (config.forbidden_paths?.length)
    parts.push(`${config.forbidden_paths.length} forbidden path(s)`)
  if (config.protected_paths?.length)
    parts.push(`${config.protected_paths.length} protected path(s)`)
  if (config.max_file_bytes) parts.push(`files ≤ ${config.max_file_bytes} bytes`)
  if (config.forbidden_content?.length)
    parts.push(`${config.forbidden_content.length} content pattern(s)`)
  if (config.external_url) parts.push('external policy service')
  return parts.length ? parts.join(' · ') : 'no rules — accepts everything'
}

export default function ServerHooksPanel({ repoId = null, canManage = false }) {
  const [hooks, setHooks] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [form, setForm] = useState({})
  const [saving, setSaving] = useState(false)

  const [testHook, setTestHook] = useState(null)
  const [testMessage, setTestMessage] = useState('')
  const [testPaths, setTestPaths] = useState('')
  const [testResult, setTestResult] = useState(null)
  const [testing, setTesting] = useState(false)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await api.listServerHooks(repoId)
      setHooks(response.hooks || [])
    } catch (err) {
      setError(err.message || 'Failed to load push rules')
    } finally {
      setLoading(false)
    }
  }, [repoId])

  useEffect(() => {
    load()
  }, [load])

  const buildConfig = () => {
    const config = {}
    for (const field of RULE_FIELDS) {
      const raw = (form[field.key] ?? '').toString().trim()
      if (!raw) continue
      if (field.type === 'number') config[field.key] = Number(raw)
      else if (field.type === 'list')
        config[field.key] = raw.split(',').map((s) => s.trim()).filter(Boolean)
      else config[field.key] = raw
    }
    return config
  }

  const handleCreate = async () => {
    if (!name.trim()) return
    try {
      setSaving(true)
      setError(null)
      await api.createServerHook({
        name: name.trim(),
        repository_id: repoId,
        hook_type: 'pre-receive',
        config: buildConfig(),
      })
      setCreateOpen(false)
      setName('')
      setForm({})
      await load()
    } catch (err) {
      setError(err.message || 'Failed to create the rule')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (hook) => {
    if (!window.confirm(`Delete the push rule "${hook.name}"?`)) return
    try {
      await api.deleteServerHook(hook.id)
      await load()
    } catch (err) {
      setError(err.message || 'Failed to delete the rule')
    }
  }

  const handleToggle = async (hook) => {
    try {
      await api.updateServerHook(hook.id, { enabled: !hook.enabled })
      await load()
    } catch (err) {
      setError(err.message || 'Failed to update the rule')
    }
  }

  const runTest = async () => {
    try {
      setTesting(true)
      setTestResult(null)
      const paths = testPaths.split(',').map((s) => s.trim()).filter(Boolean)
      const response = await api.testServerHook(testHook.id, { message: testMessage, paths })
      setTestResult(response)
    } catch (err) {
      setError(err.message || 'Dry run failed')
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-xl font-light tracking-tight text-ink">
            Push rules
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Pre-receive policy applied to every push, including from a modified client.
            Rules are declarative, not scripts — the server never executes uploaded code.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={load} title="Refresh" className="!px-2">
            <FiRefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
          {canManage && (
            <Button onClick={() => setCreateOpen(true)} className="flex items-center gap-2">
              <FiPlus className="h-4 w-4" />
              Add rule
            </Button>
          )}
        </div>
      </div>

      {error && (
        <GlassCard className="flex items-center gap-2 border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
          <FiAlertTriangle className="h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{error}</span>
        </GlassCard>
      )}

      {loading ? (
        <div className="space-y-3">
          {[0, 1].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-xl bg-white/[0.04]" />
          ))}
        </div>
      ) : hooks.length === 0 ? (
        <EmptyState
          icon={FiFilter}
          title="No push rules"
          description="Every push is accepted as long as it passes the server-wide size and ignore checks."
          action={canManage ? <Button onClick={() => setCreateOpen(true)}>Add a rule</Button> : null}
        />
      ) : (
        <div className="space-y-2">
          {hooks.map((hook) => (
            <GlassCard
              key={hook.id}
              hover={false}
              className={`flex flex-wrap items-center gap-3 p-4 ${hook.enabled ? '' : 'opacity-60'}`}
            >
              <div
                className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border ${
                  hook.enabled
                    ? 'border-accent/30 bg-accent/10 text-accent'
                    : 'border-border bg-surface text-muted'
                }`}
              >
                <FiFilter className="h-4 w-4" />
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-medium text-ink">{hook.name}</span>
                  <Badge variant={hook.enabled ? 'success' : 'default'}>
                    {hook.enabled ? 'Enforced' : 'Disabled'}
                  </Badge>
                  {hook.repository_id === null && <Badge variant="info">Server-wide</Badge>}
                </div>
                <p className="mt-1 text-xs text-muted">{summarise(hook.config)}</p>
              </div>

              <div className="flex shrink-0 items-center gap-1.5">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setTestHook(hook)
                    setTestResult(null)
                    setTestMessage('')
                    setTestPaths('')
                  }}
                  className="flex items-center gap-1.5"
                >
                  <FiPlay className="h-3.5 w-3.5" />
                  Dry run
                </Button>
                {canManage && (
                  <>
                    <Button variant="ghost" size="sm" onClick={() => handleToggle(hook)}>
                      {hook.enabled ? 'Disable' : 'Enable'}
                    </Button>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => handleDelete(hook)}
                      className="!px-2"
                      title="Delete"
                    >
                      <FiTrash2 className="h-3.5 w-3.5" />
                    </Button>
                  </>
                )}
              </div>
            </GlassCard>
          ))}
        </div>
      )}

      {/* --- create --- */}
      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="New push rule"
        subtitle="Leave a field blank to skip that rule."
      >
        <div className="space-y-5">
          <Input
            label="Rule name"
            placeholder="no-secrets"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />

          <div className="max-h-[45vh] space-y-4 overflow-y-auto pr-1">
            {RULE_FIELDS.map((field) => (
              <div key={field.key}>
                <label className="mb-1.5 block text-[13px] font-medium text-ink-soft">
                  {field.label}
                </label>
                <input
                  type={field.type === 'number' ? 'number' : 'text'}
                  value={form[field.key] ?? ''}
                  onChange={(e) => setForm((f) => ({ ...f, [field.key]: e.target.value }))}
                  placeholder={field.placeholder}
                  className="w-full rounded-lg border border-border bg-cream-mid px-3 py-2 font-mono text-[13px] text-ink transition-colors placeholder:text-muted focus:border-accent focus:outline-none"
                />
                {field.help && <p className="mt-1 text-xs text-muted">{field.help}</p>}
                {LIST_KEYS.has(field.key) && (
                  <p className="mt-0.5 text-[11px] text-muted">Comma-separated.</p>
                )}
              </div>
            ))}
          </div>

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="secondary" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={!name.trim() || saving}>
              {saving ? 'Creating…' : 'Create rule'}
            </Button>
          </div>
        </div>
      </Modal>

      {/* --- dry run --- */}
      <Modal
        open={Boolean(testHook)}
        onClose={() => setTestHook(null)}
        title={`Dry run — ${testHook?.name || ''}`}
        subtitle="Find out what this rule does before it starts rejecting real pushes."
      >
        <div className="space-y-4">
          <Input
            label="Commit message"
            placeholder="[FIX] correct the thing"
            value={testMessage}
            onChange={(e) => setTestMessage(e.target.value)}
          />
          <Input
            label="File paths"
            placeholder="src/main.py, .env"
            value={testPaths}
            onChange={(e) => setTestPaths(e.target.value)}
            hint="Comma-separated. Content and size rules cannot be exercised without file bodies."
          />

          {testResult && (
            <div
              className={`flex items-start gap-2 rounded-lg border p-3 text-sm ${
                testResult.would_accept
                  ? 'border-success-fg/25 bg-success-bg text-success-fg'
                  : 'border-danger-fg/25 bg-danger-bg text-danger-fg'
              }`}
            >
              {testResult.would_accept ? (
                <FiCheckCircle className="mt-0.5 h-4 w-4 shrink-0" />
              ) : (
                <FiXCircle className="mt-0.5 h-4 w-4 shrink-0" />
              )}
              <div className="min-w-0">
                <p className="font-medium">
                  {testResult.would_accept ? 'This push would be accepted' : 'This push would be rejected'}
                </p>
                {testResult.reasons?.length > 0 && (
                  <ul className="mt-1.5 list-inside list-disc space-y-0.5">
                    {testResult.reasons.map((reason, i) => (
                      <li key={i} className="break-words">
                        {reason}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}

          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setTestHook(null)}>
              Close
            </Button>
            <Button onClick={runTest} disabled={testing}>
              {testing ? 'Running…' : 'Run'}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
