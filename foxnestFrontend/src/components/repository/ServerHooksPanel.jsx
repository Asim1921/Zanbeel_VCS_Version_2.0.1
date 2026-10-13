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
 * Push rules.
 *
 * Applied to every push and to everything else that puts content on a branch —
 * pull-request and branch merges, conflict resolutions, cherry-picks, reverts,
 * rollbacks. Rules judge what a change adds, modifies or deletes, not the whole tree.
 *
 * Presented as named fields rather than a raw JSON box, because the point of
 * declarative rules is that someone can read them back and know what they do.
 * The dry-run is prominent for the same reason a hook is dangerous: a rule that
 * does not mean what its author thought starts rejecting colleagues' work.
 */

const RULE_FIELDS = [
  {
    key: 'block_secrets',
    label: 'Block secrets',
    type: 'bool',
    help: 'Refuse private keys, AWS, GitHub, Slack, Google and Zanbeel tokens, and files such as id_rsa, *.p12 and .env.',
  },
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
    key: 'branch_name_pattern',
    label: 'New branch names must match (regex)',
    type: 'text',
    placeholder: '^(feature|fix|release)/',
    help: 'Checked when a branch is created, by push or in the web interface. Existing branches are unaffected.',
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
    help: 'Scanned inside added and changed text files. Binary files are skipped. Patterns that take too long are stopped and the push is refused.',
  },
  {
    key: 'external_url',
    label: 'External policy service',
    type: 'text',
    placeholder: 'https://policy.example.com/pre-receive',
    help: 'POSTed the commit message and changed and deleted paths (never file contents). Must answer {"allow": true}. An unreachable service rejects the push.',
  },
  {
    key: 'external_secret',
    label: 'Policy service signing secret',
    type: 'secret',
    placeholder: 'at least 16 characters',
    help: 'Signs each request in X-Zanbeel-Signature-256, so the service can tell it really came from Zanbeel.',
  },
]

const LIST_KEYS = new Set(
  RULE_FIELDS.filter((f) => f.type === 'list').map((f) => f.key)
)

function summarise(config, hasSecret) {
  const parts = []
  if (config.block_secrets) parts.push('blocks secrets')
  if (config.branch_name_pattern) parts.push(`new branches match ${config.branch_name_pattern}`)
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
  if (config.external_url) parts.push(hasSecret ? 'external policy service (signed)' : 'external policy service')
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
  const [formError, setFormError] = useState(null)

  const [testHook, setTestHook] = useState(null)
  const [testMessage, setTestMessage] = useState('')
  const [testPaths, setTestPaths] = useState('')
  const [testFilePath, setTestFilePath] = useState('')
  const [testFileText, setTestFileText] = useState('')
  const [testDeleted, setTestDeleted] = useState('')
  const [testBranch, setTestBranch] = useState('')
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
      if (field.type === 'bool') {
        if (form[field.key]) config[field.key] = true
        continue
      }
      const raw = (form[field.key] ?? '').toString().trim()
      if (!raw) continue
      if (field.type === 'number') config[field.key] = Number(raw)
      else if (field.type === 'list')
        config[field.key] = raw.split(',').map((s) => s.trim()).filter(Boolean)
      else config[field.key] = raw
    }
    return config
  }

  const closeCreate = () => {
    setCreateOpen(false)
    setFormError(null)
  }

  const handleCreate = async () => {
    if (!name.trim()) return
    try {
      setSaving(true)
      setFormError(null)
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
      setFormError(err.message || 'Failed to create the rule')
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
      const split = (text) => text.split(',').map((s) => s.trim()).filter(Boolean)
      const files = testFilePath.trim() ? { [testFilePath.trim()]: testFileText } : {}
      const response = await api.testServerHook(testHook.id, {
        message: testMessage,
        paths: split(testPaths),
        files,
        deleted: split(testDeleted),
        branch: testBranch.trim() || null,
        newBranch: Boolean(testBranch.trim()),
      })
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
            Applied to every push and to everything else that lands on a branch — merges,
            conflict resolutions, cherry-picks, reverts and rollbacks. Rules look at what a
            change adds, modifies or deletes. They are declarative, not scripts — the server
            never executes uploaded code.
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
              data-testid="push-rule-row"
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
                  {hook.never_runs && (
                    <Badge variant="warning">post-receive: never runs — use a webhook</Badge>
                  )}
                </div>
                <p className="mt-1 text-xs text-muted">{summarise(hook.config, hook.has_external_secret)}</p>
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
                    setTestFilePath('')
                    setTestFileText('')
                    setTestDeleted('')
                    setTestBranch('')
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
        onClose={closeCreate}
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
            {RULE_FIELDS.map((field) => field.type === 'bool' ? (
              <label
                key={field.key}
                className="flex cursor-pointer items-start gap-3 rounded-lg border border-border bg-white/[0.02] p-3 transition-colors hover:border-border-strong"
              >
                <input
                  type="checkbox"
                  checked={Boolean(form[field.key])}
                  onChange={(e) => setForm((f) => ({ ...f, [field.key]: e.target.checked }))}
                  className="mt-0.5 h-4 w-4 accent-[#3b9dff]"
                />
                <span className="min-w-0">
                  <span className="block text-[13px] text-ink">{field.label}</span>
                  <span className="block text-xs text-muted">{field.help}</span>
                </span>
              </label>
            ) : (
              <div key={field.key}>
                <label
                  htmlFor={`rule-${field.key}`}
                  className="mb-1.5 block text-[13px] font-medium text-ink-soft"
                >
                  {field.label}
                </label>
                <input
                  id={`rule-${field.key}`}
                  type={field.type === 'number' ? 'number' : field.type === 'secret' ? 'password' : 'text'}
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

          {formError && (
            <div className="flex items-start gap-2 rounded-lg border border-danger-fg/25 bg-danger-bg p-3 text-sm text-danger-fg">
              <FiAlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="min-w-0 break-words">{formError}</span>
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="secondary" onClick={closeCreate}>
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
            hint="Comma-separated, checked by path rules only."
          />
          <Input
            label="A file to test content rules with (path)"
            placeholder="config/settings.py"
            value={testFilePath}
            onChange={(e) => setTestFilePath(e.target.value)}
          />
          <div>
            <label htmlFor="dry-run-content" className="mb-1.5 block text-[13px] font-medium text-ink-soft">
              Its content
            </label>
            <textarea
              id="dry-run-content"
              rows={3}
              value={testFileText}
              onChange={(e) => setTestFileText(e.target.value)}
              placeholder="aws_key = AKIA..."
              className="w-full resize-y rounded-xl border border-border bg-white/[0.04] px-3.5 py-2.5 font-mono text-[12px] text-ink outline-none transition-all duration-200 placeholder:text-muted hover:border-border-strong focus:border-accent/70 focus:bg-white/[0.06] focus:ring-4 focus:ring-accent/10"
            />
          </div>
          <Input
            label="Deleted paths"
            placeholder="generated/schema.sql"
            value={testDeleted}
            onChange={(e) => setTestDeleted(e.target.value)}
            hint="Comma-separated. Deleting a protected path counts as changing it."
          />
          <Input
            label="New branch name"
            placeholder="feature/login"
            value={testBranch}
            onChange={(e) => setTestBranch(e.target.value)}
            hint="Tests the branch-name rule as if this branch were being created."
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
                {testResult.note && <p className="mt-1.5 text-xs opacity-80">{testResult.note}</p>}
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
