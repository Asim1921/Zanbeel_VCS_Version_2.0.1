import React, { useState } from 'react'
import { FiClock, FiGlobe, FiShield, FiUnlock, FiUser } from 'react-icons/fi'
import GlassCard from '../ui/GlassCard'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import api from '../../utils/api'

/**
 * Sign-in protection as the server applies it: a few failures from one address stop
 * that address guessing at that account; many failures from anywhere lock the account,
 * each lock twice as long as the last; one address failing across many accounts is
 * throttled on its own. Everything here can be cleared early.
 */

function minutesLeft(seconds) {
  const minutes = Math.max(1, Math.round((seconds || 0) / 60))
  return minutes >= 120 ? `${Math.round(minutes / 60)} h left` : `${minutes} min left`
}

function ordinal(n) {
  const mod100 = n % 100
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`
  return `${n}${{ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th'}`
}

export default function SignInSecurityPanel({ data, reload, onNotice, onError }) {
  const [busy, setBusy] = useState(null)
  const policy = data?.policy || {}
  const locked = data?.locked_accounts || []
  const pairs = data?.throttled_pairs || []
  const addresses = data?.throttled_addresses || []

  const clear = async (key, username, ip, what) => {
    try {
      setBusy(key)
      onError(null)
      await api.unlockAccount(username, ip)
      onNotice(`${what} cleared.`)
      await reload()
    } catch (err) {
      onError(err.message || 'Could not clear the limit')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-4">
      <GlassCard hover={false} className="p-4">
        <p className="mb-2 text-[13px] font-medium text-ink-soft">Active policy</p>
        <div className="grid gap-3 text-[13px] text-muted sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <span className="block font-mono text-ink">{policy.max_failures_per_address ?? '—'} in {policy.account_window_minutes ?? '—'} min</span>
            failures from one address stop it guessing at that account
          </div>
          <div>
            <span className="block font-mono text-ink">{policy.max_account_failures ?? '—'} in {policy.account_window_minutes ?? '—'} min</span>
            failures from anywhere lock the account
          </div>
          <div>
            <span className="block font-mono text-ink">{policy.lockout_minutes ?? '—'} min, doubling</span>
            lock length, up to {policy.max_lockout_minutes ? `${Math.round(policy.max_lockout_minutes / 60)} h` : '—'}
          </div>
          <div>
            <span className="block font-mono text-ink">{policy.max_ip_failures ?? '—'} in {policy.ip_window_minutes ?? '—'} min</span>
            failures from one address, any account, throttle it
          </div>
        </div>
        <p className="mt-2 text-xs text-muted">
          Unknown usernames are counted and locked exactly like real ones, and every failed
          sign-in takes the same time, so neither the limits nor the timing say which accounts exist.
          A password reset by email unlocks an account at once.
        </p>
      </GlassCard>

      {locked.length === 0 && pairs.length === 0 && addresses.length === 0 ? (
        <EmptyState
          icon={FiShield}
          title="Nothing is locked or throttled"
          description="Limits apply automatically and lift themselves as their window passes."
        />
      ) : (
        <div className="space-y-2">
          {locked.map((l) => (
            <GlassCard key={`lock:${l.username}`} hover={false} className="flex flex-wrap items-center gap-3 p-4">
              <FiClock className="h-4 w-4 shrink-0 text-warning-fg" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-medium text-ink">{l.username}</span>
                  <Badge variant="warning">Locked · {minutesLeft(l.seconds_remaining)}</Badge>
                  {l.lock_count > 1 && <Badge variant="danger">{ordinal(l.lock_count)} lock in a row</Badge>}
                </div>
                <p className="mt-0.5 text-xs text-muted">
                  {l.reason}
                  {l.last_ip && ` · last attempt from ${l.last_ip}`}
                </p>
              </div>
              <Button variant="secondary" size="sm" disabled={busy === `lock:${l.username}`}
                onClick={() => clear(`lock:${l.username}`, l.username, null, `The lock on ${l.username}`)}
                className="flex items-center gap-1.5">
                <FiUnlock className="h-3.5 w-3.5" />
                Unlock
              </Button>
            </GlassCard>
          ))}
          {pairs.map((p) => (
            <GlassCard key={`pair:${p.username}:${p.ip_address}`} hover={false} className="flex flex-wrap items-center gap-3 p-4">
              <FiUser className="h-4 w-4 shrink-0 text-warning-fg" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-medium text-ink">{p.username}</span>
                  <Badge variant="warning">Throttled from {p.ip_address}</Badge>
                </div>
                <p className="mt-0.5 text-xs text-muted">
                  {p.failures} failed attempts from this address; it may not try this account again until they age out.
                </p>
              </div>
              <Button variant="secondary" size="sm" disabled={busy === `pair:${p.username}`}
                onClick={() => clear(`pair:${p.username}`, p.username, null, `The failures for ${p.username}`)}
                className="flex items-center gap-1.5">
                <FiUnlock className="h-3.5 w-3.5" />
                Clear
              </Button>
            </GlassCard>
          ))}
          {addresses.map((a) => (
            <GlassCard key={`ip:${a.ip_address}`} hover={false} className="flex flex-wrap items-center gap-3 p-4">
              <FiGlobe className="h-4 w-4 shrink-0 text-danger-fg" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-mono text-ink">{a.ip_address}</span>
                  <Badge variant="danger">Address throttled</Badge>
                </div>
                <p className="mt-0.5 text-xs text-muted">
                  {a.failures} failed attempts across accounts in {policy.ip_window_minutes} minutes.
                </p>
              </div>
              <Button variant="secondary" size="sm" disabled={busy === `ip:${a.ip_address}`}
                onClick={() => clear(`ip:${a.ip_address}`, null, a.ip_address, `The limit on ${a.ip_address}`)}
                className="flex items-center gap-1.5">
                <FiUnlock className="h-3.5 w-3.5" />
                Clear address
              </Button>
            </GlassCard>
          ))}
        </div>
      )}

      {(data?.recent_failures_by_ip || []).length > 0 && (
        <GlassCard hover={false} className="p-4">
          <p className="mb-2 text-[13px] font-medium text-ink-soft">
            Failures in the last {policy.ip_window_minutes} minutes, by source
          </p>
          <div className="space-y-1">
            {data.recent_failures_by_ip.slice(0, 8).map((r) => (
              <div key={r.ip_address} className="flex items-center justify-between font-mono text-[12px]">
                <span className="text-ink-soft">{r.ip_address}</span>
                <span className="text-muted">{r.failures}</span>
              </div>
            ))}
          </div>
        </GlassCard>
      )}
    </div>
  )
}
